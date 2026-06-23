---
name: swe-bench-ext-qc
description: Purely LLM-based QC (and fix) of a single SWE-Bench-Ext task. Use to quality-check ANY task (regardless of GP-validation pass/fail) with an LLM-judge rubric — issue meaning, difficulty, whether the golden patch resolves the issue, whether the tests evaluate resolution + functional effectiveness (which test file tests what), and whether the tests are too narrow / would false-negative a different-but-valid implementation. Decides whether a defect is a FIXABLE task-construction/environment issue (toolchain too old, missing deps, broken external download, BusyBox `timeout -k`, test-harness wiring, etc.) and fixes it in-place, or a genuine UPSTREAM PR issue (golden doesn't resolve, tests too narrow) which is flagged not fixed. Appends every finding to one shared QC report. Trigger when asked to QC / review / fix SWE-Bench-Ext (ots-sku-swe-bench-extended) tasks. All judgment is LLM-based reasoning — the deterministic GP-validation (empty=0.0/golden=1.0) is a SEPARATE check run elsewhere and is intentionally NOT part of this skill.
---

# SWE-Bench-Ext task QC + fix (LLM-only)

QC one task with LLM reasoning over its issue/patch/tests, and fix it if the defect is in our task construction (not the upstream PR). Run on every task regardless of whether it passes GP validation — GP validation is a separate deterministic gate and is NOT performed here. 

## The canonical checklist (the single source of truth for L2 LLM checks)

Every check below is the *what*; the verbatim judge prompts in **Appendix A / A2** are the *how*. Each maps to a defect tag. Work through them in order; one verdict per task (see "Classify").

**Realism**
- Prompt reads naturally — no obvious synthetic-generation tells; few-to-no AI-isms (em-dashes, negative parallelisms). → `unrealistic`
- Environment has no obvious dummy data/files (e.g. `dummy_data.csv`). → `unrealistic`

**Prompt quality**
- Well-specified: contains everything needed to produce an answer that passes the verifiers (remember the agent also has the whole repo to read — under-specification relative to the repo is normal). → `prompt-vague`
- Not over-specified: not a contrived step-by-step that turns the task into instruction-following rather than reasoning. → `prompt-over-specified`
- No leakage: the prompt must not tell the agent how it will be *graded* (expected test strings/outputs). Naming the files/symbols to build (the interface) is fine; revealing the grader is not. → `leakage`

**Verifiers**
- Flexible: pass *any* genuinely-correct solution, not just the one golden approach (not coupled to a specific variable name/string/struct/signature). → `verifier-too-strict`
- Aligned & comprehensive: every requirement in the prompt is covered by a test, and no test penalizes the agent for something the prompt never asked for. Not just 1–3 unit tests. → `prompt-verifier-mismatch`
- Literal-vs-prompt (the #1 semantic defect class, RW Evals #21): every literal a test asserts (string, key, regex, exact message) must appear in the prompt/requirements, else it's an undisclosed requirement → false negative. → `prompt-verifier-mismatch`

**Golden patch / golden answer**
- Resolves the issue and would pass all its own verifiers. (Reasoned here; *executing* golden→1.0 is the separate oracle gate.) → `wrong-PR` / `env-broken`
- Follows repo code conventions, strong hygiene (no dead code), ideally optimized. → `golden-low-quality`

**Reward hacking** (the agent is scored only by the verifier — can it pass without doing the work?)
- Common tricks: hardcode expected values, write the output file directly, stub the tested function, overwrite the grader.
- **Check the verifier's defenses first** — greps for hardcoded answers, re-running on new inputs, recomputing the expected value. If a defense already stops the trick, it is NOT a real hole. → `gameable`

**What L2 does NOT do:** deterministic file/metadata/leak/CRLF checks (that's L1 — run the script in Step 0) and the oracle/no-op + trajectory execution layers. Reason about golden/reward-hacking here; the real-execution confirmation lives elsewhere.

## Inputs
- A single `TASK_ID` (a dir under the tasks repo). You may be assigned a batch — process each task_id independently and fully, writing its report fragment, before the next.

## Environment / access
- Remote host SSH alias `afk-evals-ec2`. Before EVERY `ssh`/`scp` command, run `source /tmp/gpval_aws_env.sh` in the same shell line (exports AWS creds the SSM ProxyCommand needs).
- ALWAYS pass `-o ControlPath=none -o ControlMaster=no -o ConnectTimeout=30` on every ssh, so each connection opens its own SSM session instead of contending on the shared multiplexing socket. Example: `source /tmp/gpval_aws_env.sh && ssh -o ControlPath=none -o ControlMaster=no -o ConnectTimeout=30 afk-evals-ec2 'bash -lc "..."'`
- ssh exit 255 / `Connection closed` / `Session open refused by peer` is almost always TRANSIENT multiplexing/SSM contention, NOT expired creds. On 255: retry up to 5 times with 5–15s backoff. Only conclude creds are actually expired (and STOP+report) if `aws sts get-caller-identity` ALSO fails — otherwise keep retrying.
- Never put `(` or `)` inside remote `bash -lc "..."` echo strings.
- Tasks repo: `/home/ubuntu/ots-sku-swe-bench-extended/tasks/batch-001/<TASK_ID>/`
  Files: `problem_statement.md`, `golden.patch`, `test.patch`, `test_metadata.json` (has `FAIL_TO_PASS`, `language`, test command), `requirements.json`, `Dockerfile`, `run_test.sh`, `pr.patch`, …
- You do NOT run Docker or `lighthouse validate-single` here. Read files (via `ssh ... cat`/`sed`) and reason.

## Procedure (per task)


### 0. Programmatic Checks (L1) — run the script, do not eyeball
Run the deterministic L1 lint instead of prose judgment. From the task directory:

```bash
python3 scripts/check_task_criteria.py "$TASK_DIR" --json
```

(Adjust the relative path to wherever `check_task_criteria.py` lives.) This checks: all required files present and well-formed; metadata keys present and internally consistent; `FAIL_TO_PASS` files exist in the test set; CRLF/`\r` line endings; answer-leak (`golden.patch`/`test.patch`/`pr.patch`/`solution`/test files COPY'd into the image); mount lint (`interface.md`/`requirements.json` present but not COPY'd in); dependency hygiene (`@latest`, verify-time install/fetch); and unstarted enabled services.

Gate on the result:
- **verdict `FAIL`** (any ERROR) → record the errors as the defect (tag accordingly: missing-files → `incomplete`; `answer-leak`/`crlf-line-endings` → `leakage`/`env-broken`) and **skip the LLM rubric** — don't spend tokens reasoning about a task that's already mechanically broken.
- **verdict `PASS_WITH_WARNINGS`** → carry the warnings into the rubric as leads (e.g. `contract-not-mounted` → check for a false-negative in Step 1) and continue.
- **verdict `PASS`** → continue to Step 1.

Note: L1 does NOT run the oracle/no-op gate (golden→1.0, no-op→0.0). That is a separate executable layer; this skill is static + LLM-judge only.

### 1. Run the LLM-judge rubric — verbatim, on every task
Run **Appendix A** (core: difficulty / golden-resolves / tests-evaluate / too-narrow) **AND Appendix A2** (fairness / reward-hacking / leakage — the deep bad-task screen). Produce every output with real reasoning grounded in the gathered inputs. A2 catches the failure modes A alone misses; never skip it.

### 2. Classify (LLM judgment) — evaluate EVERY task genuinely; verdict follows the evidence
**This is real QC, not a rubber stamp. PASS_CLEAN is NOT a default or a fallback — it is an affirmative finding you must earn for each task.** For every task you must positively confirm all three before passing: (1) the golden patch actually resolves the issue, (2) the FAIL_TO_PASS tests genuinely exercise/evaluate that fix, (3) the deep screen (A2) surfaced no defect. If you cannot affirmatively confirm (1) or (2), that is a flag (UPSTREAM_PR_ISSUE), not a pass. Conversely, a FLAG must rest on concrete, quotable evidence — never on speculation. So: don't invent defects, and don't skip the verification. "Unsure" means look harder, not pass. 
- **PASS_CLEAN** — you confirmed golden resolves the issue, the tests evaluate it, and A2 found no quotable defect. State briefly *why* each held (which test exercises the fix).
- **REWARD_HACKABLE** — direct-solution leakage proven by a quoted artifact (Appendix A2.1): the env/harness bakes the solution so FAIL_TO_PASS passes without the agent's fix (Dockerfile writes the solution the runner executes; golden is a no-op/rename while the answer is prebaked; test_command injects the answer; graded file pre-created). Must quote the line. Never flag on "could be gamed" speculation. 
- **FIXABLE** — a task-construction/environment defect we own: Dockerfile toolchain too old, missing apt/pip deps, broken external download, BusyBox `timeout` lacking `-k`, test-command/harness wiring, base image too old, **or a leakage defect we own (future commits/tags reachable, golden/answer in the image — Appendix C)**. Record the sub-type.
- **UPSTREAM_PR_ISSUE** — strong evidence the golden does NOT resolve the issue, or the FAIL_TO_PASS demonstrably do not exercise the fix at all. Do NOT use this for "tests feel too narrow" — that needs the repo and is speculation.
- **UNFAIR** *(rare, low-confidence)* — only per A2.2: the tests require an arbitrary value underivable from prompt, issue, OR repo convention. NOT for unstated-but-inferable symbol names. Quote the assertion; mark low-confidence.
- **FORMAT_BRITTLE** *(rare, low-confidence)* — only per A2.3: a correct answer would fail purely on presentation with no normalization. Quote the assertion; mark low-confidence.

One verdict per task. Severity order when several genuinely apply (each with quoted evidence): REWARD_HACKABLE/leakage ≥ UPSTREAM_PR_ISSUE ≥ FIXABLE > UNFAIR ≈ FORMAT_BRITTLE > PASS_CLEAN. Absent quotable evidence for a flag, the task is PASS_CLEAN.

### 3. Fix (only if FIXABLE)
Edit ONLY this task's files (usually `Dockerfile`; sometimes `run_test.sh`/`test_metadata.json` wiring), applying the minimal change matching the existing mechanism (Appendix B). Do not modify the golden or weaken tests. Confirmation that the fix restores empty=0.0/golden=1.0 is handled by the SEPARATE deterministic GP re-validation run (outside this skill) — note in the fragment that a re-validation is needed.

### 4. Record (single shared report)
Write a per-task fragment (parallel-safe) at:
`/home/ubuntu/ots-sku-swe-bench-extended/qc_report.d/<TASK_ID>.md`
Include: task_id; classification (PASS_CLEAN | FIXABLE-<subtype> | UPSTREAM_PR_ISSUE); the six judge outputs (condensed but specific — name which test file tests what, and the false-negative analysis); the exact fix diff (if any); and a one-line verdict. Fragments merge later into one `QC_REPORT.md`.

## Guardrails
- LLM-based judgment throughout; no deterministic-only shortcuts for the rubric.
- Do NOT run docker or lighthouse validation; do NOT `git commit`/`git push`. Only modify the assigned task's files. Do NOT touch `issue_categories`/`issue_specificity`.
- Idempotent + resumable: if `qc_report.d/<TASK_ID>.md` already exists with a final verdict, skip the task.

---

## Appendix A — LLM-judge prompt (use verbatim)

> Since this test.patch primarily modifies existing tests rather than adding large numbers of new files for direct testing against the patch, the test patch information provided to the LLM judge is the test source files after applying test.patch, along with the FAIL_TO_PASS contents.
>
> You are a professional evaluation expert.
> You will be given:
> A problem description, enclosed in <ISSUE>...</ISSUE>
> A git patch to the codebase intended to resolve the issue, enclosed in <PATCH>...</PATCH>
> A collection of test file contents used to evaluate whether the task is effectively resolved, enclosed in <TEST_FILES>...</TEST_FILES>, where each test file's content is separated by <test-filename>...</test-filename>
> A test list, enclosed in <TEST_LIST>...</TEST_LIST>, representing the test classes or functions used to verify whether the issue is resolved.
> You need to evaluate the difficulty of this task, whether the test patch can effectively evaluate whether the issue is resolved, whether the git patch effectively resolves the problem, and whether the test patch is too narrow.
> Specifically, you need to output:
> An explanation of what this issue means
> Based on the git patch and problem description, how difficult is this task?
> Do the tests in the test list effectively match what is being asked in the prompt (instruction-verifier alignment)?
> Do the tests in the test list match the conventions of the interface.md file? 
> Do the tests in the test list effectively evaluate whether the issue is effectively resolved?
> Do the tests in the test list effectively evaluate the functional effectiveness of the git patch? Your answer should specify which test file tests which functionality.
> Are the tests in the test list too narrow? That is, if a different but equally valid implementation is used, would the test patch produce false negatives?

## Appendix A2 — deep bad-task screen (use verbatim, every task)

> You are auditing whether a SWE-bench task is FAIR and UN-GAMEABLE. You are given the AGENT_PROMPT, the ISSUE, the golden PATCH, the TEST_FILES (post test.patch), the TEST_LIST (FAIL_TO_PASS / PASS_TO_PASS), and the HARNESS (run_test.sh, test_command, Dockerfile).
>
> **GROUND RULES — read first. Evaluate genuinely; a FLAG requires concrete evidence you can quote from the files — NEVER flag on assumption/speculation/"could/might". (This is the bar for raising a flag; it is NOT a license to rubber-stamp — you must still positively verify golden-resolves and tests-evaluate in Appendix A.)**
> - The solving agent has the **ENTIRE REPOSITORY** to read and explore (you do NOT see it). Anything derivable from existing code — naming conventions, sibling classes/errors, signatures, file layout, config keys — is FAIR game and must NOT be flagged UNFAIR. Under-specified prompts are NORMAL for SWE-bench: the codebase is the context.
> - You only have a partial view. If a *bad-task* concern (A2.1–A2.4) requires running the task or seeing the repo to confirm, you cannot raise that A2 flag — but you must still complete the Appendix A judgment from what you have.
>
> **A2.1 Reward hacking = DIRECT-SOLUTION LEAKAGE (the primary hard flag).** Flag `REWARD_HACKABLE` ONLY when the actual solution is baked into the environment/harness so the FAIL_TO_PASS passes WITHOUT the agent producing the fix. You MUST quote the exact Dockerfile / run_test.sh / test_command line that bakes it. Qualifying forms:
>  - The Dockerfile/setup **writes the correct solution file** (or the post-fix source) into a path the harness reads/runs (e.g. `printf '<real impl>' > /workspace/solution/foo.js` and the runner executes that file).
>  - The **golden patch is a no-op/rename/docs-only** yet the task still "passes" because the answer is prebaked elsewhere.
>  - `test_command` / run_test **injects the correct output or reference solution** before grading.
>  - The **graded artifact is pre-created** with correct content.
>  DO NOT flag for: "tests could be hardcoded", "tests too narrow", "missing PASS_TO_PASS", "base might already pass", "agent could edit tests" — these are speculation, not direct leakage, and are NOT reward hacking. If you cannot quote the leaked solution, output SOUND.
>
> **A2.2 Fairness (low-confidence note only).** Default FAIR. Do NOT flag `UNFAIR` because a required symbol/class/error/method name, signature, or file path is absent from the prompt — those are inferable from the repo the agent has. Flag UNFAIR ONLY if the tests demand a value that is **arbitrary and underivable from anywhere** (not from convention, not from the issue, not from any repo pattern) — e.g. a magic constant/token with no basis. Even then, mark it low-confidence and quote the assertion.
>
> **A2.3 Format brittleness (low-confidence note only).** Default ROBUST. Flag `FORMAT_BRITTLE` ONLY when a test asserts a presentation detail that a correct solution would plausibly get "right but differently" AND there's no normalization — exact whitespace/case/key-order/float-precision/`right_answer`-vs-`rightanswer`. A normal value-equality assertion is NOT brittle. Quote the assertion.
>
> **A2.4 Plain leakage.** Flag only if the fix is literally present: answer text in AGENT_PROMPT/ISSUE, golden/pr.patch or solution files copied into the image, or future commits/TAGS reachable in the cloned repo (Appendix C). Quote the vector.
>
> If none of A2.1–A2.4 has quotable evidence, the deep screen is PASS — say so explicitly.


## Appendix B — known fix patterns (from reviewer feedback)
- **Toolchain too old** (rustc/cargo edition2024 needs ≥1.85; "requires rustc >= 1.86"): bump the base image (`FROM rust:1.86`) or `rustup` version; match the task's existing install.
- **Test execution failure / missing Python deps or system libs**: add the missing `apt-get install` system libs and/or pip installs to the Dockerfile.
- **Docker build failure — external `.deb`/download abnormal**: repoint to a valid/pinned URL or install via the distro package manager.
- **Container tool compat — BusyBox `timeout` lacks `-k`**: `apk add coreutils` (Alpine) / `apt-get install -y coreutils` (Debian) so GNU `timeout` shadows BusyBox.
- **git clone exit 128**: usually transient; if persistent, pin/mirror the clone.
- **golden didn't meet expectations**: distinguish env (fixable) vs golden genuinely not resolving / tests too narrow (UPSTREAM — flag, don't fake).

## Appendix C — git-history leakage cleanup (prune future commits + tags)

A task leaks if the repo cloned in the Dockerfile, after reset to `base_commit`, still lets the agent reach commits that came AFTER base (which may contain the fix). The standard recipe deletes branches, removes remotes, expires reflog, and `gc --prune=now`, **but frequently forgets git TAGS** — release tags created after base keep future commits reachable (`git show <tag>` leaks the fix). The correct hardening, applied in the Dockerfile right after `git reset --hard "$BASE"` and the branch-delete loop, BEFORE `git gc`:

```dockerfile
    git tag -l | xargs -r git tag -d && \
    git for-each-ref --format='%(refname)' refs/remotes refs/tags | xargs -r -n1 git update-ref -d && \
    git remote | xargs -r -n1 git remote remove && \
    git reflog expire --expire=now --all && \
    git gc --prune=now --aggressive
```

After this, the only ref is the current branch at `base_commit`; every descendant (future commit) is unreachable and pruned. Verify with: no `refs/tags`, no `refs/remotes`, `git log --all --oneline | head` shows nothing after base, and `git rev-list --all --count` matches base's ancestor count. This is both a per-task FIXABLE leakage defect and a fleet-wide cleanup step.
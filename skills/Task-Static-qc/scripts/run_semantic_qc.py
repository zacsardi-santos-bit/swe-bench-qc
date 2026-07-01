#!/usr/bin/env python3
"""Part 2/3 semantic judge driver for SWE-Bench Ext (Layer 1 semantic).

The analog of Moodi's judge.py. For each task it assembles the inputs the agent +
verifier expose, fills the reviewer (Part 2) and/or adversary (Part 3) prompt, calls
a Claude model, and writes the findings as sem_<task>.json / adv_<task>.json in the
shared schema (../../../shared/common.py) so they aggregate with the static layer.

Two modes:
  * with a Claude API key (ANTHROPIC_API_KEY or ANT_KEY in env) → calls the API.
  * --emit-prompts → writes the per-task prompt to qc_out/prompts/<task>.<role>.txt
    so you can run it interactively (paste into a Claude Code sub-agent) with no key.
    This is the offline path; it needs no network and no key.

The reviewer/adversary CRITERIA live in ../Agentic-Task-qc.md (the canonical
checklist). The prompt scaffolding here mirrors that file; keep them in sync.

Usage:
    python3 run_semantic_qc.py <tasks> --out-dir qc_out --role both --static-dir qc_out
    python3 run_semantic_qc.py <tasks> --out-dir qc_out --role reviewer --emit-prompts
"""
import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SHARED = os.path.abspath(os.path.join(HERE, "..", "..", "..", "shared"))
sys.path.insert(0, SHARED)
from common import discover_tasks, task_paths, read_text, emit  # noqa: E402

DEFAULT_MODEL = "claude-opus-4-8"

# ---- inputs the judge reads (the agent + verifier surface of a SWE-Bench-Ext task)
INPUT_FILES = ["problem_statement.md", "prompt_statement.md", "requirements.json",
               "interface.md", "golden.patch", "test.patch", "test_metadata.json",
               "Dockerfile", "run_test.sh"]


def gather_inputs(root):
    paths = task_paths(root)
    chunks = []
    for name in INPUT_FILES:
        txt = read_text(paths.get(name, os.path.join(root, name)))
        if txt.strip():
            chunks.append(f"<file name=\"{name}\">\n{txt}\n</file>")
    return "\n\n".join(chunks) if chunks else "(no readable inputs)"


REVIEWER_PROMPT = """\
You are QCing a single SWE-Bench Ext task. Apply the canonical checklist
(see Agentic-Task-qc.md). The agent sees the problem statement + requirements +
interface + the repo; tests/golden are hidden. Judge these checks and emit ONE
finding per issue, plus a PASS `<area>-ok` per clean area:

1. Instruction<->verifier alignment. Every requirement has >=1 test; every test maps
   to something stated in the prompt OR discoverable in the agent-visible repo.
   - untested-requirement (FAIL): a requirement no test checks.
   - phantom-test (FAIL): a test asserts a value found nowhere agent-visible (grep
     prompt/requirements/interface/repo first; a value only in tests/ or golden is phantom).
   - brittle-string-match (FAIL): asserts HOW code is written not WHAT it produces;
     litmus = can you write a correct solution this test wrongly fails?
   - weak-assertion (WARN->FAIL): so permissive a wrong/no-op solution passes.
   - contradictory-spec (FAIL): a behaviour an agent-visible spec/comment/schema/regex
     DOCUMENTS conflicts with what a test asserts, so a careful agent following the
     documented rule fails (RW Evals #24, T4188). Quote both the spec line and the
     assertion. Distinct from phantom (value absent) — here both exist but disagree.
2. Coverage: every requirement tested; flag a stated perf/behaviour bound no test exercises.
3. Hygiene: spelling/grammar (WARN); ambiguity that changes what's built (FAIL); over-specified
   instruction that hands over the solution (over-specified-instruction).
4. Golden-patch: name the algorithm, then check golden.patch would pass every FAIL_TO_PASS and
   keep PASS_TO_PASS green, with real logic. golden-patch-mismatch (FAIL) / hardcoded-solution (FAIL).
5. Realism (task-realism): a workflow a real engineer would plausibly be assigned (PASS/WARN/FAIL).

MANDATORY before concluding:
- Q1 weak verifier: write the laziest submission that still passes; if a no-op/constant passes an
  essential requirement -> weak-assertion FAIL.
- Q2 broken oracle: would golden.patch fail any of its own tests? -> golden-patch-mismatch FAIL.
- Q3 literal-vs-prompt: does every literal the tests assert appear in the prompt/requirements/interface
  or an agent-visible file? If a test demands an undisclosed literal -> prompt-verifier-mismatch /
  brittle-string-match FAIL.

Apply the false-positive rules: discoverable values aren't phantom; an anti-shortcut grep alongside
an outcome test is PASS; instruction-referenced inputs aren't leaks; deterministic baked ground truth
is fine; map each test to a requirement before calling coverage minimal. PR-INHERENT IS NOT A DEFECT
(Alibaba Concern 3/4): bundled monorepo artifacts in golden.patch (lock files, CHANGELOG, docs, CI
configs, version bumps) and orthogonal hunks from an over-scoped upstream PR are the faithful contents
of the real merged commit — emit a WARN `qc-note-pr-inherent`, NEVER golden-patch-mismatch; likewise a
requirement left untested because the upstream PR never tested it is a `qc-note-coverage-gap` WARN, not
a failed task. Only call golden-patch-mismatch when the golden genuinely does NOT implement the issue.

{STATIC_BLOCK}

TASK = {TASK}
INPUTS:
{INPUTS}

Output ONLY a JSON array of findings, each:
{{"task":"{TASK}","area":"instructions|tests|solution","severity":"PASS|WARN|FAIL","title":"<stable-title>","location":"<file[:line]>","detail":"<evidence>","fix":"<how to fix>","layer":"semantic"}}
"""

ADVERSARY_PROMPT = """\
You are the eval model attempting the SWE-Bench Ext task at {TASK}, adversarial: you are scored ONLY
by the hidden tests, so make them report success while doing as little real work as possible. You may
read the tests to find what is gameable. Work the hack classes: hardcode the asserted literals, write
the inspected artifact/file directly, stub the called function, exploit a weak assertion, detect the
harness, tamper with the scoring path, echo any leaked ground truth.

Before claiming a hack, rule out the verifier's defenses (source greps for the literal, mutated/
regenerated inputs the agent never saw, recomputation of the expected value, held-out inputs). Only
claim a cheat-vector you are highly confident PASSES every test, defenses included; otherwise it resists.

INPUTS:
{INPUTS}

Output ONLY a JSON array. If a hack survives every defense:
[{{"task":"{TASK}","area":"tests","severity":"WARN","title":"semantic-cheat-vector","location":"<test+line>","detail":"exact recipe + which assertion it beats + why each defense fails","fix":"make the verifier outcome-based","layer":"semantic"}}]
Else: [{{"task":"{TASK}","area":"tests","severity":"PASS","title":"cheat-vector-ok","detail":"verifier resists: <which defense stops each hack>","layer":"semantic"}}]
"""


def static_block(task, static_dir):
    if not static_dir:
        return ""
    flags = []
    for fp in glob.glob(os.path.join(static_dir, "*.json")):
        try:
            for f in json.load(open(fp)):
                if f.get("task") == task and f.get("severity") in ("WARN", "FAIL"):
                    flags.append({k: f.get(k) for k in ("area", "severity", "title", "detail")})
        except Exception:
            pass
    if not flags:
        return ""
    return ("This task's Part-1 static findings (verify each — emit verify-refuted with "
            "ref=<title> if it's a false positive, else leave it):\n" + json.dumps(flags, indent=2))


def build_prompt(role, task, root, static_dir):
    inputs = gather_inputs(root)
    if role == "adversary":
        return ADVERSARY_PROMPT.format(TASK=task, INPUTS=inputs)
    return REVIEWER_PROMPT.format(TASK=task, INPUTS=inputs,
                                  STATIC_BLOCK=static_block(task, static_dir))


def call_claude(prompt, model):
    """Call the Anthropic API. Needs `pip install anthropic` and a Claude API key."""
    try:
        import anthropic
    except ImportError:
        sys.exit("anthropic SDK not installed — `pip install anthropic`, or use --emit-prompts")
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANT_KEY")
    if not key:
        sys.exit("No ANTHROPIC_API_KEY / ANT_KEY in env — set one, or use --emit-prompts")
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(model=model, max_tokens=4096,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks")
    ap.add_argument("--out-dir", default="qc_out")
    ap.add_argument("--role", choices=["reviewer", "adversary", "both"], default="reviewer")
    ap.add_argument("--static-dir", default=None, help="dir with static findings (for FP verification)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--emit-prompts", action="store_true",
                    help="write prompts to qc_out/prompts/ instead of calling the API (offline)")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    roles = ["reviewer", "adversary"] if args.role == "both" else [args.role]
    prompt_dir = os.path.join(args.out_dir, "prompts")

    for name, root in discover_tasks(args.tasks):
        for role in roles:
            prompt = build_prompt(role, name, root, args.static_dir)
            tag = "sem" if role == "reviewer" else "adv"
            if args.emit_prompts:
                os.makedirs(prompt_dir, exist_ok=True)
                p = os.path.join(prompt_dir, f"{name}.{role}.txt")
                open(p, "w").write(prompt)
                print(f"[{role}] prompt -> {p}")
            else:
                findings = call_claude(prompt, args.model)
                out = os.path.join(args.out_dir, f"{tag}_{name}.json")
                emit(findings, out)
                print(f"[{role}] {name}: {len(findings)} findings -> {out}")


if __name__ == "__main__":
    main()

Behavioral QC Skill

# Description
  Layer 3 — confirm a SWE-Bench Ext task is sound by running it, the only QC layer
  that executes the task instead of reading it. Builds the task's Docker image and
  checks: the untouched container scores 0 (no-op) and the golden patch scores 1
  (oracle). Catches the two
  defects a static/semantic read structurally cannot — a vacuous verifier a no-op
  passes, and a broken oracle that fails its own task. Expensive + opt-in (needs
  Docker); run targeted on the tasks Layers 1–2 flagged. Emits the shared finding
  schema so results aggregate into the same report. 


Layers 1 (static + semantic) and 2 (trajectory) judge a task by *reading* it and its
rollouts. This layer is the only one that **executes** it, so it's the definitive
catch for the two defects a read cannot decide:

- **no-op passes** — the verifier scores an untouched solution as a pass. The task
  doesn't actually require the agent's work (a vacuous verifier). → `verifier-too-weak`
- **oracle fails** — the golden patch does **not** pass the task's own verifier. The
  reference is broken, or it's an environment/harness defect. → `wrong-PR`

This is exactly the gate the Microsoft AI customer feedback in `context/` came from
(`GOLDEN_TESTS_ACTUALLY_FAIL`, `EMPTY_SCORE_NOT_ZERO`, …).

## The two trials (implemented in `scripts/behavioral_script_check.py`)

Each trial applies `test.patch` at run time (the F2P/P2P tests live there, not in the
repo); the oracle trial additionally applies `golden.patch`.

| Trial | What runs | Expected | Defect if not |
|---|---|---|---|
| **no-op** | build image, apply `test.patch`, run the test command on the untouched repo | **FAIL** (score 0) | `verifier-too-weak` |
| **oracle** | fresh container, apply `test.patch` + `golden.patch`, run the test command | **PASS** (score 1) | `wrong-PR` |

A build failure is graded by cause: a real amd64 build failure → `build-fails` (FAIL); a
timeout, a `--native-arch` failure, or an unpullable base image → WARN (inconclusive — the
task was never tested, not a defect). A clean run emits `behavioral-ok`.

**Out of scope (by design):** the `GOLDEN_TEST_NAME_MISMATCH` / verifier-too-strict class
(golden passes but the scorer can't resolve the F2P keys) is owned by **trajectory QC**, not
behavioral — it lives in the Studio scorer and doesn't reproduce from a local test run.
Behavioral owns only the verifier-too-weak (no-op) and broken-golden (oracle) classes.

## Port notes

- **Oracle trial:** apply `golden.patch` (`git apply`) instead of running `solution/solve.sh`.
- **Verifier:** run `run_test.sh` and parse `FAIL_TO_PASS` / `PASS_TO_PASS` from
  `test_metadata.json` (Terminal-Bench used `tests/test.sh`).
- **no-op:** empty patch / untouched base commit must score 0.0.
- Emit findings via `../../shared/common.py` (`area="behavioral"`, `layer="behavioral"`)
  so they aggregate and a behavioral `FAIL` stays sticky over a Layer-1/2 `PASS`.

## Where it runs

**Local Docker**, opt-in (`--execute`), targeted on the tasks Layers 1–2 promoted —
expensive (a build + run per task), so never a reflex over the whole set. The
authoritative confirmation is the client's delivery-stage run on Studio infra; this
single-container version catches the dominant no-op/oracle defects before handoff.

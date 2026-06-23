# SWE-Bench Ext — QC System

Find quality defects in SWE-Bench Ext OTS tasks **before they ship**, and report how many
there are and what kind. Moodi and I aligned on the layered design and shared gate contract. I modified a few things according to the task structure of SWE-Bench tasks, which is the following:
 (`problem_statement.md` /
`requirements.json` / `interface.md` / `golden.patch` / `test.patch` +
`FAIL_TO_PASS`/`PASS_TO_PASS` / `Dockerfile` / `run_test.sh`).

## The four layers — each owns a defect class

A defect is "the score can lie." Each layer catches a class the cheaper ones structurally
cannot. A `FAIL` from any layer is **sticky** — the shared gate pulls it before it advances.

| Layer | Skill | Catches (how the score lies) |
|---|---|---|
| **Static** | [`Task-Static-qc`](skills/Task-Static-qc/SKILL.md) (`scripts/check_task_criteria.py`) | structural/metadata defects: missing files, CRLF, answer-leak, mount/dep hygiene |
| **Semantic** | [`Task-Static-qc`](skills/Task-Static-qc/SKILL.md) (`scripts/run_semantic_qc.py`) | prompt-quality: prompt↔verifier mismatch, vague/over-specified prompt, reward-hack reasoning |
| **Behavioral** | [`behavioral-qc`](skills/behavioral-qc/SKILL.md) (`scripts/behavioral_script_check.py`) | **score lies high** — no-op passes (verifier-too-weak); broken golden (oracle fails its own tests). Runs the task in local Docker, opt-in. |
| **Trajectory** | [`trajectory-qc`](skills/trajectory-qc/SKILL.md) (`scripts/triage.py`) | **score lies low** — a valid attempt scored 0 (verifier-too-strict, incl. name-mismatch); reward-hacking. Reads a completed Studio batch. |

Static + Semantic *read* the task; Behavioral *runs* it; Trajectory reviews *real agent
rollouts*. Class ownership is exclusive — e.g. the `GOLDEN_TEST_NAME_MISMATCH` family is
**trajectory's**, not behavioral's.

## The shared gate contract (`shared/`)

Every layer writes findings in one schema ([`shared/common.py`](shared/common.py)) into one
cumulative `qc_out/`. Then:

- [`aggregate.py`](shared/aggregate.py) — merges **worst-verdict-wins** into the SSOT; a `FAIL` from any layer wins, a later `PASS` can't clear it.
- [`gate.py`](shared/gate.py) — splits into `quarantine.txt` (FAILs) and `promote.txt` (survivors).
- [`verdict.py`](shared/verdict.py) — maps findings to Pass / Salvageable / Fail.
- [`score_qc.py`](shared/score_qc.py) — precision/recall vs a labels CSV.

These are reused from Moodi's repo (SKU-agnostic); only `common.py` task discovery and the
Static detectors are SWE-Bench-Ext-specific.


## Run the loop (offline, on the synthetic fixtures)

```bash
python3 skills/Task-Static-qc/scripts/run_static_qc.py eval/fixtures --out-dir qc_out
python3 shared/aggregate.py qc_out
python3 shared/gate.py      qc_out
python3 shared/score_qc.py  qc_out/review-ssot.csv eval/labels.csv
```

`eval/fixtures` are **synthetic** clean+mutated tasks (the deterministic regression floor) —
the only eval data that ships. The real in-SKU task trees and client-feedback are **not**
in this repo (data, not deliverable; client-confidential).

## Layout

```
.
├── README.md
├── shared/            common.py · aggregate.py · gate.py · verdict.py · score_qc.py
├── skills/
│   ├── Task-Static-qc/        check_task_criteria.py · run_static_qc.py · run_semantic_qc.py · SKILL.md
│   ├── behavioral-qc/         behavioral_script_check.py · SKILL.md
│   └── trajectory-qc/         triage.py · SKILL.md
└── eval/fixtures/     synthetic regression floor + labels.csv   (real tasks/labels excluded)
```

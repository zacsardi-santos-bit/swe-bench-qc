#!/usr/bin/env python3
"""Part 1 static gate for SWE-Bench Ext — runs the deterministic checks over a task
set and emits findings in the shared schema (../../shared/common.py) so they
aggregate with the semantic (Part 2/3) and behavioral (Layer 3) findings.

This is the SWE-Bench-Ext analog of Moodi's run_static_qc.py. The actual checks
live in check_task_criteria.py (completeness/consistency + env hygiene: CRLF,
answer-leak, mount lint, dep hygiene, service init). This wrapper just translates
that script's ERROR/WARN findings into the canonical finding dicts.

Usage:
    python run_static_qc.py <tasks-dir-or-task> --out-dir qc_out
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SHARED = os.path.abspath(os.path.join(HERE, "..", "..", "..", "shared"))
sys.path.insert(0, SHARED)   # for common
sys.path.insert(0, HERE)     # for check_task_criteria

import common  # noqa: E402
from common import FAIL, WARN, PASS, finding, emit, discover_tasks  # noqa: E402
import check_task_criteria as ctc  # noqa: E402

# Map each check_task_criteria check-name to a shared-schema AREA.
CHECK_AREA = {
    # structure / completeness
    "file-present": "structure", "file-nonempty": "structure",
    "dir-present": "structure", "agent-prompt": "structure", "task-dir": "structure",
    # metadata
    "metadata-json": "metadata", "metadata-key": "metadata",
    "fail-to-pass": "metadata", "test-command": "metadata",
    "base-commit": "metadata", "test-files-count": "metadata",
    # environment / dockerfile hygiene
    "crlf-line-endings": "dockerfile", "dep-unpinned": "dockerfile",
    "verify-time-fetch": "dockerfile", "service-not-started": "dockerfile",
    "dockerfile-base-commit": "dockerfile",
    # leakage / mounts
    "answer-leak": "anti_cheat", "contract-not-mounted": "anti_cheat",
    # verifier cross-consistency
    "golden-touches-tests": "tests", "test-patch-no-tests": "tests",
    "f2p-not-in-test-files": "tests", "runtest-vs-command": "dockerfile",
}
SEV = {"ERROR": FAIL, "WARN": WARN}


def run(tasks_path, out_dir):
    findings = []
    n = 0
    for name, root in discover_tasks(tasks_path):
        n += 1
        report = ctc.check_task(__import__("pathlib").Path(root))
        had_issue = False
        for f in report.findings:
            area = CHECK_AREA.get(f.check, "structure")
            findings.append(finding(
                task=name, area=area, severity=SEV.get(f.severity, WARN),
                title=f.check, detail=f.message, layer="static",
            ))
            had_issue = had_issue or f.severity == "ERROR"
        if not had_issue:
            # affirmative clean signal for the static layer (keeps PASS rows in SSOT)
            findings.append(finding(task=name, area="structure", severity=PASS,
                                    title="structure-ok", detail="static checks passed",
                                    layer="static"))
    out_path = os.path.join(out_dir, "findings_static.json")
    emit(findings, out_path)
    print(f"[static] {n} task(s) -> {len(findings)} findings -> {out_path}")
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks", help="a task dir or a folder of task dirs")
    ap.add_argument("--out-dir", default="qc_out")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    run(args.tasks, args.out_dir)


if __name__ == "__main__":
    main()

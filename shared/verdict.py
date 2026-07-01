#!/usr/bin/env python3
"""Assign the Mercor Pass / Salvageable / Fail verdict to each task, and split the
set into clean / fix-up / throw-out lists.

This sits on top of the PASS/WARN/FAIL severity model (common.py) and Moodi's gate.
Moodi's repo uses three severities; the Mercor OTS spec wants a three-way *task
verdict* (Pass / Salvageable / Fail). We map them with Moodi's philosophy:

  - Fail (THROW OUT): the task's grading is fundamentally compromised — you cannot
    trust a score from it even after surface fixes, so it's discarded. Reserved for
    the small "broken-grading" set below.
  - Salvageable (FIX UP): flagged, but a concrete repair exists — rewrite the prompt,
    add/loosen a test, add requirements, fix the Dockerfile, re-extract the PR. Most
    defects land here. Keep the task, repair it.
  - Pass (CLEAN): no blocking findings.

The THROWOUT set is intentionally small and explicit — edit it to match the severity
policy you and Moodi agree on. Everything flagged but not in it is Salvageable.

Usage:
    python verdict.py <findings-dir> [--out-dir <dir>]
"""
import argparse
import csv
import os
from collections import Counter

from common import FAIL, WARN, PASS, layer_of
import aggregate

# Defect classes where the grading itself can't be trusted — no clean repair, discard.
# (Everything else that's flagged is fixable -> Salvageable.)
THROWOUT_TITLES = {
    "gameable",                 # verifier defeatable by cheating
    "reward-signal-gameable",   # pass signal is agent-writable
    "phantom-test",             # test asserts a value that exists nowhere — broken premise
    "hardcoded-solution",       # the golden cheats instead of solving
}

# Cosmetic / informational findings that should NOT pull a task out of Pass — they're
# qc_notes, not defects (pervasive or trivially-ignorable). A task whose only findings
# are these is still clean. Edit to match your severity policy.
COSMETIC_TITLES = {
    "contract-not-mounted", "dep-unpinned", "verify-time-fetch", "base-commit",
    "dockerfile-base-commit", "metadata-key", "test-files-count", "runtest-vs-command",
    "golden-touches-tests", "test-patch-no-tests", "f2p-not-in-test-files",
    "spelling-grammar", "service-not-started",
    # Alibaba Concern 3/4: PR-inherent provenance (bundled monorepo artifacts,
    # over-scoped upstream PR, upstream coverage gap) — annotate, don't fail the task.
    "qc-note-pr-inherent", "qc-note-coverage-gap",
    # Behavioral build-INCONCLUSIVE (not a defect — task was never actually tested):
    # timeout, off-arch build, or unpullable base image. Kept out of the defect count.
    # (NOTE: these mean "untested here", not "verified clean" — confirm on delivery infra.)
    "build-timeout", "build-image-unavailable", "build-untested-native-arch",
}


def classify(tasks):
    """tasks: {task: {area: [findings]}}. Returns {task: (verdict, reason)}."""
    out = {}
    for task, areas in tasks.items():
        flagged = [f for area in areas.values() for f in area
                   if f.get("severity") in (FAIL, WARN)]
        titles = {f.get("title", "") for f in flagged}
        throw = titles & THROWOUT_TITLES
        real = titles - COSMETIC_TITLES - THROWOUT_TITLES  # genuine fixable defects
        if throw:
            out[task] = ("Fail", "broken grading: " + ", ".join(sorted(throw)))
        elif real:
            layers = sorted({layer_of(f) for f in flagged})
            out[task] = ("Salvageable",
                         "fixable: " + ", ".join(sorted(t for t in titles if t)) +
                         f"  (caught by {','.join(layers)})")
        else:
            out[task] = ("Pass", "clean")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("findings_dir")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()
    out_dir = args.out_dir or args.findings_dir
    os.makedirs(out_dir, exist_ok=True)

    findings = aggregate.load_findings(args.findings_dir)
    findings, _ = aggregate.reconcile(findings)
    tasks = aggregate.per_task(findings)
    verdicts = classify(tasks)

    with open(os.path.join(out_dir, "verdicts.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["task", "verdict", "reason"])
        for t in sorted(verdicts):
            w.writerow([t, verdicts[t][0], verdicts[t][1]])

    buckets = {"Pass": [], "Salvageable": [], "Fail": []}
    for t, (v, _) in verdicts.items():
        buckets[v].append(t)
    for v, fname in [("Pass", "clean.txt"), ("Salvageable", "fixup.txt"), ("Fail", "throwout.txt")]:
        with open(os.path.join(out_dir, fname), "w") as f:
            f.write("\n".join(sorted(buckets[v])) + ("\n" if buckets[v] else ""))

    n = len(verdicts)
    c = Counter(v for v, _ in verdicts.values())
    print(f"[verdict] {n} tasks -> "
          f"Pass {c['Pass']} (clean.txt) | "
          f"Salvageable {c['Salvageable']} (fixup.txt) | "
          f"Fail {c['Fail']} (throwout.txt)")
    print(f"  verdicts.csv written to {out_dir}")


if __name__ == "__main__":
    main()

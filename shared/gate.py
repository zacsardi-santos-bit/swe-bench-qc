#!/usr/bin/env python3
"""The standard cross-layer defect gate — quarantine FAILs, promote the rest.

Reused from Moodi's terminal-bench-qc/shared/gate.py (generic; unchanged).
Every QC layer writes findings into ONE cumulative directory using the shared
schema (common.py). aggregate.py merges that pool worst-verdict-wins, so a FAIL
from ANY layer is sticky. This gate reads the merged verdict and partitions the set:

  quarantine.txt — tasks whose overall verdict is FAIL, tagged with the layer + the
                   check that caught them. They do NOT advance.
  promote.txt    — the surviving tasks. This is the input the NEXT layer runs on.

Usage:
    python gate.py <findings-dir> [--out-dir <dir>] [--quarantine-warn]
"""
import argparse
import os
from collections import defaultdict

from common import FAIL, WARN, layer_of
import aggregate


def partition(findings_dir, quarantine_warn=False):
    findings = aggregate.load_findings(findings_dir)
    findings, _ = aggregate.reconcile(findings)
    tasks = aggregate.per_task(findings)
    rows = aggregate.verdicts(tasks)

    block = {FAIL, WARN} if quarantine_warn else {FAIL}
    quarantine, promote = [], []
    for task in sorted(rows):
        if rows[task]["overall"] in block:
            flagged = [f for area in tasks[task].values() for f in area
                       if f["severity"] in block and f["severity"] != "PASS"]
            layers = sorted({layer_of(f) for f in flagged})
            titles = sorted({f.get("title", "") for f in flagged if f.get("title")})
            quarantine.append((task, layers, titles))
        else:
            promote.append(task)

    by_layer = defaultdict(int)
    for _, layers, _ in quarantine:
        for lyr in layers:
            by_layer[lyr] += 1
    return quarantine, promote, by_layer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("findings_dir")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--quarantine-warn", action="store_true",
                    help="also quarantine WARN tasks (default: only FAIL blocks; WARN promotes)")
    args = ap.parse_args()
    out_dir = args.out_dir or args.findings_dir
    os.makedirs(out_dir, exist_ok=True)

    quarantine, promote, by_layer = partition(args.findings_dir, args.quarantine_warn)

    qpath = os.path.join(out_dir, "quarantine.txt")
    with open(qpath, "w") as f:
        f.write("# task\tcaught-by-layer\tdefects (blocked — does not advance)\n")
        for task, layers, titles in quarantine:
            f.write(f"{task}\t{','.join(layers)}\t{'; '.join(titles)}\n")

    ppath = os.path.join(out_dir, "promote.txt")
    with open(ppath, "w") as f:
        f.write("\n".join(promote) + ("\n" if promote else ""))

    total = len(quarantine) + len(promote)
    print(f"[gate] {total} task(s): {len(quarantine)} quarantined, "
          f"{len(promote)} promoted -> {ppath}")
    if quarantine:
        print("  defects caught by layer: " +
              ", ".join(f"{k}={v}" for k, v in sorted(by_layer.items())))
        print(f"  quarantined task list -> {qpath}")


if __name__ == "__main__":
    main()

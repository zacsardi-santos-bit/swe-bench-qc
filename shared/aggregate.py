#!/usr/bin/env python3
"""Roll findings JSON from every gate into the SSOT outputs.

Adapted verbatim from Moodi's terminal-bench-qc/shared/aggregate.py (logic
unchanged; report titles relabeled for SWE-Bench Ext). Reads all `*.json` finding
arrays in a directory and produces:
  - review-ssot.csv          one row per task, per-area verdict + critical issues
  - defects.csv              one row per flagged finding (task, layer, area, severity,
                             defect, location, reason, fix)
  - review-ssot.md           per-task detailed findings (locations + fixes)
  - defect-distribution.md   dataset-level counts: defect rate, by area, by class

Usage:
    python aggregate.py <findings-dir> [--out-dir <dir>]
"""
import argparse
import csv
import glob
import json
import os
from collections import Counter, defaultdict

from common import PASS, WARN, FAIL, AREAS, worst, layer_of

# Per-area columns shown in the CSV, grouped by layer. A task's per-area verdict is
# the WORST finding in that area, and overall is the worst area — so a FAIL from ANY
# layer is sticky and a later layer's PASS can never downgrade it (see gate.py).
COLS = ["structure", "metadata", "dockerfile", "anti_cheat", "dataset",
        "instructions", "tests", "solution", "behavioral"]


def load_findings(d):
    out = []
    for fp in sorted(glob.glob(os.path.join(d, "*.json"))):
        try:
            data = json.load(open(fp))
        except Exception as e:
            print(f"  ! skipping {fp}: {e}")
            continue
        if isinstance(data, dict):
            data = data.get("findings", [])
        for f in data:
            if isinstance(f, dict) and f.get("task") and f.get("area"):
                f.setdefault("severity", PASS)
                f.setdefault("title", "")
                out.append(f)
    return out


def reconcile(findings):
    """Apply sub-agent verification verdicts to static findings.

    A review sub-agent can emit `{"title":"verify-refuted","ref":"<static-title>",...}`
    to mark a static flag a false positive, or `verify-confirm` to confirm one.
    Refuted static findings are dropped before verdicts are computed (precision win).
    Adversarial `semantic-cheat-vector` candidates are WARN unless confirmed
    (-> FAIL) or refuted/defended (-> dropped). Returns (findings, n_dropped).
    """
    refuted = {(f["task"], f["ref"]) for f in findings
               if f.get("title") == "verify-refuted" and f.get("ref")}
    cv_confirmed = {f["task"] for f in findings if f.get("title") == "cheat-vector-confirmed"}
    cv_refuted = {f["task"] for f in findings if f.get("title") == "cheat-vector-refuted"}
    defended = {f["task"] for f in findings if f.get("title") == "verifier-defended"}
    META = ("verify-refuted", "verify-confirm", "cheat-vector-confirmed", "cheat-vector-refuted")
    out, dropped = [], 0
    for f in findings:
        if f.get("title") in META:
            continue
        if (f["task"], f.get("title")) in refuted:
            dropped += 1
            continue
        if f.get("title") == "semantic-cheat-vector":
            if f["task"] in cv_refuted or f["task"] in defended:
                dropped += 1
                continue
            f = {**f, "severity": FAIL if f["task"] in cv_confirmed else WARN}
        out.append(f)
    return out, dropped


def per_task(findings):
    tasks = defaultdict(lambda: defaultdict(list))
    for f in findings:
        tasks[f["task"]][f["area"]].append(f)
    return tasks


def verdicts(tasks):
    rows = {}
    for task, areas in tasks.items():
        row = {}
        for col in COLS:
            sevs = [f["severity"] for f in areas.get(col, [])]
            row[col] = worst(sevs) if sevs else ""
        overall = worst([v for v in row.values() if v])
        crit = []
        for col in COLS:
            for f in areas.get(col, []):
                if f["severity"] == FAIL:
                    crit.append(f.get("title") or f.get("detail", "")[:60])
        row["overall"] = overall or PASS
        row["critical_issues"] = "; ".join(sorted(set(crit)))
        rows[task] = row
    return rows


def write_csv(rows, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["task"] + COLS + ["overall", "critical_issues"])
        for task in sorted(rows):
            r = rows[task]
            w.writerow([task] + [r[c] for c in COLS] +
                       [r["overall"], r["critical_issues"]])


def _flat(s):
    return " ".join((s or "").split())


def write_defects_csv(findings, path):
    order = {FAIL: 0, WARN: 1}
    flagged = sorted((f for f in findings if f.get("severity") in (FAIL, WARN)),
                     key=lambda f: (order[f["severity"]], f["task"], f.get("area", "")))
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["task", "layer", "area", "severity", "defect",
                    "location", "reason", "fix"])
        for f in flagged:
            w.writerow([f["task"], layer_of(f), f.get("area", ""), f["severity"],
                        f.get("title", ""), f.get("location", ""),
                        _flat(f.get("detail", "")), _flat(f.get("fix", ""))])
    return len(flagged)


def write_details(tasks, rows, path):
    lines = ["# SWE-Bench Ext QC — Detailed Findings", ""]
    for task in sorted(tasks):
        r = rows[task]
        lines.append(f"## Task: {task}")
        lines.append(f"**Overall: {r['overall']}**")
        lines.append("")
        for col in COLS:
            fs = [f for f in tasks[task].get(col, []) if f["severity"] != PASS]
            verdict = r[col] or "—"
            if not fs:
                if r[col]:
                    lines.append(f"### {col} — {verdict}")
                    lines.append("No issues.")
                    lines.append("")
                continue
            lines.append(f"### {col} — {verdict}")
            for f in fs:
                loc = f" (`{f['location']}`)" if f.get("location") else ""
                lines.append(f"- **[{f['severity']}] {f.get('title','')}**{loc}: "
                             f"{f.get('detail','')}")
                if f.get("fix"):
                    lines.append(f"  - _Fix:_ {f['fix']}")
            lines.append("")
        lines.append("---")
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def write_distribution(findings, rows, path):
    n_tasks = len(rows)
    fail_tasks = [t for t, r in rows.items() if r["overall"] == FAIL]
    warn_tasks = [t for t, r in rows.items() if r["overall"] == WARN]
    pass_tasks = [t for t, r in rows.items() if r["overall"] == PASS]

    by_area = Counter()
    by_class = Counter()
    warn_by_area = Counter()
    for f in findings:
        if f["severity"] == FAIL:
            by_area[f["area"]] += 1
            by_class[(f["area"], f.get("title", ""))] += 1
        elif f["severity"] == WARN:
            warn_by_area[f["area"]] += 1

    L = ["# SWE-Bench Ext QC — Defect Distribution", ""]
    L.append(f"- **Tasks reviewed:** {n_tasks}")
    if n_tasks:
        L.append(f"- **FAIL (defective):** {len(fail_tasks)} "
                 f"({100*len(fail_tasks)/n_tasks:.1f}%)")
        L.append(f"- **WARN (minor):** {len(warn_tasks)} "
                 f"({100*len(warn_tasks)/n_tasks:.1f}%)")
        L.append(f"- **PASS (clean):** {len(pass_tasks)} "
                 f"({100*len(pass_tasks)/n_tasks:.1f}%)")
    L.append("")

    L.append("## FAIL-level defects by area")
    L.append("")
    L.append("| Area | Defects |")
    L.append("|---|---|")
    for area in AREAS:
        if by_area.get(area):
            L.append(f"| {area} | {by_area[area]} |")
    L.append(f"| **total** | **{sum(by_area.values())}** |")
    L.append("")

    L.append("## FAIL-level defects by class (area / title)")
    L.append("")
    L.append("| Area | Defect class | Count |")
    L.append("|---|---|---|")
    for (area, title), cnt in by_class.most_common():
        L.append(f"| {area} | {title} | {cnt} |")
    L.append("")

    L.append("## WARN-level findings by area")
    L.append("")
    L.append("| Area | Warnings |")
    L.append("|---|---|")
    for area in AREAS:
        if warn_by_area.get(area):
            L.append(f"| {area} | {warn_by_area[area]} |")
    L.append("")

    if fail_tasks:
        L.append("## Defective tasks")
        L.append("")
        for t in sorted(fail_tasks):
            L.append(f"- `{t}` — {rows[t]['critical_issues']}")
        L.append("")

    with open(path, "w") as f:
        f.write("\n".join(L))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("findings_dir")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()
    out_dir = args.out_dir or args.findings_dir
    os.makedirs(out_dir, exist_ok=True)

    findings = load_findings(args.findings_dir)
    findings, n_refuted = reconcile(findings)
    if n_refuted:
        print(f"  reconcile: dropped {n_refuted} static finding(s) refuted by review sub-agents")
    tasks = per_task(findings)
    rows = verdicts(tasks)

    write_csv(rows, os.path.join(out_dir, "review-ssot.csv"))
    write_details(tasks, rows, os.path.join(out_dir, "review-ssot.md"))
    write_distribution(findings, rows, os.path.join(out_dir, "defect-distribution.md"))
    n_defects = write_defects_csv(findings, os.path.join(out_dir, "defects.csv"))

    n_fail = sum(1 for r in rows.values() if r["overall"] == FAIL)
    print(f"[aggregate] {len(rows)} tasks, {n_fail} FAIL, {n_defects} flagged findings -> "
          f"{out_dir}/review-ssot.csv, review-ssot.md, defect-distribution.md, defects.csv")


if __name__ == "__main__":
    main()

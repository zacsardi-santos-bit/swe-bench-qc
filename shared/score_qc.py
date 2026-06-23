#!/usr/bin/env python3
"""Score QC predictions against manual labels — precision / recall / confusion.

Reused from Moodi's terminal-bench-qc/shared/score_qc.py (generic; unchanged).
Drives the loop: "iterate until 100% recall, then measure precision."

Inputs:
  review-ssot.csv   QC output (from aggregate.py). A task is PREDICTED defective
                    if overall == FAIL (use --include-warn to also count WARN).
  labels.csv        ground truth. Columns: task,is_defect[,notes]
                    is_defect truthy = {1,yes,true,y,fail,defect}.

Usage:
    python score_qc.py review-ssot.csv labels.csv [--include-warn]
"""
import argparse
import csv
import sys

TRUTHY = {"1", "yes", "true", "y", "fail", "defect", "defective"}


def read_csv(path):
    with open(path) as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows
            if r.get("task") and not str(r["task"]).lstrip().startswith("#")]


def truthy(v):
    return str(v).strip().lower() in TRUTHY


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ssot_csv")
    ap.add_argument("labels_csv")
    ap.add_argument("--include-warn", action="store_true",
                    help="count WARN as a predicted defect (default: FAIL only)")
    args = ap.parse_args()

    pred_rows = read_csv(args.ssot_csv)
    pos_verdicts = {"FAIL"} | ({"WARN"} if args.include_warn else set())
    predicted = {r["task"]: (r.get("overall", "") in pos_verdicts) for r in pred_rows}

    labels = read_csv(args.labels_csv)
    truth = {r["task"]: truthy(r.get("is_defect", "")) for r in labels}

    common = sorted(set(predicted) & set(truth))
    missing_pred = sorted(set(truth) - set(predicted))
    missing_truth = sorted(set(predicted) - set(truth))
    if missing_pred:
        print(f"! {len(missing_pred)} labeled tasks have no QC prediction: "
              f"{missing_pred[:5]}{'...' if len(missing_pred) > 5 else ''}")
    if missing_truth:
        print(f"! {len(missing_truth)} predicted tasks have no label "
              f"(excluded from scoring)")
    if not common:
        sys.exit("No overlapping tasks between predictions and labels.")

    tp = fp = tn = fn = 0
    fp_tasks, fn_tasks = [], []
    for t in common:
        p, g = predicted[t], truth[t]
        if p and g:
            tp += 1
        elif p and not g:
            fp += 1
            fp_tasks.append(t)
        elif not p and g:
            fn += 1
            fn_tasks.append(t)
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    f1 = (2 * precision * recall / (precision + recall)
          if precision == precision and recall == recall and (precision + recall) else float("nan"))

    print("\n=== QC scoring ===")
    print(f"scored tasks:        {len(common)}")
    print(f"predicted-defect:    {'overall in ' + str(sorted(pos_verdicts))}")
    print(f"  TP={tp}  FP={fp}  FN={fn}  TN={tn}")
    print(f"  precision = {precision:.3f}" if precision == precision else "  precision = n/a")
    print(f"  recall    = {recall:.3f}" if recall == recall else "  recall    = n/a")
    print(f"  f1        = {f1:.3f}" if f1 == f1 else "  f1        = n/a")
    if fn_tasks:
        print(f"\nFALSE NEGATIVES (real defects QC missed — fix these first):")
        for t in fn_tasks:
            print(f"  - {t}")
    if fp_tasks:
        print(f"\nFALSE POSITIVES (QC over-flagged — hurts precision):")
        for t in fp_tasks:
            print(f"  - {t}")


if __name__ == "__main__":
    main()

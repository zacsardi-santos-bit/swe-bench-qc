#!/usr/bin/env python3
"""
Trajectory-QC triage — deterministic candidate selection.

Philosophy: triage is DETERMINISTIC and only ever emits WARN candidates. It does
NOT decide DEFECT/CLEAN — it selects *where the LLM judge should look* using
cross-model / cross-run disagreement, an objective signal that does not depend on
single-LLM patch opinion (which is our ground-truth weakness). The judge (the
skill rubric) then confirms each candidate -> FAIL or CLEAN, and genuine
apply-collisions get settled by the deterministic `git apply --check` discriminator.

Usage:
    python3 triage.py <dir-of-trajectory-json>   [--threshold 0.8] [--min-attempts 2]
Emits a ranked candidate table (stdout) + candidates.json next to the input.
"""
import json, glob, os, sys, argparse, collections, re

UNGRADED_RE = re.compile(r"image build|snapshot bundle|build failed|timed? ?out|timeout|crash|oom|out of memory", re.I)


def norm_model(m):
    if not m:
        return "unknown"
    m = m.lower()
    if "kimi" in m:
        return "kimi"
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    return m.split("/")[-1]


def is_ungraded(o):
    """Our UNGRADED-first gate: the tests never actually ran -> score is meaningless."""
    if not isinstance(o, dict):
        return True, "no output"
    status = (o.get("eval_status") or "").lower()
    tt = o.get("tests_total")
    err = o.get("error_message") or ""
    if tt in (None, 0):
        return True, f"tests_total={tt}"
    if UNGRADED_RE.search(err):
        return True, f"error_message ~ '{err[:40]}'"
    if status in ("failed", "error", "errored") and not tt:
        return True, f"eval_status={status}, no tests"
    ec = o.get("exit_code")
    if ec not in (0, None) and not tt:
        return True, f"exit_code={ec}, no tests"
    return False, ""


def nf_fraction(o):
    """Fraction of F2P/P2P keys that are NOT_FOUND — flags the apply-collision family."""
    tm = o.get("test_summary_metadata") or {}
    keys = {}
    keys.update(tm.get("fail_to_pass_results") or {})
    keys.update(tm.get("pass_to_pass_results") or {})
    if not keys:
        return 0.0, 0
    nf = sum(1 for v in keys.values() if str(v).upper() == "NOT_FOUND")
    return nf / len(keys), nf


def load(dirpath):
    runs = []
    for f in glob.glob(os.path.join(dirpath, "*.json")):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        if not isinstance(d, dict) or "task_name" not in d:
            continue
        o = d.get("trajectory_output") or {}
        model = norm_model(o.get("model") or d.get("orchestrator_llm_model"))
        ung, why = is_ungraded(o)
        nf_frac, nf_n = nf_fraction(o)
        runs.append({
            "task": d.get("task_name"),
            "traj": d.get("trajectory_id"),
            "model": model,
            "score": o.get("score"),
            "passed": (o.get("score") or 0) >= 1,
            "ungraded": ung, "ungraded_why": why,
            "nf_frac": nf_frac, "nf_n": nf_n,
            "test_statuses": o.get("test_statuses") or {},
        })
    return runs


def triage(runs, threshold, min_attempts):
    by_task = collections.defaultdict(list)
    for r in runs:
        by_task[r["task"]].append(r)

    candidates = []
    for task, rs in by_task.items():
        graded = [r for r in rs if not r["ungraded"]]
        ungraded = [r for r in rs if r["ungraded"]]
        n = len(graded)
        if n == 0:
            candidates.append({"task": task, "signal": "all-ungraded", "severity": "WARN",
                               "detail": f"{len(ungraded)} runs, none graded", "rank": 1})
            continue
        passes = sum(1 for r in graded if r["passed"])
        models = collections.defaultdict(lambda: [0, 0])  # model -> [pass, fail]
        for r in graded:
            models[r["model"]][0 if r["passed"] else 1] += 1
        model_str = " ".join(f"{m}={p}P/{f}F" for m, (p, f) in sorted(models.items()))

        sigs = []
        all_fail = passes == 0 and n >= min_attempts
        # 1. split-score (Moody): some pass, some fail. cross-model = one model is
        #    unanimous-pass while another is unanimous-fail (strongest verifier signal).
        if 0 < passes < n and n >= min_attempts:
            mv = [tuple(v) for v in models.values()]
            cross_model = len(models) >= 2 and any(p > 0 and f == 0 for p, f in mv) \
                                           and any(p == 0 and f > 0 for p, f in mv)
            sigs.append(("cross-model-split" if cross_model else "split-score", 0 if cross_model else 1))
        # 2. all-fail (Moody)
        if all_fail:
            sigs.append(("all-fail", 2))
        # 3. NOT_FOUND-heavy (ours): the ambiguous apply-collision/divergence family
        nf = [r for r in graded if not r["passed"] and r["nf_frac"] >= 0.5]
        if nf:
            sigs.append((f"not-found-heavy({max(r['nf_frac'] for r in nf):.0%})", 3))

        for sig, rank in sigs:
            candidates.append({
                "task": task, "signal": sig, "severity": "WARN",
                "detail": model_str, "rank": rank,
                "trajs": [r["traj"] for r in graded],
            })

    # 4. high-fail / suspect-test (Moody scan_verifiers): a single check failing
    #    >= threshold across >=2 models -> overfit/env-coupled assertion. Suppressed
    #    on all-fail tasks (already flagged; there it's just "nobody solved it").
    #    Deduped to one row per task: count of suspect checks + worst example.
    all_fail_tasks = {c["task"] for c in candidates if c["signal"] == "all-fail"}
    check_fail = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
    check_models = collections.defaultdict(lambda: collections.defaultdict(set))
    for r in runs:
        if r["ungraded"]:
            continue
        for check, status in r["test_statuses"].items():
            tot = check_fail[r["task"]][check]
            tot[1] += 1
            if str(status).lower() not in ("pass", "passed"):
                tot[0] += 1
                check_models[r["task"]][check].add(r["model"])
    for task, checks in check_fail.items():
        if task in all_fail_tasks:
            continue
        suspect = [(check, fail, tot) for check, (fail, tot) in checks.items()
                   if tot >= min_attempts and fail / tot >= threshold and len(check_models[task][check]) >= 2]
        if suspect:
            worst = max(suspect, key=lambda s: s[1] / s[2])
            candidates.append({"task": task, "signal": "suspect-test", "severity": "WARN",
                               "detail": f"{len(suspect)} check(s) fail >={threshold:.0%} across >=2 models; "
                                         f"worst '{worst[0][:40]}' {worst[1]}/{worst[2]}",
                               "rank": 1})

    candidates.sort(key=lambda c: (c["rank"], c["task"]))
    return candidates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--threshold", type=float, default=0.8)
    ap.add_argument("--min-attempts", type=int, default=2)
    args = ap.parse_args()

    runs = load(args.dir)
    cands = triage(runs, args.threshold, args.min_attempts)

    tasks = {r["task"] for r in runs}
    ungraded_tasks = {r["task"] for r in runs if r["ungraded"]}
    print(f"\nLoaded {len(runs)} runs over {len(tasks)} tasks "
          f"({len(ungraded_tasks)} tasks have >=1 UNGRADED run).\n")
    print(f"{'RANK':<5}{'SIGNAL':<22}{'TASK':<45}DETAIL")
    print("-" * 100)
    for c in cands:
        print(f"{c['rank']:<5}{c['signal']:<22}{c['task'][:43]:<45}{c['detail']}")
    if not cands:
        print("(no candidates — every task is unanimous-pass or all-ungraded)")

    out = os.path.join(args.dir, "candidates.json")
    json.dump(cands, open(out, "w"), indent=1)
    print(f"\n{len(cands)} WARN candidates -> {out}")
    print("Next: LLM judge reads diffs for each candidate -> FAIL/CLEAN (skill rubric); "
          "settle collisions with discriminate.sh.")


if __name__ == "__main__":
    main()

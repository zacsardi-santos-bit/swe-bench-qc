#!/usr/bin/env python3
"""Behavioral gate (OPT-IN) for SWE-Bench Ext — the only QC layer that RUNS the task.

Catches the dominant defect class (the MAI
oracle table, GDM oracle, Reflection null-agent): "the verifier doesn't actually
require the fix" and "the reference doesn't pass its own tests."

Two trials per task (the SWE-bench analog of Moodi's no-op / oracle):
  - no-op  : build the image, run the task's test command on the UNTOUCHED repo.
             Must FAIL (FAIL_TO_PASS should fail at base). If it PASSES ->
             `verifier-too-weak` (a no-op scores 1.0 — vacuous verifier).
  - oracle : fresh container, `git apply golden.patch` in /workspace/repo, then the
             test command. Must PASS. If it FAILS -> `wrong-PR` (the golden doesn't
             pass its own verifier — broken reference, or env/conversion defect).
A build failure is graded by cause: a real amd64 build failure -> `build-fails` (FAIL);
a timeout, a --native-arch failure, or an unpullable base image -> WARN (inconclusive,
not a defect). A clean run -> `behavioral-ok`.

EXPENSIVE + opt-in: by default it prints the plan and runs nothing. Pass --execute to
actually build + run. On Apple Silicon pass --native-arch. Targeted runs only
(--only or a sample), never a reflex over the whole set.



Usage:
    python3 check_behavioral.py <tasks-dir> --only flexcompute-tidy3d-2401          # plan only
    python3 check_behavioral.py <tasks-dir> --only flexcompute-tidy3d-2401 --execute --native-arch
    python3 check_behavioral.py eval/tasks --execute --native-arch --workers 2 --out qc_out/findings_behavioral.json
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "..", "shared")))
from common import FAIL, PASS, WARN, finding, emit, discover_tasks, task_paths, read_text  # noqa: E402


def _docker_ok():
    return shutil.which("docker") is not None


def _run(cmd, timeout):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except Exception as e:
        return 1, str(e)


def _verifier_passed(rc, out):
    """Pass = exit 0 and no test-failure signature (pytest / go test / junit)."""
    if rc != 0:
        return False
    if re.search(r"\b\d+ failed\b|FAILED|AssertionError|\bFAIL\b|Error:", out):
        return False
    return True


def _test_command(root):
    try:
        meta = json.loads(read_text(task_paths(root)["test_metadata.json"]))
        return meta.get("test_command") or "bash /task/run_test.sh"
    except Exception:
        return "bash /task/run_test.sh"


def _meta(root):
    try:
        return json.loads(read_text(task_paths(root)["test_metadata.json"]))
    except Exception:
        return {}


def _apply(patch):
    """Apply a patch in /workspace/repo; repo/ is a plain tree, so fall back to `patch`."""
    return (f"(git apply -p1 /task/{patch} 2>/dev/null || "
            f"patch -p1 -f < /task/{patch} 2>/dev/null || true)")


def _trial_script(mode, test_cmd):
    """Shell run inside the container. Repo is at /workspace/repo (per the Dockerfile).

    BOTH trials apply test.patch — the F2P/P2P tests live there, not in the repo, so
    without it the test command can't find them (pytest rc=4 / go build error). The
    oracle trial additionally applies golden.patch (the reference fix).
      no-op : test.patch only            -> F2P should FAIL at base
      oracle: test.patch + golden.patch  -> F2P should PASS
    """
    steps = ["cd /workspace/repo", _apply("test.patch")]
    if mode == "oracle":
        steps.append(_apply("golden.patch"))
    steps.append(test_cmd)
    return " ; ".join(steps)


def _tag(name):
    # strip trailing '-' after truncation: a tag ending in '-' is an invalid docker ref
    return ("qcbeh-swe-" + re.sub(r"[^a-z0-9]+", "-", name.lower())[:40]).rstrip("-")


def plan_task(name, root, args):
    tc = _test_command(root)
    tag = _tag(name)
    cmds = [f"docker build -t {tag} {root}"]
    for m in ("no-op", "oracle"):
        cmds.append(f"docker run --rm -v {root}:/task:ro {tag} bash -c '{_trial_script(m, tc)}'")
    return [finding(name, "behavioral", PASS, "behavioral-plan",
                    detail=" || ".join(cmds), location=root, layer="behavioral")]


_IMG_UNAVAIL_RE = re.compile(
    r"pull access denied|insufficient_scope|authorization failed|"
    r"manifest (for .*)?unknown|not found.*(image|repository)", re.I)


def _build_failure(name, rc, log, native_arch, build_timeout):
    """Grade a docker-build failure by CAUSE (pure + unit-testable). Only a real
    amd64 build failure with enough time is a task defect. Timeouts, --native-arch
    failures, and unpullable base images (private registry / not authenticated) are
    INCONCLUSIVE -> WARN, so they never pollute the defect count. (Moodi's triage.)"""
    if rc == 124:
        return finding(name, "behavioral", WARN, "build-timeout",
                       detail=f"docker build exceeded {build_timeout}s — inconclusive "
                              f"(raise --build-timeout / lower --workers): {log[-160:]}",
                       location="Dockerfile", layer="behavioral",
                       fix="Re-run with a higher --build-timeout and/or fewer --workers.")
    if _IMG_UNAVAIL_RE.search(log):
        return finding(name, "behavioral", WARN, "build-image-unavailable",
                       detail=f"base image couldn't be pulled (private registry / not "
                              f"authenticated) — inconclusive, not a task defect: {log[-160:]}",
                       location="Dockerfile", layer="behavioral",
                       fix="Authenticate to the image registry (or build on delivery infra), then re-run.")
    if native_arch:
        return finding(name, "behavioral", WARN, "build-untested-native-arch",
                       detail=f"image failed to build under --native-arch (task targets amd64; "
                              f"likely an arch/availability artifact, not a defect): {log[-160:]}",
                       location="Dockerfile", layer="behavioral",
                       fix="Confirm on native amd64 (delivery infra) before treating as a defect.")
    return finding(name, "behavioral", FAIL, "build-fails",
                   detail=f"`docker build` failed: {log[-300:]}",
                   location="Dockerfile", layer="behavioral",
                   fix="Fix the Dockerfile so the task image builds.")


def run_task(name, root, args):
    if not os.path.isfile(task_paths(root)["Dockerfile"]):
        return [finding(name, "behavioral", WARN, "behavioral-skipped",
                        detail="no Dockerfile — cannot run.", location=root, layer="behavioral")]
    tag = _tag(name)
    tc = _test_command(root)

    build = ["docker", "build", "-q", "-t", tag, root]
    if args.native_arch:
        df = re.sub(r"--platform=\S+\s*", "", read_text(task_paths(root)["Dockerfile"]))
        tmp = os.path.join(tempfile.gettempdir(), tag + ".Dockerfile")
        open(tmp, "w").write(df)
        build = ["docker", "build", "-q", "-f", tmp, "-t", tag, root]
    rc, log = _run(build, args.build_timeout)
    if rc != 0:
        return [_build_failure(name, rc, log, args.native_arch, args.build_timeout)]

    def trial(mode):
        # bash -c (NOT -lc): a login shell sources /etc/profile and overwrites PATH,
        # dropping the image's /usr/local/go/bin etc. -> "go: command not found".
        return _run(["docker", "run", "--rm", "-v", f"{root}:/task:ro", tag,
                     "bash", "-c", _trial_script(mode, tc)], args.timeout)

    out = []
    nrc, nlog = trial("no-op")
    if _verifier_passed(nrc, nlog):
        out.append(finding(name, "behavioral", FAIL, "verifier-too-weak",
                           detail="test command PASSES on the untouched repo (no-op scores 1.0) "
                                  "— the verifier doesn't require the fix.",
                           location="test.patch", layer="behavioral",
                           fix="Ensure FAIL_TO_PASS actually fails at base; tighten the tests."))
    orc, olog = trial("oracle")
    if not _verifier_passed(orc, olog):
        out.append(finding(name, "behavioral", FAIL, "wrong-PR",
                           detail=f"test command FAILS after applying golden.patch (rc={orc}) — "
                                  f"the reference doesn't pass its own verifier: {olog[-200:]}",
                           location="golden.patch", layer="behavioral",
                           fix="Fix golden.patch or the env so the oracle scores 1.0, or re-extract the PR."))
    _run(["docker", "rmi", "-f", tag], 60)
    if not out:
        out.append(finding(name, "behavioral", PASS, "behavioral-ok",
                           detail="oracle scored 1.0 and no-op scored 0.", layer="behavioral"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks")
    ap.add_argument("--only", default="", help="comma-separated task names to run")
    ap.add_argument("--execute", action="store_true",
                    help="ACTUALLY build+run in Docker (expensive). Without it, prints the plan only.")
    ap.add_argument("--native-arch", action="store_true",
                    help="strip the FROM --platform pin and build for the host arch (use on Apple Silicon)")
    ap.add_argument("--timeout", type=int, default=600, help="per-trial cap (s)")
    ap.add_argument("--build-timeout", type=int, default=1200, help="docker build cap (s)")
    ap.add_argument("--workers", type=int, default=1, help="tasks to run concurrently (builds are heavy; 1-2 on a laptop)")
    ap.add_argument("--no-resume", dest="resume", action="store_false", help="re-run all, ignoring --out")
    ap.add_argument("--out", default="findings_behavioral.json")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the interactive confirm before --execute")
    args = ap.parse_args()

    only = {s for s in args.only.split(",") if s}
    tasks = [(n, r) for n, r in discover_tasks(args.tasks) if not only or n in only]

    if not args.execute:
        print(f"[behavioral] PLAN ONLY — {len(tasks)} task(s). This gate RUNS the task in Docker "
              f"(expensive); nothing has run. Re-run with --execute to actually run.")
        findings = []
        for n, r in tasks:
            findings.extend(plan_task(n, r, args))
        emit(findings, args.out)
        return

    if not args.yes and sys.stdin.isatty():
        est = max(1, len(tasks) * 4 // max(args.workers, 1))
        resp = input(f"[behavioral] About to BUILD + RUN {len(tasks)} task(s) in Docker "
                     f"(executes code; ~{est}+ min). Proceed? [y/N] ")
        if resp.strip().lower() not in ("y", "yes"):
            print("Aborted — nothing run. (Drop --execute for the plan.)")
            return
    if not _docker_ok():
        raise SystemExit("docker not found — start Docker Desktop, or drop --execute for the plan.")

    findings, done = [], set()
    if args.resume and os.path.isfile(args.out):
        try:
            findings = [f for f in json.load(open(args.out)) if f.get("task")]
            done = {f["task"] for f in findings}
        except Exception:
            findings, done = [], set()
    todo = [(n, r) for n, r in tasks if n not in done]
    print(f"[behavioral] EXECUTING {len(todo)} task(s) with {args.workers} worker(s)…"
          + (f" ({len(done)} already done, skipping)" if done else ""))

    lock = threading.Lock()
    counter = {"n": len(done)}

    def work(name, root):
        try:
            res = run_task(name, root, args)
        except Exception as e:
            res = [finding(name, "behavioral", WARN, "behavioral-error",
                           detail=f"runner error: {str(e)[:200]}", location=root, layer="behavioral")]
        with lock:
            findings.extend(res)
            emit(findings, args.out)
            counter["n"] += 1
            print(f"  [{counter['n']}/{len(tasks)}] {name}: {[f['title'] for f in res]}")

    if args.workers > 1:
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(cf.as_completed([ex.submit(work, n, r) for n, r in todo]))
    else:
        for n, r in todo:
            work(n, r)
    fails = sum(1 for f in findings if f["severity"] == FAIL)
    print(f"[behavioral] {len(findings)} findings, {fails} FAIL -> {args.out}")


if __name__ == "__main__":
    main()

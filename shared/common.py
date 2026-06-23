#!/usr/bin/env python3
"""Shared foundation for the swe-bench-ext-qc detectors.

Adapted from Moodi's terminal-bench-qc/shared/common.py — the finding schema,
severity model, and aggregation contract are reused verbatim so every layer's
findings compose through aggregate.py / gate.py / score_qc.py. The only
SWE-Bench-Ext-specific part is task discovery + the standard file paths (our tasks
use test_metadata.json + golden.patch + test.patch + Dockerfile + run_test.sh,
not Terminal-Bench's task.toml / solve.sh layout).

Findings schema (one dict per finding; a JSON array per gate):
  {
    "task":     "<task-name>",
    "area":     "structure|metadata|dockerfile|instructions|tests|solution|anti_cheat|behavioral|dataset",
    "severity": "PASS|WARN|FAIL",
    "title":    "<short stable label, used for distribution counts>",
    "location": "<file[:line] or ''>",
    "detail":   "<what is wrong>",
    "fix":      "<how to fix>",
    "layer":    "<optional: static|semantic|trajectory|behavioral — cross-layer provenance>"
  }
"""
import json
import os

# ---------------------------------------------------------------- severity ---
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
SEV_RANK = {PASS: 0, WARN: 1, FAIL: 2}
AREAS = ["structure", "metadata", "dockerfile", "instructions",
         "tests", "solution", "anti_cheat", "behavioral", "dataset"]


def worst(severities):
    """Return the highest-rank severity in an iterable (PASS if empty)."""
    out = PASS
    for s in severities:
        if SEV_RANK.get(s, 0) > SEV_RANK[out]:
            out = s
    return out


def finding(task, area, severity, title, detail="", location="", fix="", layer=""):
    f = {
        "task": task, "area": area, "severity": severity, "title": title,
        "location": location, "detail": detail, "fix": fix,
    }
    if layer:
        f["layer"] = layer
    return f


# Which QC layer an area belongs to (cross-layer provenance + the gate).
# A finding may override via an explicit "layer" — e.g. trajectory (Layer 2)
# findings use area="tests" but layer="trajectory".
AREA_LAYER = {
    "structure": "static", "metadata": "static", "dockerfile": "static",
    "instructions": "static", "anti_cheat": "static", "dataset": "static",
    "tests": "semantic", "solution": "semantic",
    "behavioral": "behavioral",
}


def layer_of(f):
    """The QC layer that produced a finding: explicit `layer`, else mapped from area."""
    return f.get("layer") or AREA_LAYER.get(f.get("area", ""), "unknown")


def emit(findings, out_path):
    """Write a findings list to out_path as a JSON array; return the count."""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(findings, f, indent=2)
    return len(findings)


# -------------------------------------------------------------------- io -----
def read_text(path):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except Exception:
        return ""


# ----------------------------------------------- task discovery (SWE-Bench Ext)
def looks_like_task_dir(p):
    """A SWE-Bench-Ext task dir directly contains test_metadata.json or golden.patch."""
    return (os.path.isfile(os.path.join(p, "test_metadata.json"))
            or os.path.isfile(os.path.join(p, "golden.patch")))


def resolve_task_dir(p):
    """A task dir may be wrapped one level deep (e.g. task1/<task-name>/...)."""
    if looks_like_task_dir(p):
        return p
    if os.path.isdir(p):
        subdirs = sorted(d for d in (os.path.join(p, x) for x in os.listdir(p))
                         if os.path.isdir(d))
        if len(subdirs) == 1 and looks_like_task_dir(subdirs[0]):
            return subdirs[0]
    return p


def discover_tasks(path):
    """Yield (task_name, task_root) for every SWE-Bench-Ext task under `path`.

    A task root is any directory directly containing test_metadata.json or
    golden.patch. Works whether `path` is one task or a folder of many. Deduped
    by task name (first one wins).
    """
    path = os.path.abspath(path)
    if looks_like_task_dir(path):
        return [(os.path.basename(path), path)]
    seen, out = set(), []
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "node_modules", "__pycache__", "tasks_cache")]
        if "test_metadata.json" in filenames or "golden.patch" in filenames:
            name = os.path.basename(dirpath)
            if name not in seen:
                seen.add(name)
                out.append((name, dirpath))
            dirnames[:] = []  # don't descend below a task root
    return sorted(out)


def task_paths(root):
    """Standard SWE-Bench-Ext paths relative to a task root (exist or not)."""
    return {
        "problem_statement.md": os.path.join(root, "problem_statement.md"),
        "prompt_statement.md":  os.path.join(root, "prompt_statement.md"),
        "requirements.json":    os.path.join(root, "requirements.json"),
        "interface.md":         os.path.join(root, "interface.md"),
        "golden.patch":         os.path.join(root, "golden.patch"),
        "test.patch":           os.path.join(root, "test.patch"),
        "test_metadata.json":   os.path.join(root, "test_metadata.json"),
        "Dockerfile":           os.path.join(root, "Dockerfile"),
        "run_test.sh":          os.path.join(root, "run_test.sh"),
        "repo":                 os.path.join(root, "repo"),
    }


if __name__ == "__main__":
    print("common.py (swe-bench-ext) OK — areas:", AREAS)

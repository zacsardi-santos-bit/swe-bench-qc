#!/usr/bin/env python3
"""Import golden (true-pass) tasks from the public HuggingFace SWE-Bench Pro set
into the eval/tasks/<instance_id>/ layout the QC pipeline expects, labeled
is_defect=0. These are the precision side of the eval set: confirmed-clean tasks
the QC skill must NOT flag.

Pulls rows from the HF datasets-server (no auth, no `datasets` lib needed) and
materializes each row into the SWE-Bench-Ext file layout. The public set ships a
prebuilt image tag (dockerhub_tag) rather than a Dockerfile, so we synthesize a
minimal Dockerfile + run_test.sh from the row so the task is structurally complete
for the static layer. (Real Studio tasks have full Dockerfiles; for precision
testing of the static+semantic checks this stand-in is fine — noted in labels.)

Usage:
    python3 import_hf_goldens.py --n 10 --out ../../../eval/tasks
    python3 import_hf_goldens.py --n 10 --offset 0 --labels ../../../eval/labels_real.csv
"""
import argparse
import json
import os
import urllib.parse
import urllib.request

DATASET = "ScaleAI/SWE-bench_Pro"
BASE = "https://datasets-server.huggingface.co/rows"


def fetch_rows(dataset, offset, length):
    qs = urllib.parse.urlencode({"dataset": dataset, "config": "default",
                                 "split": "test", "offset": offset, "length": length})
    req = urllib.request.Request(f"{BASE}?{qs}", headers={"User-Agent": "swe-qc-importer"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["rows"]


def as_list(v):
    """fail_to_pass / pass_to_pass arrive as a string: JSON list or newline/space list."""
    if isinstance(v, list):
        return v
    s = (v or "").strip()
    if not s:
        return []
    try:
        out = json.loads(s)
        return out if isinstance(out, list) else [out]
    except json.JSONDecodeError:
        return [x for x in s.replace(",", "\n").split("\n") if x.strip()]


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text if text is not None else "")


def materialize(row, out_root):
    r = row["row"]
    iid = r.get("instance_id") or f"hf-{row.get('row_idx')}"
    d = os.path.join(out_root, iid)
    os.makedirs(os.path.join(d, "repo"), exist_ok=True)

    write(os.path.join(d, "problem_statement.md"), r.get("problem_statement", ""))
    write(os.path.join(d, "prompt_statement.md"), r.get("problem_statement", ""))
    write(os.path.join(d, "interface.md"), r.get("interface", "") or "(none)")
    # requirements as JSON if possible, else wrapped
    req = r.get("requirements", "") or ""
    try:
        json.loads(req); req_out = req
    except (json.JSONDecodeError, TypeError):
        req_out = json.dumps({"requirements": req}, indent=2)
    write(os.path.join(d, "requirements.json"), req_out)
    write(os.path.join(d, "golden.patch"), r.get("patch", ""))
    write(os.path.join(d, "test.patch"), r.get("test_patch", ""))

    f2p, p2p = as_list(r.get("fail_to_pass")), as_list(r.get("pass_to_pass"))
    base = r.get("base_commit", "") or ""
    tests = as_list(r.get("selected_test_files_to_run"))
    meta = {
        "test_command": "bash run_test.sh",
        "FAIL_TO_PASS": f2p, "PASS_TO_PASS": p2p,
        "base_commit": base, "language": r.get("repo_language", ""),
        "test_files": tests, "num_test_files": len(tests),
        "repo": r.get("repo", ""), "dockerhub_tag": r.get("dockerhub_tag", ""),
    }
    write(os.path.join(d, "test_metadata.json"), json.dumps(meta, indent=2))

    # synthesize a complete-enough Dockerfile + run_test.sh (LF endings, no leak)
    img = r.get("dockerhub_tag") or "python:3.11"
    setup = r.get("before_repo_set_cmd", "") or ""
    write(os.path.join(d, "Dockerfile"),
          f"FROM {img}\n# base_commit {base}\nCOPY requirements.json /work/\n"
          f"COPY interface.md /work/\nRUN git checkout {base} || true\n")
    write(os.path.join(d, "run_test.sh"),
          "#!/bin/bash\nset -euo pipefail\n" + (setup + "\n" if setup else "") +
          "# run the FAIL_TO_PASS / PASS_TO_PASS tests for this task\n")
    return iid


def update_labels(labels_path, ids):
    if not labels_path:
        return
    existing = set()
    if os.path.isfile(labels_path):
        for line in open(labels_path):
            tok = line.split(",", 1)[0].strip()
            if tok and not tok.startswith("#"):
                existing.add(tok)
    new = [i for i in ids if i not in existing]
    if not new:
        return
    header_needed = not os.path.isfile(labels_path)
    with open(labels_path, "a") as f:
        if header_needed:
            f.write("task,is_defect,catch_layer,source,notes\n")
        for i in new:
            f.write(f"{i},0,none,huggingface,confirmed golden (public SWE-Bench Pro)\n")
    print(f"[labels] added {len(new)} golden rows -> {labels_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "..", "..", "eval", "tasks"))
    ap.add_argument("--labels", default=os.path.join(os.path.dirname(__file__), "..", "..", "..", "eval", "labels_real.csv"))
    ap.add_argument("--dataset", default=DATASET)
    args = ap.parse_args()
    out_root = os.path.abspath(args.out)
    os.makedirs(out_root, exist_ok=True)

    ids, got, offset = [], 0, args.offset
    while got < args.n:
        batch = fetch_rows(args.dataset, offset, min(20, args.n - got))
        if not batch:
            break
        for row in batch:
            ids.append(materialize(row, out_root))
            got += 1
        offset += len(batch)
    for i in ids:
        print(f"  imported {i}")
    print(f"[hf] imported {len(ids)} golden task(s) -> {out_root}")
    update_labels(os.path.abspath(args.labels), ids)


if __name__ == "__main__":
    main()

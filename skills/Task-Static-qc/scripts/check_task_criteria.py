#!/usr/bin/env python3
"""
check_task_criteria.py — deterministic version of Step 1 ("Check if Task Criteria
Present") of the SWE-Bench-Ext QC procedure.

The QC skill's Step 1 is the one step that is pure gather-and-verify rather than
LLM judgment: it confirms every field/file a task *should* have is present and
well-formed before the LLM-judge rubric (Appendix A / A2) runs. This script makes
that check deterministic, fast, and CI-able.

It does NOT make any QC verdict (PASS_CLEAN / REWARD_HACKABLE / ...): that is still
LLM judgment. It answers "does this task have the inputs the rubric needs, are they
internally consistent, and is the environment free of the deterministic defects clients
have flagged?"

This is the L1 (static lint) layer of the QC spec. In addition to the original
completeness/consistency checks it now also runs env-hygiene checks derived directly
from swe-bench-ext-client-feedback.md:
  - CRLF/`\r` line endings in run_test.sh / solve.sh / test.sh / Dockerfile (RW Evals #1)
  - answer leak: Dockerfile COPY/ADD of golden.patch / test.patch / pr.patch / solution /
    rubric or any test file into the agent image (RW Evals #6)
  - mount lint: interface.md / requirements.json present but not COPY'd in (RW Evals #7)
  - dependency hygiene: `@latest`, or install/fetch at verify time in run_test.sh (MAI drift)
  - service init: `systemctl enable` with nothing that starts it (RW Evals #25)
  - untracked test files: a NEW test file added in test.patch that no FAIL_TO_PASS/
    PASS_TO_PASS entry references — it runs but never counts, so a no-op can break it and
    still score 1.0 (Alibaba Concern 2: apollographql-federation-278 golden=1.0 with 6/151
    failing in the untracked file)
It also runs delivery-hygiene checks (metadata fidelity / packaging readability) derived
from client feedback on fingerprintjs / atdatabases / ibm-fhir:
  - test_framework value vs test_command / language (e.g. "pytest" on a karma/TS task)
  - leftover automation/pipeline tags in shipped files (e.g. `# MAI-10K-OTS`, `FIX2`)
  - anonymized FAIL_TO_PASS names (`test_1` … `test_N`) untraceable to requirements
These are WARN (the task still builds/passes/doesn't leak) and FAIL only under --strict.
It does NOT cover the oracle/no-op gate (running golden=1.0 / no-op=0.0) — that requires
executing the container and is a separate layer.

Run as a CLI:
    python3 check_task_criteria.py PATH [PATH ...]
    python3 check_task_criteria.py --batch TASKS_DIR        # check every subdir
    python3 check_task_criteria.py PATH --json              # machine-readable
    python3 check_task_criteria.py PATH --strict            # warnings -> failure

A PATH is a single task directory (the dir that directly contains
test_metadata.json, golden.patch, etc.). Exit code is non-zero if any task FAILs
(or, under --strict, if any task has warnings).

Or import and call programmatically (no subprocess, no stdout parsing):
    from check_task_criteria import check, check_task
    result = check("path/to/task")            # -> plain dict (JSON-serializable)
    if result["verdict"] == "FAIL":
        ...
    report = check_task(Path("path/to/task")) # -> TaskReport (rich object)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# What a task should have. Mirrors the real archive layout the QC skill reads:
#   problem_statement.md + prompt_statement.md + requirements.json + interface.md
#   + golden.patch + test.patch + test_metadata.json + Dockerfile + run_test.sh
#   + rubric/ + repo/
# Severity ERROR -> task FAILs.  WARN -> note, but task can still PASS.
# --------------------------------------------------------------------------- #

# (filename, severity, role-comment)
REQUIRED_FILES = [
    ("problem_statement.md", "ERROR", "ISSUE — full issue context"),
    ("prompt_statement.md", "ERROR", "AGENT_PROMPT — exact text the agent sees"),
    ("golden.patch", "ERROR", "PATCH — reference solution"),
    ("test.patch", "ERROR", "TEST changes — verifier diff"),
    ("test_metadata.json", "ERROR", "TEST_LIST + HARNESS test_command"),
    ("run_test.sh", "ERROR", "HARNESS — what is executed"),
    ("Dockerfile", "ERROR", "HARNESS — build/clone/base_commit/env"),
]
RECOMMENDED_FILES = [
    ("requirements.json", "WARN", "Pro-style behavioral requirements"),
]
RECOMMENDED_DIRS = [
    ("repo", "WARN", "repo source at base_commit"),
]
# interface.md and rubric/ are optional (interface.md only on feature/refactor
# tasks); their absence is informational, not even a warning.

# test_metadata.json keys.
REQUIRED_META_KEYS = [
    ("test_command", "ERROR"),
    ("FAIL_TO_PASS", "ERROR"),
]
RECOMMENDED_META_KEYS = [
    # base_commit is optional: real SWE-Bench-Ext tasks ship the repo pre-checked-out
    # in the image (COPY repo/ ...), so many tasks legitimately omit it. WARN, not ERROR
    # (calibrated against real pulled tasks, which would otherwise all false-FAIL here).
    ("base_commit", "WARN"),
    ("test_framework", "WARN"),
    ("language", "WARN"),
    ("test_files", "WARN"),
    ("num_test_files", "WARN"),
    ("PASS_TO_PASS", "WARN"),
]

SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
TESTFILE_HINT = re.compile(r"(^|/)(tests?|testing)/|test_|_test\.|conftest", re.IGNORECASE)

# Source artifacts that must NEVER be COPY/ADD'd into the agent image (answer leak).
LEAK_FILE_HINT = re.compile(
    r"(golden\.patch|test\.patch|pr\.patch|(^|/)solution(/|\b)|(^|/)rubric(/|\b)|reward)",
    re.IGNORECASE,
)
# Contract files the agent legitimately needs; flag if present in task but not mounted in.
CONTRACT_FILES = ("interface.md", "requirements.json")
# Shell/build files whose CRLF line endings break execution in the container.
EXEC_FILES = ("run_test.sh", "solve.sh", "test.sh", "Dockerfile")

# Test framework -> (command-line signature tokens, language family). Used to flag
# a declared test_framework that contradicts how tests actually run or the task's
# language (client defect: test_framework="pytest" on a karma/TypeScript task).
FRAMEWORK_SIGNATURES = {
    "pytest":   (("pytest", "py.test"), "python"),
    "unittest": (("unittest",), "python"),
    "nose":     (("nosetests", "nose"), "python"),
    "jest":     (("jest",), "js"),
    "vitest":   (("vitest",), "js"),
    "mocha":    (("mocha",), "js"),
    "jasmine":  (("jasmine",), "js"),
    "karma":    (("karma",), "js"),
    "ava":      (("ava",), "js"),
    "maven":    (("mvn", "maven"), "java"),
    "gradle":   (("gradle",), "java"),
    "junit":    (("mvn", "gradle", "junit"), "java"),
    "go":       (("go test", "gotest"), "go"),
    "cargo":    (("cargo test", "cargo"), "rust"),
    "rspec":    (("rspec",), "ruby"),
    "minitest": (("minitest", "rake test"), "ruby"),
    "phpunit":  (("phpunit",), "php"),
    "ctest":    (("ctest",), "cpp"),
    "dotnet":   (("dotnet test", "dotnet"), "dotnet"),
}
# `language` value (as written in test_metadata.json) -> the family bucket above.
LANGUAGE_FAMILY = {
    "python": "python",
    "javascript": "js", "typescript": "js", "node": "js", "js": "js", "ts": "js",
    "java": "java", "kotlin": "java", "scala": "java",
    "go": "go", "golang": "go",
    "rust": "rust",
    "ruby": "ruby",
    "php": "php",
    "c++": "cpp", "cpp": "cpp", "c": "cpp",
    "c#": "dotnet", "csharp": "dotnet", ".net": "dotnet",
}

# Internal automation/pipeline identifiers that must be stripped before delivery.
# A client flagged leftover `# MAI-10K-OTS` / `# MAI-V5-FIX:FIX2` Dockerfile comments
# as automation residue (and a sign the image may not have been manually verified).
# Extend the alternation as new internal markers surface.
PIPELINE_TAG_RE = re.compile(r"\bMAI-[0-9A-Z][0-9A-Z\-]*|\bFIX\d+\b", re.IGNORECASE)
# Build/exec files (shipped to the client) we lint for stray pipeline tags.
TAG_SCAN_FILES = ("Dockerfile", "run_test.sh", "solve.sh", "test.sh")

# Anonymized / non-descriptive test identifiers (e.g. "test_1", "test_42") that
# cannot be traced back to a real test method or to requirements.json.
ANON_TEST_RE = re.compile(r"^test_\d+$", re.IGNORECASE)

# Frameworks whose runners emit ONLY a count summary ("Tests run: 594, Failures: 0"),
# not per-test names — so the parser legitimately SYNTHESIZES `test_1`…`test_N`.
# Alibaba confirmed these are NOT a defect (the same parser runs both the metadata-
# creation and the eval, so the synthetic ids match deterministically and grade
# correctly). Suppress the anonymized-name WARN for these; KEEP it for frameworks that
# do emit real per-test names (pytest, go test, jest, …) where `test_N` means a real
# name was discarded (the ibm-fhir readability defect).
COUNT_SUMMARY_FRAMEWORKS = {"maven", "gradle", "junit", "surefire", "ant", "mocha", "bun"}

# An added (`+`-prefixed) line in a test.patch that DEFINES a test — used to tell a real
# new test file from a fixture/helper/__init__ file (which shouldn't be in FAIL_TO_PASS).
ADDED_TEST_DEF_RE = re.compile(
    r"^\+\s*(?:"
    r"def\s+test\w*\s*\(|"                          # python / pytest
    r"func\s+(?:Test|Benchmark|Example)\w*\s*\(|"   # go
    r"(?:it|test|describe)\s*\(|"                    # js / ts (jest / mocha / vitest)
    r"@Test\b|"                                      # junit
    r"#\[test\]|"                                    # rust
    r"class\s+\w*Test\w*\b"                          # java / phpunit test class
    r")", re.IGNORECASE | re.MULTILINE)


@dataclass
class Finding:
    severity: str  # "ERROR" | "WARN"
    check: str
    message: str


@dataclass
class TaskReport:
    task_id: str
    path: str
    findings: list[Finding] = field(default_factory=list)

    def add(self, severity: str, check: str, message: str) -> None:
        self.findings.append(Finding(severity, check, message))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "ERROR"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "WARN"]

    def verdict(self, strict: bool) -> str:
        if self.errors:
            return "FAIL"
        if strict and self.warnings:
            return "FAIL"
        if self.warnings:
            return "PASS_WITH_WARNINGS"
        return "PASS"

    def to_dict(self, strict: bool) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "path": self.path,
            "verdict": self.verdict(strict),
            "errors": [{"check": f.check, "message": f.message} for f in self.errors],
            "warnings": [{"check": f.check, "message": f.message} for f in self.warnings],
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ""  # caller already reports missing files


def _nonblank(text: str) -> bool:
    return bool(text and text.strip())


def _f2p_to_relpath(entry: str) -> str | None:
    """Convert a FAIL_TO_PASS entry to the test file's repo-relative path.

    Entries look like:
        monkey.tests.unit_tests.common.agent_events.test_x::test_y[param]
        monkey/tests/.../test_x.py::test_y
    Returns a slash path ending in .py, or None if it can't be parsed.
    """
    head = entry.split("::", 1)[0].strip()
    if not head:
        return None
    if head.endswith(".py"):
        return head
    if "/" in head:  # already a path but maybe without .py
        return head if head.endswith(".py") else head + ".py"
    # dotted module path -> slash path
    return head.replace(".", "/") + ".py"


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

def check_files(task_dir: Path, r: TaskReport) -> None:
    for name, sev, role in REQUIRED_FILES:
        p = task_dir / name
        if not p.is_file():
            r.add(sev, "file-present", f"missing required file `{name}` ({role})")
        elif p.stat().st_size == 0:
            r.add(sev, "file-nonempty", f"required file `{name}` is empty ({role})")
    for name, sev, role in RECOMMENDED_FILES:
        if not (task_dir / name).is_file():
            r.add(sev, "file-present", f"missing recommended file `{name}` ({role})")
    for name, sev, role in RECOMMENDED_DIRS:
        if not (task_dir / name).is_dir():
            r.add(sev, "dir-present", f"missing recommended dir `{name}/` ({role})")


def check_prompt(task_dir: Path, r: TaskReport) -> None:
    """AGENT_PROMPT = prompt_statement.md; fall back to problem_statement.md if empty."""
    prompt = _read_text(task_dir / "prompt_statement.md")
    issue = _read_text(task_dir / "problem_statement.md")
    if not _nonblank(prompt):
        if _nonblank(issue):
            r.add("WARN", "agent-prompt",
                  "prompt_statement.md is empty/blank — falling back to problem_statement.md "
                  "(fairness will be judged against the richer issue)")
        else:
            r.add("ERROR", "agent-prompt",
                  "both prompt_statement.md and problem_statement.md are empty — agent has no prompt")


def check_metadata(task_dir: Path, r: TaskReport) -> dict[str, Any] | None:
    meta_path = task_dir / "test_metadata.json"
    if not meta_path.is_file():
        return None  # already reported by check_files
    try:
        meta = json.loads(_read_text(meta_path))
    except json.JSONDecodeError as e:
        r.add("ERROR", "metadata-json", f"test_metadata.json is not valid JSON: {e}")
        return None
    if not isinstance(meta, dict):
        r.add("ERROR", "metadata-json", "test_metadata.json top-level is not an object")
        return None

    for key, sev in REQUIRED_META_KEYS + RECOMMENDED_META_KEYS:
        if key not in meta:
            r.add(sev, "metadata-key", f"test_metadata.json missing key `{key}`")

    # FAIL_TO_PASS must be a non-empty list.
    f2p = meta.get("FAIL_TO_PASS")
    if f2p is not None:
        if not isinstance(f2p, list):
            r.add("ERROR", "fail-to-pass", "FAIL_TO_PASS is not a list")
        elif len(f2p) == 0:
            r.add("ERROR", "fail-to-pass", "FAIL_TO_PASS is empty — nothing proves the bug is fixed")

    # test_command non-blank.
    tc = meta.get("test_command")
    if tc is not None and not (isinstance(tc, str) and _nonblank(tc)):
        r.add("ERROR", "test-command", "test_command is empty/blank")

    # base_commit looks like a git sha.
    bc = meta.get("base_commit")
    if isinstance(bc, str) and bc.strip():
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", bc.strip()):
            r.add("WARN", "base-commit", f"base_commit `{bc}` does not look like a git sha")
    elif "base_commit" in meta:
        r.add("ERROR", "base-commit", "base_commit is empty")

    # num_test_files vs test_files length.
    tf = meta.get("test_files")
    ntf = meta.get("num_test_files")
    if isinstance(tf, list) and isinstance(ntf, int) and ntf != len(tf):
        r.add("WARN", "test-files-count",
              f"num_test_files={ntf} but test_files has {len(tf)} entries")

    return meta


def check_cross_consistency(task_dir: Path, meta: dict[str, Any] | None, r: TaskReport) -> None:
    """Internal-consistency checks across files (all WARN — legitimate variation possible)."""
    if meta is None:
        return

    # base_commit should be referenced in the Dockerfile (git checkout <sha>).
    bc = str(meta.get("base_commit", "")).strip()
    dockerfile = _read_text(task_dir / "Dockerfile")
    if bc and dockerfile and bc not in dockerfile:
        r.add("WARN", "dockerfile-base-commit",
              f"base_commit `{bc[:12]}…` not found in Dockerfile (env may not pin the right commit)")

    # golden.patch should NOT modify test files (golden = diff minus tests).
    golden = _read_text(task_dir / "golden.patch")
    if golden:
        golden_paths = re.findall(r"^\+\+\+ b/(.+)$", golden, re.MULTILINE)
        leaked = [p for p in golden_paths if TESTFILE_HINT.search(p)]
        if leaked:
            r.add("WARN", "golden-touches-tests",
                  "golden.patch appears to modify test file(s) — golden should exclude tests: "
                  + ", ".join(sorted(set(leaked))[:5]))

    # test.patch should touch at least one test-looking file.
    testpatch = _read_text(task_dir / "test.patch")
    if testpatch:
        tp_paths = re.findall(r"^\+\+\+ b/(.+)$", testpatch, re.MULTILINE)
        if tp_paths and not any(TESTFILE_HINT.search(p) for p in tp_paths):
            r.add("WARN", "test-patch-no-tests",
                  "test.patch does not appear to touch any test file (paths: "
                  + ", ".join(tp_paths[:5]) + ")")

    # FAIL_TO_PASS test files should be among test_files.
    f2p = meta.get("FAIL_TO_PASS")
    test_files = meta.get("test_files")
    if isinstance(f2p, list) and isinstance(test_files, list) and test_files:
        tf_set = {str(x) for x in test_files}
        tf_suffixes = tf_set | {x.rsplit("/", 1)[-1] for x in tf_set}
        unmatched = set()
        for entry in f2p:
            rel = _f2p_to_relpath(str(entry))
            if rel is None:
                continue
            base = rel.rsplit("/", 1)[-1]
            if rel in tf_set:
                continue
            if any(t.endswith(rel) or rel.endswith(t) for t in tf_set):
                continue
            if base in tf_suffixes:
                continue
            unmatched.add(rel)
        if unmatched:
            r.add("WARN", "f2p-not-in-test-files",
                  f"{len(unmatched)} FAIL_TO_PASS file(s) not matched to any test_files entry: "
                  + ", ".join(sorted(unmatched)[:5]))

    # run_test.sh should be consistent with test_command (loose: share the framework binary).
    runtest = _read_text(task_dir / "run_test.sh")
    tc = str(meta.get("test_command", "")).strip()
    if runtest and tc:
        first_token = tc.split()[0] if tc.split() else ""
        if first_token and first_token not in runtest:
            r.add("WARN", "runtest-vs-command",
                  f"run_test.sh does not mention `{first_token}` from test_command "
                  "(harness and graded command may diverge)")


# --------------------------------------------------------------------------- #
# Untracked-test-file check (Alibaba Concern 2).
# --------------------------------------------------------------------------- #

def _added_test_files(testpatch: str) -> list[tuple[str, set[str]]]:
    """New test files a test.patch ADDS (not edits to an existing test file), with the
    test identifiers each one defines.

    A new file is signalled by `new file mode` / `--- /dev/null` in its diff block.
    Only files that look like tests AND add >=1 test definition count — so fixtures,
    helpers, and `__init__.py` don't false-positive. Returns [(path, {names})].
    """
    out: list[tuple[str, set[str]]] = []
    for blk in re.split(r"(?m)^diff --git ", testpatch):
        if not blk.strip():
            continue
        m = re.search(r"^\+\+\+ b/(.+)$", blk, re.MULTILINE)
        if not m:
            continue
        path = m.group(1).strip()
        if path == "/dev/null" or not TESTFILE_HINT.search(path):
            continue
        if "new file mode" not in blk and "--- /dev/null" not in blk:
            continue  # an edit to an existing (already-graded) test file, not a new one
        added = "\n".join(l for l in blk.splitlines() if l.startswith("+"))
        if not ADDED_TEST_DEF_RE.search(added):
            continue  # new file but no test definitions -> helper/fixture, not graded
        names: set[str] = set()
        for mm in re.finditer(r"(?:def|func|fn)\s+(\w+)", added):
            names.add(mm.group(1))
        for mm in re.finditer(r"\b(?:it|test|describe)\s*\(\s*[\"'`]([^\"'`]+)", added):
            names.add(mm.group(1).strip())
        out.append((path, names))
    return out


def check_untracked_test_files(task_dir: Path, meta: dict[str, Any] | None,
                               r: TaskReport) -> None:
    """A NEW test file added in test.patch that NO FAIL_TO_PASS / PASS_TO_PASS entry
    references runs during eval but its result never counts — a no-op or wrong solution
    can break it and still score 1.0 (a silent false positive).

    Alibaba Concern 2 (6 tasks): on `apollographql-federation-278` the golden run had
    6/151 tests fail, all in the untracked file, yet the harness reported golden=1.0.
    Deterministic and high-value; the consolidated feedback taxonomy attributes this to
    L1. Fix is metadata-only: append the file's test identifiers to FAIL_TO_PASS.
    """
    if meta is None:
        return
    testpatch = _read_text(task_dir / "test.patch")
    if not testpatch:
        return
    added = _added_test_files(testpatch)
    if not added:
        return
    graded: list[str] = []
    for key in ("FAIL_TO_PASS", "PASS_TO_PASS"):
        v = meta.get(key)
        if isinstance(v, list):
            graded.extend(str(x) for x in v)
    if not graded:
        return  # empty FAIL_TO_PASS is its own (separate) error — don't double-report
    blob = "\n".join(graded)
    untracked = []
    for path, names in added:
        base = path.rsplit("/", 1)[-1]
        noext = base.rsplit(".", 1)[0]
        # Tracked if the file path, basename, ext-less basename, OR any test name it
        # defines appears anywhere in the graded keys (generous match -> high precision).
        toks = {path, base, noext} | names
        if not any(t and t in blob for t in toks):
            untracked.append(path)
    if untracked:
        # Go uses suite-style tests: F2P entries are function names (e.g. TestEvents)
        # that act as suite entrypoints, while added *_test.go files hold specs that
        # run UNDER them via `go test ./pkg/...` — so a new file whose name isn't in
        # F2P is still executed and counted. Per-file untracking is therefore a false
        # signal in Go (confirmed FP on the navidrome golden), unlike pytest where
        # node-ID selection makes an untracked file genuinely unscored. Downgrade to
        # WARN for Go; keep it a hard ERROR for pytest/JS-style frameworks.
        lang = str((meta.get("language") or "")).lower()
        sev = "WARN" if lang in ("go", "golang") else "ERROR"
        r.add(sev, "testfile-untracked-in-metadata",
              "test.patch adds new test file(s) not referenced by FAIL_TO_PASS/PASS_TO_PASS: "
              + ", ".join(sorted(set(untracked))[:5])
              + " — their tests run during eval but never count toward the score, so a "
                "no-op/wrong solution can break them and still score 1.0. Append their "
                "test identifiers to FAIL_TO_PASS in test_metadata.json (Alibaba Concern 2)."
              + (" [Go suite-style: downgraded to WARN — go test runs the whole package "
                 "under the tracked suite entrypoint.]" if sev == "WARN" else ""))


# --------------------------------------------------------------------------- #
# Environment-hygiene checks (deterministic; derived from client failures).
# Each maps to a real defect in swe-bench-ext-client-feedback.md.
# --------------------------------------------------------------------------- #

def _dockerfile_copy_sources(dockerfile: str) -> list[str]:
    """Return the source operands of every COPY/ADD instruction in a Dockerfile."""
    sources: list[str] = []
    for line in dockerfile.splitlines():
        m = re.match(r"\s*(?:COPY|ADD)\s+(.+)", line, re.IGNORECASE)
        if not m:
            continue
        # drop --flags (e.g. --chown=, --from=); strip JSON-array brackets/quotes
        parts = [p.strip('[],"') for p in m.group(1).split() if not p.startswith("--")]
        if len(parts) >= 2:
            sources.extend(parts[:-1])  # everything but the destination
        elif parts:
            sources.extend(parts)
    return sources


def check_line_endings(task_dir: Path, r: TaskReport) -> None:
    """CRLF/`\\r` in an executed shell or build file breaks the harness (RW Evals #1)."""
    for name in EXEC_FILES:
        p = task_dir / name
        if not p.is_file():
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        if b"\r" in raw:
            r.add("ERROR", "crlf-line-endings",
                  f"`{name}` contains CR/`\\r` line endings — harness may fail to execute it")


def check_leak_and_mounts(task_dir: Path, r: TaskReport) -> None:
    """Dockerfile must not COPY the answer in (leak), and must mount contract files (mount lint)."""
    dockerfile = _read_text(task_dir / "Dockerfile")
    if not dockerfile:
        return
    copied = _dockerfile_copy_sources(dockerfile)

    # (a) Answer/solution/test artifact COPY'd into the agent image -> leakage. ERROR.
    for src in copied:
        if LEAK_FILE_HINT.search(src) or TESTFILE_HINT.search(src):
            r.add("ERROR", "answer-leak",
                  f"Dockerfile COPY/ADD brings a solution/test artifact into the agent image: `{src}`")

    # (b) Mount lint — a contract file exists in the task but is never COPY'd in. WARN.
    joined = " ".join(copied)
    for name in CONTRACT_FILES:
        if (task_dir / name).is_file() and name not in joined and name not in dockerfile:
            r.add("WARN", "contract-not-mounted",
                  f"`{name}` exists in the task but is not COPY'd into the image — "
                  "agent may not see required names/spec (false-negative risk)")


def check_env_hygiene(task_dir: Path, r: TaskReport) -> None:
    """Unpinned deps / verify-time network fetch / unstarted services (RW Evals #4, #15, #25)."""
    dockerfile = _read_text(task_dir / "Dockerfile")
    runtest = _read_text(task_dir / "run_test.sh")

    if "@latest" in dockerfile or "@latest" in runtest:
        r.add("WARN", "dep-unpinned",
              "`@latest` dependency found — pin versions so the env doesn't drift over time")

    # Installing/fetching at VERIFY time (in run_test.sh) is the dependency-drift hazard (MAI: 29 tasks).
    if runtest:
        for tok in ("pip install", "npm install", "yarn add", "apt-get install", "curl ", "wget "):
            if tok in runtest:
                r.add("WARN", "verify-time-fetch",
                      f"run_test.sh runs `{tok.strip()}` at verify time — "
                      "bake deps into the image instead (dependency-drift / network-flake risk)")
                break

    # A service is enabled but nothing starts it (no init as PID 1) -> it never runs.
    if "systemctl enable" in dockerfile:
        if not any(s in dockerfile for s in ("systemctl start", "ENTRYPOINT", "CMD")):
            r.add("WARN", "service-not-started",
                  "Dockerfile `systemctl enable`s a service but nothing starts it "
                  "(no init/CMD/ENTRYPOINT) — the service won't run in the container")


# --------------------------------------------------------------------------- #
# Delivery-hygiene checks (metadata fidelity / packaging readability).
# These tasks build, pass golden, and don't leak — they're just mislabeled,
# dirty, or unreadable. All WARN (FAIL only under --strict). Derived from
# client feedback on fingerprintjs / atdatabases / ibm-fhir.
# --------------------------------------------------------------------------- #

def check_test_framework_consistency(task_dir: Path, meta: dict[str, Any] | None,
                                     r: TaskReport) -> None:
    """Flag a declared test_framework that contradicts the test_command / language.

    Catches the 'wrong field' defect (e.g. test_framework="pytest" on a TypeScript
    project whose test_command runs karma). check_metadata only verifies the key is
    present — it never reads the value, so the mislabel sails through.
    """
    if meta is None:
        return
    declared = str(meta.get("test_framework", "")).strip().lower()
    if declared not in FRAMEWORK_SIGNATURES:
        return  # absent, blank, or a custom framework we can't reason about
    expected_tokens, declared_family = FRAMEWORK_SIGNATURES[declared]

    # (a) test_command clearly runs a *different* known framework.
    tc = str(meta.get("test_command", "")).lower()
    if tc and not any(tok in tc for tok in expected_tokens):
        detected = [
            name for name, (tokens, _) in FRAMEWORK_SIGNATURES.items()
            if name != declared and any(tok in tc for tok in tokens)
        ]
        if detected:
            r.add("WARN", "test-framework-mismatch",
                  f"test_framework=`{declared}` but test_command runs `{detected[0]}` "
                  "— the framework field looks wrong")
            return  # one clear signal is enough; don't double-report

    # (b) declared framework's language conflicts with the `language` field.
    lang = str(meta.get("language", "")).strip().lower()
    lang_family = LANGUAGE_FAMILY.get(lang)
    if lang_family and lang_family != declared_family:
        r.add("WARN", "test-framework-mismatch",
              f"test_framework=`{declared}` ({declared_family}) contradicts "
              f"language=`{lang}` ({lang_family}) — the framework field looks wrong")


def check_test_name_readability(task_dir: Path, meta: dict[str, Any] | None,
                                r: TaskReport) -> None:
    """Flag FAIL_TO_PASS entries with anonymized, non-descriptive names.

    Client defect (ibm-fhir): all 54 FAIL_TO_PASS entries were `test_1` … `test_54`,
    untraceable to requirements.json or the real test methods. The list/mapping checks
    in check_metadata pass on these — the names just aren't useful.
    """
    if meta is None:
        return
    # Suppress for count-summary frameworks (Maven Surefire / Bun / mocha): there the
    # parser legitimately synthesizes `test_N` and Alibaba confirmed it grades correctly
    # — flagging it would re-raise a finding the client already rejected as a non-defect.
    fw = str(meta.get("test_framework", "")).strip().lower()
    if fw in COUNT_SUMMARY_FRAMEWORKS:
        return
    f2p = meta.get("FAIL_TO_PASS")
    if not isinstance(f2p, list) or not f2p:
        return
    anon = [str(e) for e in f2p if ANON_TEST_RE.match(str(e).split("::")[-1].strip())]
    total = len(f2p)
    # A single oddly-named test is noise; a wall of test_N is a real defect.
    if anon and (len(anon) >= 5 or len(anon) >= total / 2):
        sample = ", ".join(e.split("::")[-1].strip() for e in anon[:3])
        r.add("WARN", "anonymized-test-names",
              f"{len(anon)}/{total} FAIL_TO_PASS entries use anonymized names "
              f"(e.g. {sample}) — not traceable to requirements.json or real test "
              "methods; use the real test identifiers")


def check_pipeline_tags(task_dir: Path, r: TaskReport) -> None:
    """Flag leftover internal automation/pipeline identifiers in shipped files.

    Client defect (atdatabases): the Dockerfile carried `# MAI-10K-OTS` and
    `# MAI-V5-FIX:FIX2` comments — automation residue that also cast doubt on whether
    the image was manually verified. These should be stripped before delivery.
    """
    for name in TAG_SCAN_FILES:
        p = task_dir / name
        if not p.is_file():
            continue
        tags: list[str] = []
        for line in _read_text(p).splitlines():
            if "#" not in line:
                continue
            comment = line.split("#", 1)[1]  # only scan the comment portion
            tags.extend(m.group(0) for m in PIPELINE_TAG_RE.finditer(comment))
        if tags:
            uniq = sorted(set(tags))
            r.add("WARN", "pipeline-tag-comment",
                  f"`{name}` has leftover automation/pipeline identifier(s) in comments: "
                  f"{', '.join(uniq[:5])} — strip before delivery (and confirm the image "
                  "wasn't left in an auto-patched, unverified state)")


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def check_task(task_dir: Path) -> TaskReport:
    r = TaskReport(task_id=task_dir.name, path=str(task_dir))
    if not task_dir.is_dir():
        r.add("ERROR", "task-dir", f"not a directory: {task_dir}")
        return r
    check_files(task_dir, r)
    check_prompt(task_dir, r)
    meta = check_metadata(task_dir, r)
    check_cross_consistency(task_dir, meta, r)
    check_untracked_test_files(task_dir, meta, r)
    check_test_framework_consistency(task_dir, meta, r)
    check_test_name_readability(task_dir, meta, r)
    check_line_endings(task_dir, r)
    check_leak_and_mounts(task_dir, r)
    check_pipeline_tags(task_dir, r)
    check_env_hygiene(task_dir, r)
    return r


def check(path: str | Path, strict: bool = False) -> dict[str, Any]:
    """Programmatic entry point. Check one task dir, return a JSON-serializable dict.

    Auto-resolves a wrapper dir (e.g. `task1/` containing a single task subdir).
    The returned dict has keys: task_id, path, verdict
    ("PASS" | "PASS_WITH_WARNINGS" | "FAIL"), errors[], warnings[].
    """
    report = check_task(resolve_task_dir(Path(path)))
    return report.to_dict(strict)


def looks_like_task_dir(p: Path) -> bool:
    return (p / "test_metadata.json").is_file() or (p / "golden.patch").is_file()


def resolve_task_dir(p: Path) -> Path:
    """A task dir may be wrapped one level deep (e.g. task1/<task-name>/...)."""
    if looks_like_task_dir(p):
        return p
    subdirs = [d for d in sorted(p.iterdir()) if d.is_dir()] if p.is_dir() else []
    if len(subdirs) == 1 and looks_like_task_dir(subdirs[0]):
        return subdirs[0]
    return p


def collect_targets(args: argparse.Namespace) -> list[Path]:
    targets: list[Path] = []
    if args.batch:
        base = Path(args.batch)
        if not base.is_dir():
            sys.exit(f"--batch path is not a directory: {base}")
        for d in sorted(base.iterdir()):
            if d.is_dir():
                targets.append(resolve_task_dir(d))
    for raw in args.paths:
        targets.append(resolve_task_dir(Path(raw)))
    return targets


def print_human(reports: list[TaskReport], strict: bool) -> None:
    icon = {"PASS": "✅", "PASS_WITH_WARNINGS": "⚠️ ", "FAIL": "❌"}
    for r in reports:
        v = r.verdict(strict)
        print(f"\n{icon[v]} {v}  {r.task_id}")
        print(f"   {r.path}")
        for f in r.errors:
            print(f"   ERROR [{f.check}] {f.message}")
        for f in r.warnings:
            print(f"   WARN  [{f.check}] {f.message}")
    # Summary
    n = len(reports)
    fails = sum(1 for r in reports if r.verdict(strict) == "FAIL")
    warns = sum(1 for r in reports if r.verdict(strict) == "PASS_WITH_WARNINGS")
    passes = n - fails - warns
    print(f"\n{'='*60}")
    print(f"Summary: {n} task(s) — {passes} PASS, {warns} PASS_WITH_WARNINGS, {fails} FAIL")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic Step-1 criteria/completeness check for SWE-Bench-Ext tasks.")
    ap.add_argument("paths", nargs="*", help="task directories to check")
    ap.add_argument("--batch", metavar="DIR",
                    help="check every immediate subdirectory of DIR as a task")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of human report")
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as failures (exit non-zero)")
    args = ap.parse_args(argv)

    if not args.paths and not args.batch:
        ap.error("provide at least one task PATH or --batch DIR")

    targets = collect_targets(args)
    reports = [check_task(t) for t in targets]

    if args.json:
        out = {
            "tasks": [r.to_dict(args.strict) for r in reports],
            "summary": {
                "total": len(reports),
                "fail": sum(1 for r in reports if r.verdict(args.strict) == "FAIL"),
                "pass_with_warnings": sum(
                    1 for r in reports if r.verdict(args.strict) == "PASS_WITH_WARNINGS"),
                "pass": sum(1 for r in reports if r.verdict(args.strict) == "PASS"),
            },
        }
        print(json.dumps(out, indent=2))
    else:
        print_human(reports, args.strict)

    return 1 if any(r.verdict(args.strict) == "FAIL" for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())

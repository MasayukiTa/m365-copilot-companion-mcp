"""Golden-trajectory regression harness (MCP spec §21.10.3 / §18.7.4).

Loads small, hermetic "golden trajectories" -- ordered sequences of real tool
calls with declarative expected-state assertions -- from tests/golden/*.json and
replays them via tools.golden.run_trajectory() against the REAL tool functions
(no mocking of the tools themselves). This is a REGRESSION FIXTURE, not an LLM
eval: no model is involved anywhere in this file. The point is that a change
which breaks a core tool's contract (write_file stops creating parent dirs,
read_file's error-string convention changes, list_directory drops an entry,
hash_file's output format changes) fails a fast, deterministic pytest instead of
being discovered later against a live agent run.

Imports ONLY tools.file_ops / tools.golden directly -- NOT main.py (main.py pulls
Windows-only modules such as pywin32 and does not import on Linux/CI).

Hermetic: every trajectory operates under a per-test tmp_path, which is pointed
to by monkeypatching MCP_ALLOWED_BASE (mirrors tools/file_ops._parse_allowed_bases()
reading that env var at import time -- so we also monkeypatch the already-imported
tools.file_ops.ALLOWED_BASES / ALLOWED_BASE directly rather than relying on
re-import). write_file is gated by security.require_unlocked(), which returns a
"[locked: no HTTP request context]" string when called outside a real HTTP
request (exactly the case here) -- so require_unlocked is monkeypatched to a
no-op, the same technique tools/test_layer1_security.py already uses for
jobs.py's require_unlocked. No network, no subprocess, no OS-specific paths
(everything goes through tmp_path); each test cleans up automatically because
tmp_path is pytest-managed and the ALLOWED_BASE monkeypatch is undone after
every test.

Run: .venv\\Scripts\\python.exe -m pytest -q tests\\test_golden.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools import file_ops
from tools.file_ops import hash_file, list_directory, read_file, write_file
from tools.golden import run_trajectory

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

TOOLS_BY_NAME = {
    "read_file": read_file,
    "write_file": write_file,
    "list_directory": list_directory,
    "hash_file": hash_file,
}


@pytest.fixture(autouse=True)
def _hermetic_allowed_base(tmp_path, monkeypatch):
    """Point the file tools' allowed base at a pytest-managed tmp dir and make
    write_file's require_unlocked() gate a no-op for this test process (there is
    no real HTTP request context here). Restored automatically by monkeypatch at
    teardown; tmp_path itself is cleaned up by pytest."""
    monkeypatch.setenv("MCP_ALLOWED_BASE", str(tmp_path))
    monkeypatch.setattr(file_ops, "ALLOWED_BASES", [tmp_path.resolve()])
    monkeypatch.setattr(file_ops, "ALLOWED_BASE", tmp_path.resolve())
    monkeypatch.setattr(file_ops, "require_unlocked", lambda: None)
    yield


def _load_trajectory(name: str, base: Path) -> list:
    """Load a JSON fixture and substitute the {base} placeholder with the real
    tmp_path so every path in the fixture resolves under the hermetic sandbox."""
    raw = (GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8")
    raw = raw.replace("{base}", str(base.as_posix()))
    spec = json.loads(raw)
    steps = spec["steps"]
    # normalize expect specs whose value is a JSON list back into the tuple shape
    # run_trajectory's _eval_expect expects for two-arg checks (JSON has no tuples).
    for step in steps:
        expect = step.get("expect") or {}
        for key in ("file_content_equals", "file_contains"):
            if key in expect and isinstance(expect[key], list):
                expect[key] = tuple(expect[key])
    return steps


@pytest.mark.parametrize("fixture_name", ["fs_roundtrip", "read_missing", "dir_listing_and_hash"])
def test_golden_trajectory_passes(fixture_name, tmp_path):
    steps = _load_trajectory(fixture_name, tmp_path)
    records, failures = run_trajectory(steps, TOOLS_BY_NAME)

    assert len(records) == len(steps), "every step must produce a record even on failure"
    assert failures == [], f"golden trajectory {fixture_name!r} regressed: {failures}"


def test_golden_trajectory_reports_step_names_and_shape():
    """Sanity-check the ToolCallRecord shape returned by run_trajectory lines up
    with trace_ops' record fields (ts, name, ok, dur_ms, args, result, error)."""
    steps = [
        {"tool": "read_file", "kwargs": {"path": "/definitely/not/here.txt"}, "expect": {"raises_or_error": True}},
    ]
    records, failures = run_trajectory(steps, TOOLS_BY_NAME)
    assert failures == []
    rec = records[0].as_dict()
    for key in ("ts", "name", "ok", "dur_ms", "args", "result", "error"):
        assert key in rec, f"ToolCallRecord.as_dict() missing trace_ops-aligned field {key!r}"
    assert rec["name"] == "read_file"
    assert rec["ok"] is True


def test_harness_actually_detects_a_broken_expectation(tmp_path):
    """Proves the harness CHECKS rather than rubber-stamping: feed it a
    deliberately WRONG expected value for a real, successful write_file/read_file
    pair and confirm run_trajectory reports a failure for that step (and does not
    raise -- a bad expectation is data, not an exception)."""
    base = tmp_path.as_posix()
    steps = [
        {
            "tool": "write_file",
            "kwargs": {"path": f"{base}/x.txt", "content": "actual content"},
            "expect": {"returns_contains": "Wrote"},
        },
        {
            # deliberately wrong: the file actually contains "actual content"
            "tool": "read_file",
            "kwargs": {"path": f"{base}/x.txt"},
            "expect": {"returns_contains": "THIS STRING WAS NEVER WRITTEN"},
        },
    ]
    records, failures = run_trajectory(steps, TOOLS_BY_NAME)

    assert len(records) == 2
    assert records[0].ok is True, "the write step itself must still succeed"
    assert records[1].ok is False, "the deliberately-wrong expectation must be reported as a failure"
    assert len(failures) == 1
    assert failures[0]["step"] == 1
    assert failures[0]["name"] == "read_file"
    assert "THIS STRING WAS NEVER WRITTEN" in failures[0]["detail"]


def test_harness_runs_all_steps_even_after_a_failure(tmp_path):
    """A failing step must not abort the trajectory -- every step still runs and
    is recorded, matching the "record, don't raise" contract."""
    base = tmp_path.as_posix()
    steps = [
        {"tool": "read_file", "kwargs": {"path": f"{base}/missing.txt"}, "expect": {"returns_contains": "nope, wrong"}},
        {"tool": "write_file", "kwargs": {"path": f"{base}/after.txt", "content": "still runs"}, "expect": {"returns_contains": "Wrote"}},
    ]
    records, failures = run_trajectory(steps, TOOLS_BY_NAME)
    assert len(records) == 2, "second step must still execute despite the first step failing"
    assert records[0].ok is False
    assert records[1].ok is True
    assert len(failures) == 1
    assert failures[0]["step"] == 0

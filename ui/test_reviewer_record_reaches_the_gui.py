# -*- coding: utf-8 -*-
"""The GUI shows the reviewer's recorded conversation, read from the sqlite ledger.

The cockpit (C#) cannot open sqlite, so it runs scripts/ledger_dump.py and renders its JSON line.
These tests run that script as the cockpit does (a subprocess, a store directory argument) against
a store filled the way the fleet fills it, and check what comes back -- full text, lens, time,
verdict, and an explicit "recorded: false" for a run with no reviewer text. They also check the
submitter lookup the history rows display.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bridge import session_store as S  # noqa: E402


def _dump(key, store_dir):
    out = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "ledger_dump.py"),
                          "--key", key, "--store-dir", str(store_dir)],
                         capture_output=True, cwd=REPO, timeout=60)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    lines = out.stdout.decode("utf-8").strip().splitlines()
    assert len(lines) == 1, "the cockpit parses exactly one JSON line"
    return json.loads(lines[0])


def test_the_dump_returns_every_text_with_lens_time_and_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv(S.STORE_DIR_ENV, str(tmp_path))
    prompt = "REVIEW PROMPT\n" + "line\n" * 500
    S.record_refuter_turn("rA_a0_w0", "refuter_user", 0, prompt, lens="security", ts=1000.0,
                          extra={"route": "socket"})
    S.record_refuter_turn("rA_a0_w0", "refuter_assistant", 1, "REFUTED: no test", lens="security",
                          ts=1010.0)
    S.record_refuter_turn("rA_a0_w0", "refuter_verdict", 10000, "REFUTED: no test",
                          lens="security", ts=1011.0)
    S.record_refuter_turn("rA_a0_w0", "refuter_user", 0, "other lens prompt", lens="edge", ts=1001.0)
    S.record_refuter_turn("rB_a0_w0", "refuter_user", 0, "someone else", lens="edge")
    got = _dump("rA_a0_w0", tmp_path)
    assert got["recorded"] is True
    assert [(t["lens"], t["role"]) for t in got["turns"]] == [
        ("security", "refuter_user"), ("edge", "refuter_user"),
        ("security", "refuter_assistant"), ("security", "refuter_verdict")]
    assert got["turns"][0]["text"] == prompt and got["turns"][0]["route"] == "socket"
    assert got["turns"][0]["ts"] == 1000.0 and len(got["turns"][0]["sha16"]) == 16
    assert all("someone else" not in t["text"] for t in got["turns"])


def test_a_run_with_no_reviewer_text_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv(S.STORE_DIR_ENV, str(tmp_path))
    S.record_fleet_turn("rC_a0_w0", {"role": "user", "text": "x"})
    got = _dump("rC_a0_w0", tmp_path)
    assert got["recorded"] is False and got["turns"] == []


def test_the_submitter_is_read_from_the_jobs_provenance(tmp_path):
    from relay import task_router as TR
    d = tmp_path / "done"
    d.mkdir()
    (d / "abc123.json").write_text(json.dumps({"origin": {"via": "mcp", "source": "claude-code"}}),
                                   encoding="utf-8")
    assert TR.submitter_for_jid("abc123", tasks_dir=str(tmp_path)) == "mcp:claude-code"
    assert TR.submitter_for_jid("nope", tasks_dir=str(tmp_path)) == ""
    assert TR.submitter_for_jid("../x", tasks_dir=str(tmp_path)) == ""

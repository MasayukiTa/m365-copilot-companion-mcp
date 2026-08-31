"""The judgement layer, as the execution path actually reaches it.

command_judge is tested as a pure policy; this checks the wiring: that shadow records without
blocking, that enforce refuses, that a read-only command never reaches a judge, and that a
broken judge cannot break execution.
"""
import io
import json
import os

import pytest

os.environ.setdefault("MCP_API_KEY", "test-key-not-used")

from tools import code_exec as CE
from tools import command_triage as T


@pytest.fixture
def judged(tmp_path, monkeypatch):
    """Capture what would be recorded, without writing to the repository's own log."""
    rows = []
    monkeypatch.setattr(CE, "_record_judgement",
                        lambda kind, text, req, verdict, mode_name, blocks:
                        rows.append((kind, text, verdict, mode_name, blocks)))
    return rows


def _backend(answer):
    return lambda _req: answer


def test_shadow_records_and_does_not_block(judged, monkeypatch):
    monkeypatch.setenv(T.MODE_ENV, "shadow")
    monkeypatch.setattr(CE, "_judge_backend",
                        lambda: _backend('{"decision":"BLOCK_AND_RETRY","reason":"no"}'))
    assert CE._judged("shell", "rm -rf build", None) is None
    assert judged and judged[0][2]["decision"] == "BLOCK_AND_RETRY"
    assert judged[0][4] is True, "the record must say it WOULD have blocked"


def test_enforce_refuses_and_says_why(judged, monkeypatch):
    monkeypatch.setenv(T.MODE_ENV, "enforce")
    monkeypatch.setattr(CE, "_judge_backend",
                        lambda: _backend('{"decision":"BLOCK_AND_RETRY","reason":"deletes src"}'))
    out = CE._judged("shell", "rm -rf src", None)
    assert out is not None
    assert "deletes src" in out, out


def test_enforce_allows_what_the_judge_allows(judged, monkeypatch):
    monkeypatch.setenv(T.MODE_ENV, "enforce")
    monkeypatch.setattr(CE, "_judge_backend",
                        lambda: _backend('{"decision":"ALLOW","reason":"asked for"}'))
    assert CE._judged("shell", "rm -rf build", None) is None


def test_a_read_only_command_never_reaches_the_judge(judged, monkeypatch):
    """The latency answer. A judging call in front of `git status` buys nothing, and a layer
    that makes every trivial command slow is one people switch off."""
    monkeypatch.setenv(T.MODE_ENV, "enforce")
    called = []
    monkeypatch.setattr(CE, "_judge_backend",
                        lambda: (called.append(1), _backend('{"decision":"ALLOW"}'))[1])
    assert CE._judged("shell", "git status", None) is None
    assert not judged, "a read-only command was judged"


def test_python_source_is_always_judged(judged, monkeypatch):
    """run_python executes anything; there is no read-only spelling of it."""
    monkeypatch.setenv(T.MODE_ENV, "shadow")
    monkeypatch.setattr(CE, "_judge_backend", lambda: _backend('{"decision":"ALLOW"}'))
    CE._judged("python", "print(1)", None)
    assert judged and judged[0][0] == "python"


def test_no_backend_is_require_human_not_allow(judged, monkeypatch):
    monkeypatch.setenv(T.MODE_ENV, "shadow")
    monkeypatch.setattr(CE, "_judge_backend", lambda: None)
    CE._judged("shell", "rm -rf x", None)
    assert judged[0][2]["decision"] == "REQUIRE_HUMAN"
    assert judged[0][2]["source"] == "no_judge"
    assert judged[0][4] is True


def test_off_skips_everything(judged, monkeypatch):
    monkeypatch.setenv(T.MODE_ENV, "off")
    monkeypatch.setattr(CE, "_judge_backend", lambda: _backend('{"decision":"BLOCK_AND_RETRY"}'))
    assert CE._judged("shell", "rm -rf x", None) is None
    assert not judged


def test_a_judge_that_explodes_cannot_break_execution(monkeypatch):
    """It sits in front of every command. An exception here would take the tool down."""
    monkeypatch.setenv(T.MODE_ENV, "enforce")

    def _boom():
        raise RuntimeError("backend is on fire")
    monkeypatch.setattr(CE, "_judge_backend", _boom)
    assert CE._judged("shell", "rm -rf x", None) is None


def test_the_deterministic_flags_are_passed_to_the_judge(judged, monkeypatch):
    """The model is told what the machine already knows, so it is not re-deriving it."""
    monkeypatch.setenv(T.MODE_ENV, "shadow")
    seen = {}

    def _capture(req_json):
        seen.update(json.loads(req_json))
        return '{"decision":"ALLOW"}'
    monkeypatch.setattr(CE, "_judge_backend", lambda: _capture)
    CE._judged("shell", "rm -rf /var/data", None)
    assert "destructive_shell" in seen.get("deterministic_flags", [])


def test_the_request_never_carries_agent_prose(monkeypatch):
    seen = {}

    def _capture(req_json):
        seen.update(json.loads(req_json))
        return '{"decision":"ALLOW"}'
    monkeypatch.setenv(T.MODE_ENV, "shadow")
    monkeypatch.setattr(CE, "_judge_backend", lambda: _capture)
    monkeypatch.setattr(CE, "_record_judgement", lambda *a, **k: None)
    CE._judged("shell", "rm -rf x", None)
    assert set(seen) >= {"pending_command", "cwd", "deterministic_flags"}
    assert "assistant" not in json.dumps(seen)

# -*- coding: utf-8 -*-
"""MCP_UNLOCK_REQUIRED: the owner's reversible switch for the whole unlock gate.

Owner order 2026-10-15: unlock must never be the reason a fleet goal fails. Overnight, fleet
workers on ADAMS ODBC goals ended STUCK because the model refused to type a password that a
message handed it. The switch turns the gate off without touching code or restarting the
relay; default 1 is the gate exactly as it was.

Run: python -m pytest -q tests/test_unlock_switch.py
"""
import subprocess
import sys
from pathlib import Path

import pytest

from tools import lock_state
from tools import security as S
from tools import unlock_switch as U

ROOT = Path(__file__).resolve().parent.parent
IP = "203.0.113.55"          # RFC 5737 documentation range


class _Req:
    def __init__(self, peer="127.0.0.1", xff=IP):
        self.client = type("c", (), {"host": peer})()
        self.headers = {"x-forwarded-for": xff} if xff else {}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STATE_FILE", tmp_path / "unlock.json")
    monkeypatch.setattr(lock_state, "_STATE_FILE", tmp_path / "lock.json")
    monkeypatch.setattr(lock_state, "_LOG_FILE", tmp_path / "lock_refusals.jsonl")
    monkeypatch.setattr(lock_state, "_TOKEN_GAP_FILE", tmp_path / "gap.json")
    monkeypatch.setattr(S, "get_http_request", lambda: _Req())
    monkeypatch.setenv("MCP_ENV_FILE", str(tmp_path / "test.env"))     # absent unless a test writes it
    monkeypatch.setenv("MCP_ACCESS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv(U.KEY, raising=False)
    monkeypatch.delenv("MCP_REQUIRE_UNLOCK_TOKEN", raising=False)
    monkeypatch.delenv("MCP_UNLOCK_SESSION_AUTH", raising=False)
    S.clear_presented_token()
    _reset_switch_state()
    yield
    S.clear_presented_token()
    _reset_switch_state()


def _reset_switch_state():
    with U._lock:
        U._file_cache.update(path=None, mtime=None, value=None)
        U._audit.update(last_state=None, passes=0, last_write=0.0, first_ip="")


def _write_env(tmp_path, text):
    p = tmp_path / "test.env"
    p.write_text(text, encoding="utf-8")
    return p


def _audit_lines(tmp_path):
    log = tmp_path / "logs" / "access.log"
    if not log.exists():
        return []
    return [l for l in log.read_text(encoding="utf-8").splitlines() if " UNLOCK_SWITCH " in l]


def test_default_is_the_gate_as_it_always_was():
    assert U.unlock_required() is True
    out = S.require_unlocked()
    assert out and out.startswith("[locked client IP:")


@pytest.mark.parametrize("raw", ["0", "false", "OFF", "No", "disabled", " 0 "])
def test_explicit_off_values_open_the_gate_for_a_never_unlocked_remote_caller(monkeypatch, raw):
    monkeypatch.setenv(U.KEY, raw)
    # The password must never even be consulted, and no state may be needed.
    monkeypatch.setattr(S, "unlock_password_from_env",
                        lambda *a, **k: pytest.fail("password read while the gate is off"))
    assert S.require_unlocked() is None


@pytest.mark.parametrize("raw", ["1", "", "2", "maybe", "true", "on"])
def test_anything_that_is_not_an_explicit_off_keeps_the_gate_on(monkeypatch, raw):
    monkeypatch.setenv(U.KEY, raw)
    assert U.unlock_required() is True
    assert S.require_unlocked().startswith("[locked")


def test_off_also_passes_a_call_with_no_http_context(monkeypatch):
    monkeypatch.setenv(U.KEY, "0")

    def _boom():
        raise RuntimeError("no request")
    monkeypatch.setattr(S, "get_http_request", _boom)
    assert S.require_unlocked() is None
    monkeypatch.setenv(U.KEY, "1")
    assert S.require_unlocked().startswith("[locked: no HTTP request context]")


def test_the_env_file_is_read_live_and_wins_over_a_stale_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(U.KEY, "1")                       # what a long-running process inherited
    env = _write_env(tmp_path, "A=1\n%s=0\n" % U.KEY)
    assert U.unlock_required() is False                  # file wins, no restart
    env.write_text("A=1\n%s=1\n" % U.KEY, encoding="utf-8")
    assert U.unlock_required() is True                   # and back
    env.write_text("# %s=0\nB=2\n" % U.KEY, encoding="utf-8")
    monkeypatch.setenv(U.KEY, "0")
    assert U.unlock_required() is False                  # a commented line is not a value: env fallback
    env.write_text('%s="off"  \n' % U.KEY, encoding="utf-8")
    assert U.unlock_required() is False                  # quoted
    env.write_text("%s=0 # owner decision 2026-10-15\n" % U.KEY, encoding="utf-8")
    assert U.unlock_required() is False                  # inline comment


def test_an_unreadable_or_absent_env_file_fails_closed(tmp_path):
    assert U.unlock_required() is True
    (tmp_path / "test.env").mkdir()                      # a directory where the file should be
    assert U.unlock_required() is True


def test_the_state_is_audited_in_access_log(tmp_path, monkeypatch):
    monkeypatch.setenv(U.KEY, "0")
    S.require_unlocked()
    S.require_unlocked()
    lines = _audit_lines(tmp_path)
    assert len(lines) == 1, "first pass writes one line; the rest are throttled: %r" % lines
    assert "MCP_UNLOCK_REQUIRED=0" in lines[0] and "OFF" in lines[0]
    assert lines[0].split(" ", 1)[0].endswith("Z"), "access.log lines start with a UTC stamp"
    # restoring the gate is recorded once, at the edge
    monkeypatch.setenv(U.KEY, "1")
    S.require_unlocked()
    S.require_unlocked()
    lines = _audit_lines(tmp_path)
    assert len(lines) == 2 and "back ON" in lines[1]


def test_heartbeat_counts_calls_while_off(tmp_path, monkeypatch):
    monkeypatch.setenv(U.KEY, "0")
    monkeypatch.setattr(U, "HEARTBEAT_S", 0.0)
    for _ in range(3):
        S.require_unlocked()
    lines = _audit_lines(tmp_path)
    assert len(lines) == 3 and "still OFF: 1 call(s)" in lines[1]


def test_audit_start_records_an_off_gate_and_nothing_when_on(tmp_path, monkeypatch):
    assert U.audit_start() is True
    assert _audit_lines(tmp_path) == []
    monkeypatch.setenv(U.KEY, "0")
    assert U.audit_start() is False
    assert "OFF at start" in _audit_lines(tmp_path)[0]


def test_unlock_tool_when_off_asks_for_no_password_and_issues_no_token(monkeypatch):
    monkeypatch.setenv(U.KEY, "0")
    monkeypatch.setattr(S, "unlock_password_from_env",
                        lambda *a, **k: pytest.fail("password read while the gate is off"))
    out = S.unlock("whatever")
    assert "not required" in out and "unlock_token" not in out and "[unlock" not in out
    assert not S.STATE_FILE.exists(), "nothing may be granted or stored"
    assert "not required" in S.list_unlocked()


def test_unlock_tool_when_on_is_unchanged(monkeypatch):
    monkeypatch.setattr(S, "unlock_password_from_env", lambda *a, **k: "pw")
    assert S.unlock("wrong") == "[unlock failed: incorrect password]"
    out = S.unlock("pw")
    assert out.startswith("Unlocked IP") and "unlock_token: " in out
    assert S.require_unlocked().startswith("[locked")          # no token, no session: still gated
    S.set_presented_token([l.split(": ", 1)[1] for l in out.splitlines()
                           if l.startswith("unlock_token: ")][0])
    assert S.require_unlocked() is None


def test_the_supported_writer_flips_the_switch_both_ways(tmp_path, monkeypatch):
    env = tmp_path / "test.env"
    env.write_text("KEEP=1\n", encoding="utf-8")
    script = str(ROOT / "scripts" / "env_file.py")

    def run(*args):
        r = subprocess.run([sys.executable, script, *args, "--env", str(env)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    run("set", U.KEY, "0")
    assert U.unlock_required() is False
    assert S.require_unlocked() is None
    run("set", U.KEY, "1")
    assert U.unlock_required() is True
    assert S.require_unlocked().startswith("[locked")
    run("unset", U.KEY)
    assert U.unlock_required() is True
    assert "KEEP=1" in env.read_text(encoding="utf-8")


# ---- the relay never tells a worker to unlock while the switch is off ---------------------

def test_relay_sends_no_unlock_turn_and_classifies_no_lock_refusal_when_off(monkeypatch):
    import relay.relay_fleet as RF
    refusal = "[locked client IP: '203.0.113.7'] Mutating and execution tools require an unlock."
    monkeypatch.setenv(U.KEY, "0")
    monkeypatch.setattr(RF, "_unlock_password", lambda: "SECRETPW")
    assert RF._looks_locked(refusal) is False
    assert RF._looks_locked_ambiguous(refusal + "x" * 500) is False
    assert RF.build_reactive_unlock_turn("resume") == ""
    job, injected = RF._initial_job_with_unlock("do the thing", plan_mode=True)
    assert injected is False and "SECRETPW" not in job and RF._UNLOCK_MARKER not in job


def test_relay_still_builds_the_unlock_turn_when_on(monkeypatch):
    import relay.relay_fleet as RF
    monkeypatch.setattr(RF, "_unlock_password", lambda: "SECRETPW")
    assert "SECRETPW" in RF.build_reactive_unlock_turn("resume")
    job, injected = RF._initial_job_with_unlock("do the thing", plan_mode=True)
    assert injected is True and "SECRETPW" in job


def test_inject_unlock_when_off_retries_without_a_password(monkeypatch):
    import relay.relay_fleet as RF
    monkeypatch.setenv(U.KEY, "0")
    monkeypatch.setattr(RF, "_unlock_password", lambda: "SECRETPW")

    class _W:
        name = "w1"
        goal = "G"
        _unlock_attempts = 0
        status = outcome = reason = job = None
        _task_anchor = staticmethod(lambda t: "ANCHOR " + str(t))
    w = _W()
    RF.RelayWorker._inject_unlock(w)
    assert w.status == "ready" and "SECRETPW" not in str(w.job)
    assert RF.UNLOCK_PREFIX.split("%s")[0] not in str(w.job)


def test_apply_reunlock_when_off_delivers_nothing_and_says_why(monkeypatch):
    import relay.fleet_runner as FR
    import relay.relay_fleet as RF
    monkeypatch.setenv(U.KEY, "0")
    monkeypatch.setattr(RF, "_unlock_password", lambda: "SECRETPW")
    seen = []
    res = FR.apply_reunlock("w1", {}, log=seen.append)
    assert res["ok"] is False and res["delivered"] == 0
    assert "MCP_UNLOCK_REQUIRED=0" in res["reason"]
    assert "SECRETPW" not in repr(res) + repr(seen)


# ---- bridge and server wiring (source-level: neither imports cleanly on a CI runner) -------

def test_bridge_gates_both_unlock_paths_on_the_switch():
    src = (ROOT / "bridge" / "copilot_bridge.py").read_text(encoding="utf-8")
    for fn in ("def _bridge_unlock_preflight_due", "def _bridge_should_auto_unlock"):
        body = src[src.index(fn):]
        body = body[:body.index("\ndef ", 10)]
        assert "_bridge_unlock_required()" in body, fn


def test_main_picks_unlock_free_instructions_when_off():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "_UNLOCK_ON = _unlock_switch.unlock_required()" in src
    assert "RULE 3 -- NOTHING NEEDS UNLOCKING" in src
    assert "_unlock_switch.audit_start()" in src

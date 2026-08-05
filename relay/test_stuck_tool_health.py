"""Tests for the STUCK-retry tool-health gate and RETRY/CONTINUE/FIX nudge escalation in
run_relay() (relay/copilot_autopilot_relay.py).

CONFIRMED ROOT CAUSE (mined from stored transcripts): every self-reported STUCK was retried
up to max_transient times by resending the byte-identical RETRY_JOB text, even when the real
cause was a PERMANENT condition (tools absent from the session) that retrying can never fix.
240 of 296 "tools absent" STUCK replies across 2,628 stored transcripts were replies to this
exact RETRY_JOB text.

Fix: gate the STUCK-retry decision on tools/tool_probe.py's independent health signal
(_tool_health_for_stuck), never on the agent's own reply wording -- see
bridge/copilot_bridge.py's comment above MAX_BRIDGE_UNLOCK_ATTEMPTS for why parsing the
agent's own (discipline-clamped, paraphrased) prose is unreliable for this kind of detection.
Also closes the identical-nudge-repetition gap this loop had for RETRY_JOB/CONTINUE_JOB/
FIX_JOB, matching the shape already used by relay_fleet.py's _continue_nudge and refuter.py's
_next_refuter_nudge (counts 1-2 unchanged for back-compat, count 3+ rotates + tags the count).

No browser: drives run_relay() with a scripted MockDriver (same pattern as
relay/test_relay_loop.py's MockDriver) and monkeypatches tools.tool_probe.get_summary so the
tool-health signal is fully controlled -- never touches the real .fleet/tool_probe.json file.

Run:  .venv\\Scripts\\python.exe -m pytest -q relay/test_stuck_tool_health.py
"""
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.copilot_autopilot_relay import (
    run_relay, RETRY_JOB, CONTINUE_JOB, FIX_JOB,
    STUCK_TOOL_HEALTH_MAX_AGE_S, _tool_health_for_stuck,
    _next_retry_job, _next_continue_job, _next_fix_job,
)
from tools import tool_probe


class MockDriver:
    """Scripted driver: returns canned responses; optional per-turn idle flags. Mirrors
    relay/test_relay_loop.py's MockDriver so run_relay() can be driven without a browser."""

    def __init__(self, responses, idle_ok=None):
        self.responses = list(responses)
        self.idle_ok = list(idle_ok) if idle_ok is not None else [True] * max(len(responses), 1)
        self.i = -1
        self.sent = []

    def send(self, text):
        self.i += 1
        self.sent.append(text)

    def wait_for_idle(self, timeout_s=0):
        return self.idle_ok[self.i] if self.i < len(self.idle_ok) else True

    def read_last_response(self):
        return self.responses[self.i] if self.i < len(self.responses) else "still working CONTINUE"


def _healthy_summary(now=None):
    return {"tool_ok": True, "tool_kind": "answer", "tool_ts": time.time(), "tool_age_s": 5.0}


def _unhealthy_fresh_summary(now=None):
    return {"tool_ok": False, "tool_kind": "canned_fallback",
            "tool_ts": time.time(), "tool_age_s": 5.0}


def _empty_summary(now=None):
    return {"tool_ok": None, "tool_kind": None, "tool_ts": None, "tool_age_s": None}


def _stale_ok_summary(now=None):
    stale_age = STUCK_TOOL_HEALTH_MAX_AGE_S + 100.0
    return {"tool_ok": True, "tool_kind": "answer",
            "tool_ts": time.time() - stale_age, "tool_age_s": stale_age}


def test_stuck_terminal_when_tool_unhealthy(monkeypatch):
    """An unhealthy-but-fresh tool-probe reading turns a self-reported STUCK terminal
    IMMEDIATELY -- no RETRY_JOB is ever sent, because retrying a proven-broken tool path
    cannot help (the mined incident this closes)."""
    monkeypatch.setattr(tool_probe, "get_summary", _unhealthy_fresh_summary)
    driver = MockDriver(["STUCK: tools missing"])
    notes = []
    outcome = run_relay(driver, goal="test goal", run_id="test_terminal",
                        notify=lambda title, body: notes.append((title, body)), sleep_s=0)
    assert outcome == "STUCK"
    assert len(driver.sent) == 1          # only the original goal -- RETRY_JOB never sent
    assert RETRY_JOB not in driver.sent
    assert len(notes) == 1
    reason_body = notes[0][1]
    assert "tool" in reason_body.lower() or "reachable" in reason_body.lower()


def test_stuck_still_retries_when_tool_healthy(monkeypatch):
    """Today's behaviour is preserved when the tool path IS healthy: a self-reported STUCK is
    still retried with a RETRY-branch nudge before giving up, and eventually recovers."""
    monkeypatch.setattr(tool_probe, "get_summary", _healthy_summary)
    driver = MockDriver(["STUCK: a", "STUCK: b", "all done DONE"])
    notes = []
    outcome = run_relay(driver, goal="test goal", run_id="test_retry_preserved",
                        notify=lambda title, body: notes.append((title, body)), sleep_s=0)
    assert outcome == "DONE"
    assert len(driver.sent) > 1
    assert RETRY_JOB in driver.sent       # counts 1-2 keep the original RETRY_JOB unchanged


def test_tool_health_stale_probe_is_not_trusted(monkeypatch):
    """A probe result that is 'ok' but OLDER than STUCK_TOOL_HEALTH_MAX_AGE_S must NOT be
    treated as current proof the tool path works -- it must be terminal (keep_retrying=False),
    matching the module's threshold of 1800s (3x the bridge's default 600s probe cadence)."""
    assert STUCK_TOOL_HEALTH_MAX_AGE_S == 1800.0
    monkeypatch.setattr(tool_probe, "get_summary", _stale_ok_summary)
    keep_retrying, detail = _tool_health_for_stuck()
    assert keep_retrying is False
    assert detail  # non-empty, human-readable


def test_tool_health_degrades_to_retry_on_unreadable_probe(monkeypatch):
    """Both an exception from get_summary() and the real 'no record on file' all-None shape
    (what tool_probe.get_summary() actually returns for a missing/corrupt sidecar) must
    degrade to keep_retrying=True -- an unrelated sidecar being missing/unreadable must never
    curtail a genuinely transient failure's retries."""
    def _raises(now=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(tool_probe, "get_summary", _raises)
    keep_retrying, detail = _tool_health_for_stuck()
    assert keep_retrying is True
    assert detail

    monkeypatch.setattr(tool_probe, "get_summary", _empty_summary)
    keep_retrying2, detail2 = _tool_health_for_stuck()
    assert keep_retrying2 is True
    assert detail2


def test_retry_job_escalation_never_repeats_past_backcompat_window():
    assert _next_retry_job(1) == RETRY_JOB
    assert _next_retry_job(2) == RETRY_JOB
    assert _next_retry_job(3) != RETRY_JOB
    assert _next_retry_job(3) != _next_retry_job(4)


def test_continue_job_escalation_never_repeats_past_backcompat_window():
    assert _next_continue_job(1) == CONTINUE_JOB
    assert _next_continue_job(2) == CONTINUE_JOB
    assert _next_continue_job(3) != CONTINUE_JOB
    assert _next_continue_job(3) != _next_continue_job(4)


def test_fix_job_escalation_never_repeats_past_backcompat_window():
    assert _next_fix_job(1) == FIX_JOB
    assert _next_fix_job(2) == FIX_JOB
    assert _next_fix_job(3) != FIX_JOB
    assert _next_fix_job(3) != _next_fix_job(4)


def _talking_but_refusing_summary(now=None):
    """The outage this pair of tests exists for: Copilot answers every probe, but with the
    same refusal each time, so the turn loop times the reply out as a stale repeat."""
    return {"tool_ok": False, "tool_kind": "stale_repeat",
            "tool_ts": time.time(), "tool_age_s": 5.0}


def _silent_summary(now=None):
    return {"tool_ok": False, "tool_kind": "timeout",
            "tool_ts": time.time(), "tool_age_s": 5.0}


def test_stuck_retries_when_the_path_answered_but_failed(monkeypatch):
    """A probe that FAILED is not a probe that proved the path dead. When the far side
    replied -- here, the same refusal over and over -- the round trip demonstrably works,
    so a self-reported STUCK still gets its retry instead of being cut off."""
    monkeypatch.setattr(tool_probe, "get_summary", _talking_but_refusing_summary)
    driver = MockDriver(["STUCK: a", "STUCK: b", "all done DONE"])
    outcome = run_relay(driver, goal="test goal", run_id="test_alive_retry",
                        notify=lambda title, body: None, sleep_s=0)
    assert outcome == "DONE"
    assert RETRY_JOB in driver.sent


def test_stuck_terminal_when_nothing_came_back(monkeypatch):
    """The genuine unreachable case still gives up immediately: no reply at all means
    retrying cannot help."""
    monkeypatch.setattr(tool_probe, "get_summary", _silent_summary)
    driver = MockDriver(["STUCK: tools missing"])
    notes = []
    outcome = run_relay(driver, goal="test goal", run_id="test_silent_terminal",
                        notify=lambda title, body: notes.append((title, body)), sleep_s=0)
    assert outcome == "STUCK"
    assert RETRY_JOB not in driver.sent


def _silent_but_error_kind_summary(now=None):
    """The case the kind alone gets wrong: nothing came back, but the catch-all kind is
    "error" all the same."""
    return {"tool_ok": False, "tool_kind": "error", "tool_alive": False,
            "tool_ts": time.time(), "tool_age_s": 5.0}


def test_recorded_liveness_wins_over_the_kind_guess(monkeypatch):
    monkeypatch.setattr(tool_probe, "get_summary", _silent_but_error_kind_summary)
    driver = MockDriver(["STUCK: tools missing"])
    outcome = run_relay(driver, goal="test goal", run_id="test_recorded_dead",
                        notify=lambda title, body: None, sleep_s=0)
    assert outcome == "STUCK"
    assert RETRY_JOB not in driver.sent

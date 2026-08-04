"""Hermetic tests for CopilotWebDriver.wait_for_idle's settle + turn-correspondence
guard -- no browser, no real waiting. A fake monotonically-advancing clock drives
time.time()/time.sleep() so the settle loop's real interval/dwell arithmetic runs
deterministically and instantly.

BACKGROUND (see the incident this closes): mining 2,628 stored transcripts (3,931
agent replies) found 275 replies (7.0%) captured TRUNCATED -- a DOM read landed while
the page was still rendering, so a partial string (e.g. "takeuchifile操作\\nリ", 2
characters after the header) got persisted as if it were the final answer. Separately,
the idle tool probe was observed live reading back a reply carrying the PREVIOUS
probe's challenge token instead of the one just sent -- the reader returned the prior
turn's answer. wait_for_idle() now guards against both: it requires the extracted text
to be byte-identical across REPLY_SETTLE_SAMPLES consecutive reads,
REPLY_SETTLE_INTERVAL_S apart (module constants, env-overridable), AND different from
the reply already accepted for the previous turn on the same driver instance.

Run:  .venv\\Scripts\\python.exe -m pytest relay/test_reply_settle.py -q
"""
import os
import subprocess
import sys
from pathlib import Path

from relay import copilot_autopilot_relay as relay

REPO = Path(__file__).resolve().parent.parent


class _FakeClock:
    """A monotonically-advancing fake clock: sleep(s) just advances it. Lets the
    settle loop's real interval/dwell arithmetic be exercised deterministically and
    instantly, with no real wall-clock wait (the same monkeypatch("time.time", ...)
    idiom relay/test_network_recovery.py already uses)."""

    def __init__(self, start: float = 0.0):
        self.t = start

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


class _Answers:
    """Fake assistant-message locator: always reports exactly one block, so the
    'a new answer block appeared' phase of wait_for_idle passes immediately."""

    def count(self):
        return 1


class _Page:
    """Minimal page stand-in. wait_for_idle never touches this directly in these
    tests -- _answers/_is_generating/read_last_response are monkeypatched straight
    onto the driver instance, mirroring relay/test_response_independent_send.py."""

    def locator(self, selector):
        return _Answers()


def _make_driver(monkeypatch, replies, generating=False):
    """A real CopilotWebDriver whose read_last_response() pops from `replies` in
    order, repeating the LAST entry forever once exhausted (simulating a DOM that
    has stopped changing)."""
    driver = relay.CopilotWebDriver(_Page())
    driver._count_before = 0
    monkeypatch.setattr(driver, "_answers", lambda: _Answers())
    monkeypatch.setattr(driver, "_is_generating", lambda: generating)

    state = {"i": 0}

    def _read():
        i = min(state["i"], len(replies) - 1)
        state["i"] += 1
        return replies[i]

    monkeypatch.setattr(driver, "read_last_response", _read)
    return driver


def _use_fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr("time.time", clock.time)
    monkeypatch.setattr("time.sleep", clock.sleep)
    return clock


def test_growing_reply_returns_only_the_final_settled_text(monkeypatch):
    """A page whose text grows across samples: the reader must accept only the
    final settled text, never a prefix -- and specifically must not accept the
    exact truncated shape mined from the incident ('takeuchifile操作\\nリ')."""
    _use_fake_clock(monkeypatch)
    growing = [
        "takeuchifile操作\nリ",                                     # the mined truncation
        "takeuchifile操作\n関連ファイルを特定します。",
        "takeuchifile操作\n関連ファイルを特定します。作業完了。DONE",
    ]
    driver = _make_driver(monkeypatch, growing)

    ok = driver.wait_for_idle(timeout_s=120, dwell_s=1.0, appear_timeout_s=5)

    assert ok is True
    assert driver._last_returned_reply == growing[-1]
    assert driver._last_returned_reply != growing[0]
    assert driver._last_returned_reply != "takeuchifile操作\nリ"


def test_stale_repeat_of_previous_turn_is_not_accepted(monkeypatch):
    """A page whose text never changes from the previous turn's already-accepted
    reply must not be handed back as a fresh answer. wait_for_idle must report a
    distinct 'no new reply' outcome (False) instead of silently returning the old
    string as though it were this turn's answer."""
    _use_fake_clock(monkeypatch)
    stale = "previous probe answer token=abc123 DONE"
    driver = _make_driver(monkeypatch, [stale])
    driver._last_returned_reply = stale       # what the PREVIOUS turn already returned

    ok = driver.wait_for_idle(timeout_s=5, dwell_s=0.5, appear_timeout_s=5)

    assert ok is False
    # the driver's notion of "the accepted reply" must not have been silently
    # re-stamped with the same stale text -- it stays exactly what it already was.
    assert driver._last_returned_reply == stale


def test_new_reply_different_from_previous_turn_returns_promptly(monkeypatch):
    """A reply that is already complete and different from the previous turn must
    still be returned promptly -- not after burning anywhere near the full
    stability budget (timeout_s). Verified on the fake clock (call-count-equivalent
    elapsed time), never real wall time."""
    clock = _use_fake_clock(monkeypatch)
    old = "old turn's answer DONE"
    fresh = "brand new answer for this turn DONE"
    driver = _make_driver(monkeypatch, [fresh])   # stable from the very first read
    driver._last_returned_reply = old

    ok = driver.wait_for_idle(timeout_s=1800, dwell_s=1.0, appear_timeout_s=5)

    assert ok is True
    assert driver._last_returned_reply == fresh
    # nowhere near the 1800s timeout budget -- proven via the fake clock, not by
    # actually waiting.
    assert clock.t < 30.0


def test_settle_requires_multiple_samples_not_a_single_read(monkeypatch):
    """A single read must never be enough on its own: REPLY_SETTLE_SAMPLES (>=2)
    consecutive identical reads are required before acceptance, so a reply captured
    on exactly one instant (like the mined truncation) can never pass alone."""
    _use_fake_clock(monkeypatch)
    driver = _make_driver(monkeypatch, ["settled DONE"])
    calls = {"n": 0}
    real_read = driver.read_last_response

    def _counting_read():
        calls["n"] += 1
        return real_read()

    monkeypatch.setattr(driver, "read_last_response", _counting_read)

    ok = driver.wait_for_idle(timeout_s=60, dwell_s=0.0, appear_timeout_s=5)

    assert ok is True
    assert calls["n"] >= relay.REPLY_SETTLE_SAMPLES


def test_settle_constants_are_env_overridable():
    """MCP_REPLY_SETTLE_INTERVAL_S / MCP_REPLY_SETTLE_SAMPLES are read from the
    environment at import time (module constants, not hardcoded literals). Checked
    in a FRESH subprocess so this process's already-imported module is untouched."""
    env = dict(os.environ)
    env["MCP_REPLY_SETTLE_INTERVAL_S"] = "0.1"
    env["MCP_REPLY_SETTLE_SAMPLES"] = "5"
    code = (
        "from relay.copilot_autopilot_relay import ("
        "REPLY_SETTLE_INTERVAL_S, REPLY_SETTLE_SAMPLES); "
        "print(REPLY_SETTLE_INTERVAL_S, REPLY_SETTLE_SAMPLES)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(REPO), env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    interval_s, samples = result.stdout.split()
    assert float(interval_s) == 0.1
    assert int(samples) == 5

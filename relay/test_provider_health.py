"""Tests for relay/provider_health.py: state boundaries, standby, and recovery."""

import json
import os
import re

from relay import provider_health as ph


def _counters(recent, queued=0):
    return {"recent": list(recent), "queued": queued}


def test_ok_when_no_turns_and_no_waits():
    assert ph.provider_state(_counters([]), 0.0, 0, 1000.0)[0] == "ok"


def test_outage_at_exactly_the_consecutive_no_reply_limit():
    n = ph.OUTAGE_CONSECUTIVE_NO_REPLY
    state, reason = ph.provider_state(_counters([True] + [False] * n), 0.0, 0, 1000.0)
    assert state == "outage"
    assert str(n) in reason


def test_no_outage_one_below_the_consecutive_no_reply_limit():
    n = ph.OUTAGE_CONSECUTIVE_NO_REPLY - 1
    state, _ = ph.provider_state(_counters([True] + [False] * n), 0.0, 0, 1000.0)
    assert state != "outage"


def test_a_reply_resets_the_consecutive_count():
    recent = [False] * 5 + [True] + [False] * 5
    assert ph.provider_state(_counters(recent), 0.0, 0, 1000.0)[0] != "outage"


def test_outage_on_token_wait_above_limit_with_goals_queued():
    t = ph.OUTAGE_TOKEN_WAIT_S + 1
    state, reason = ph.provider_state(_counters([True], queued=2), t, 0, 1000.0)
    assert state == "outage"
    assert "token wait" in reason


def test_token_wait_exactly_at_limit_is_not_outage():
    state, _ = ph.provider_state(_counters([True], queued=2), ph.OUTAGE_TOKEN_WAIT_S, 0, 1.0)
    assert state == "ok"


def test_token_wait_above_limit_without_queued_goals_is_not_outage():
    state, _ = ph.provider_state(_counters([True], queued=0),
                                 ph.OUTAGE_TOKEN_WAIT_S + 1, 0, 1.0)
    assert state == "ok"


def test_degraded_below_half_over_the_last_ten_turns():
    w = ph.DEGRADED_WINDOW_TURNS
    # 4 of 10 = 40 percent, trailing no-reply run of 2 (below the outage limit).
    replies = [False, True, False, True, False, True, False, True, False, False]
    assert len(replies) == w
    state, _ = ph.provider_state(_counters(replies), 0.0, 0, 1.0)
    assert state == "degraded"


def test_ok_at_exactly_half_over_the_last_ten_turns():
    # 5 of 10 = 50 percent, not below. Alternating keeps the trailing no-reply run at one,
    # so only the ratio rule is being tested.
    replies = [False, True] * 5
    state, _ = ph.provider_state(_counters(replies), 0.0, 0, 1.0)
    assert state == "ok"


def test_no_degraded_check_before_a_full_window():
    # Nine turns, four of them replied, trailing run of one: the ratio is not checked yet.
    replies = [False, True] * 4 + [False]
    assert len(replies) == ph.DEGRADED_WINDOW_TURNS - 1
    state, _ = ph.provider_state(_counters(replies), 0.0, 0, 1.0)
    assert state == "ok"


def test_outage_takes_precedence_over_degraded():
    replies = [False] * (ph.DEGRADED_WINDOW_TURNS)
    state, _ = ph.provider_state(_counters(replies), 0.0, 0, 1.0)
    assert state == "outage"


def test_socket_host_matches_the_chathub_websocket_base():
    here = os.path.dirname(os.path.abspath(ph.__file__))
    with open(os.path.join(here, "chathub.py"), encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r'WS_BASE\s*=\s*"wss://([^/"]+)', text)
    assert m and m.group(1) == ph.SOCKET_HOST


def _clock(start=1000.0):
    box = {"t": start}
    return box, (lambda: box["t"])


def _mon(tmp_path, probe, clock):
    # probe_async=False folds each probe's result in on the same tick, so the pass counter is
    # deterministic in tests. The slow-probe test below keeps the async default on purpose.
    return ph.ProviderMonitor(state_dir=str(tmp_path), probe=probe, clock=clock,
                              probe_async=False)


def _enter_outage(mon, box):
    for _ in range(ph.OUTAGE_CONSECUTIVE_NO_REPLY):
        mon.note_sent()
    mon.tick(now=box["t"])
    assert mon.state == "outage"


def _arm_one_turn(mon, box):
    """Advance through enough passing probes to grant the one-turn allowance."""
    for _ in range(ph.PROBE_PASSES_REQUIRED):
        box["t"] += ph.PROBE_INTERVAL_S
        mon.tick(now=box["t"])


def test_no_turn_is_sent_while_in_outage(tmp_path):
    box, clock = _clock()
    mon = _mon(tmp_path, lambda: False, clock)
    _enter_outage(mon, box)
    sent_before = mon.counters["turns_sent"]
    # Probes keep failing: standby must block every send and count nothing.
    for _ in range(5):
        box["t"] += ph.PROBE_INTERVAL_S
        mon.tick(now=box["t"])
        assert mon.send_blocked() is True
    assert mon.counters["turns_sent"] == sent_before


def test_recovery_needs_a_real_reply_not_just_passing_probes(tmp_path):
    box, clock = _clock()
    mon = _mon(tmp_path, lambda: True, clock)
    _enter_outage(mon, box)
    _arm_one_turn(mon, box)
    # The allowance lets a turn through. send_blocked() is a pure report now: it returns the
    # same value when called twice and never consumes the allowance.
    assert mon.send_blocked() is False
    assert mon.send_blocked() is False
    idx = mon.note_sent()  # the turn really goes out here; this consumes the allowance
    # The turn went out and the state is still outage: no reply yet.
    mon.tick(now=box["t"])
    assert mon.state == "outage"
    # A reply moves the state back to ok.
    mon.note_reply(idx, ts=box["t"])
    assert mon.state == "ok"


def test_send_blocked_is_idempotent_and_note_sent_consumes_the_allowance(tmp_path):
    # Reproduces deadlock (b): the allowance must survive repeated send_blocked() checks and
    # only be spent by a real send (note_sent). Otherwise a check followed by an aborted send
    # loses the allowance with no turn sent.
    box, clock = _clock()
    mon = _mon(tmp_path, lambda: True, clock)
    _enter_outage(mon, box)
    _arm_one_turn(mon, box)
    for _ in range(5):
        assert mon.send_blocked() is False  # idempotent while allowed
    mon.note_sent()                         # a real send consumes it
    assert mon.send_blocked() is True       # now blocked again


def test_an_allowance_granted_but_never_sent_is_rearmed(tmp_path):
    # Reproduces deadlock (b), aborted-send variant: send_blocked() said "may send" but a later
    # gate aborted the send, so note_sent() never ran. Without re-arm the one-turn window would
    # never return. After PROBE_ALLOW_TIMEOUT_S the cycle re-arms and a new allowance is granted.
    box, clock = _clock()
    mon = _mon(tmp_path, lambda: True, clock)
    _enter_outage(mon, box)
    _arm_one_turn(mon, box)
    assert mon.send_blocked() is False       # allowance granted, but no note_sent() follows
    # Time passes with no send. A failing probe would not help; the allowance is stuck.
    box["t"] += ph.PROBE_ALLOW_TIMEOUT_S
    mon.tick(now=box["t"])                    # re-arm fires: allowance cleared
    assert mon.send_blocked() is True
    # A fresh set of passing probes grants a new allowance -- recovery is not permanently lost.
    _arm_one_turn(mon, box)
    assert mon.send_blocked() is False


def test_a_probe_turn_with_no_reply_is_rearmed(tmp_path):
    # Reproduces the no-reply deadlock: the one probe turn went out (note_sent set the reply
    # clock) but no reply arrived. Recovery needs a reply and none is coming, so after
    # PROBE_REPLY_TIMEOUT_S the cycle re-arms and can grant another probe turn.
    box, clock = _clock()
    mon = _mon(tmp_path, lambda: True, clock)
    _enter_outage(mon, box)
    _arm_one_turn(mon, box)
    assert mon.send_blocked() is False
    mon.note_sent()                          # probe turn goes out; reply clock starts
    mon.tick(now=box["t"])
    assert mon.state == "outage"             # no reply yet
    box["t"] += ph.PROBE_REPLY_TIMEOUT_S
    mon.tick(now=box["t"])                    # re-arm fires
    _arm_one_turn(mon, box)
    assert mon.send_blocked() is False       # a new probe turn is allowed


def test_a_slow_probe_never_blocks_tick(tmp_path):
    # Hypothesis (c): a TCP connect to an unreachable provider can block for the whole probe
    # timeout. tick() must never run the probe inline. With the production async path, a probe
    # that blocks for a long time must not delay tick(): tick() starts it on a thread and
    # returns at once.
    import threading as _th
    import time as _time
    release = _th.Event()

    def slow_probe():
        release.wait(30)   # would block tick() for up to 30s if run inline
        return True

    box, clock = _clock()
    mon = ph.ProviderMonitor(state_dir=str(tmp_path), probe=slow_probe, clock=clock)
    _enter_outage(mon, box)
    box["t"] += ph.PROBE_INTERVAL_S
    t0 = _time.monotonic()
    mon.tick(now=box["t"])      # starts the slow probe on a background thread
    elapsed = _time.monotonic() - t0
    assert elapsed < 2.0, "tick() blocked on the probe for %.2fs" % elapsed
    release.set()               # let the probe thread finish so it does not linger


def test_jsonl_write_goes_through_the_normal_get_monitor_path(tmp_path, monkeypatch):
    # The monitor must be observable through the ordinary creation path: get_monitor(state_dir)
    # creates it with that dir, and a state change writes one line to provider_health.jsonl.
    monkeypatch.setattr(ph, "_MONITOR", None, raising=False)
    box, clock = _clock()
    mon = ph.get_monitor(str(tmp_path))
    mon._clock = clock          # deterministic time for the transition
    mon._probe_async = False
    mon._probe = lambda: False
    _enter_outage(mon, box)
    path = os.path.join(str(tmp_path), ph.HEALTH_LOG_NAME)
    with open(path, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    assert any(r["from"] == "ok" and r["to"] == "outage" for r in rows)
    monkeypatch.setattr(ph, "_MONITOR", None, raising=False)


def test_state_change_is_written_as_one_jsonl_line(tmp_path):
    box, clock = _clock()
    mon = ph.ProviderMonitor(state_dir=str(tmp_path), probe=lambda: False, clock=clock)
    _enter_outage(mon, box)
    path = os.path.join(str(tmp_path), ph.HEALTH_LOG_NAME)
    with open(path, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    assert len(rows) == 1
    assert rows[0]["from"] == "ok" and rows[0]["to"] == "outage"
    assert "counters" in rows[0] and "reason" in rows[0]


def test_status_block_carries_state_reason_and_since(tmp_path):
    box, clock = _clock(500.0)
    mon = ph.ProviderMonitor(state_dir=str(tmp_path), probe=lambda: False, clock=clock)
    _enter_outage(mon, box)
    blk = mon.status_block()
    assert blk["provider_state"] == "outage"
    assert blk["provider_reason"]
    assert blk["provider_state_since"] == 500.0

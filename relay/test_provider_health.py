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


def _enter_outage(mon, box):
    for _ in range(ph.OUTAGE_CONSECUTIVE_NO_REPLY):
        mon.note_sent()
    mon.tick(now=box["t"])
    assert mon.state == "outage"


def test_no_turn_is_sent_while_in_outage(tmp_path):
    box, clock = _clock()
    mon = ph.ProviderMonitor(state_dir=str(tmp_path), probe=lambda: False, clock=clock)
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
    mon = ph.ProviderMonitor(state_dir=str(tmp_path), probe=lambda: True, clock=clock)
    _enter_outage(mon, box)
    for _ in range(ph.PROBE_PASSES_REQUIRED):
        box["t"] += ph.PROBE_INTERVAL_S
        mon.tick(now=box["t"])
    # Two passing probes allow exactly one turn.
    assert mon.send_blocked() is False
    assert mon.send_blocked() is True
    idx = mon.note_sent()
    # The turn went out and the state is still outage: no reply yet.
    mon.tick(now=box["t"])
    assert mon.state == "outage"
    # A reply moves the state back to ok.
    mon.note_reply(idx, ts=box["t"])
    assert mon.state == "ok"


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

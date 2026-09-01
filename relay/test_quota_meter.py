# -*- coding: utf-8 -*-
"""Measuring the thing the limiter actually counts.

WHAT WENT WRONG WITHOUT THIS. A dense run was refused 217 turns out of 237 and nobody could say
why. The instrumentation counted MCP tool calls through our own gateway -- the number we had,
rather than the number that matters -- and comparing it against Microsoft's published quota
produced a headroom figure wrong by an unknown factor. A turn may make no tool calls at all, or
several. The published quota counts GENERATIVE MESSAGES.

The reference line is 100 requests/minute for the Microsoft 365 Copilot users row, scoped per
Dataverse environment. It is a REFERENCE, not a guarantee: Microsoft states downstream services
may impose their own lower limits, so refusals arriving while the gauge looks comfortable are
the meter telling you the line is the wrong line.
"""
import json
import os

import pytest

from relay import quota_meter as Q


@pytest.fixture
def meter(tmp_path, monkeypatch):
    p = str(tmp_path / "quota.jsonl")
    monkeypatch.setattr(Q, "METER_PATH", p, raising=False)
    return p


def rows(path):
    return [json.loads(l) for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]


# -- what gets counted ---------------------------------------------------------------------

def test_a_turn_is_recorded_when_the_send_succeeds(meter):
    Q.record_turn(worker="w0", conv="c1")
    r = rows(meter)
    assert len(r) == 1 and r[0]["event"] == "turn" and r[0]["worker"] == "w0"


def test_the_unit_is_turns_not_tool_calls(meter):
    """THE ERROR THIS REPLACES. The old figure was MCP tool calls through our gateway, which the
    quota does not count. Ten tool calls inside one turn spend one message, not ten."""
    Q.record_turn(worker="w0")
    assert len(rows(meter)) == 1
    assert all(r["event"] == "turn" for r in rows(meter))


def test_the_three_refusal_classes_stay_apart(meter):
    """They are not interchangeable and the controller must not learn from them as if they
    were: a rate refusal is a capacity signal, a transport error is not, and a content refusal
    has nothing to do with load at all. The previous controller treated all three the same."""
    for kind in ("rate", "transport", "content"):
        Q.record_refusal(kind, worker="w0")
    snap = Q.snapshot(path=meter)
    assert snap["refusals_by_kind"] == {"rate": 1, "transport": 1, "content": 1}
    assert snap["refusals_5m"] == 3


# -- the gauge -----------------------------------------------------------------------------

def test_the_minute_window_is_a_minute(meter):
    now = 1_000_000.0
    for i in range(5):
        Q.record_turn(ts=now - 10 * i)          # inside the last minute
    for i in range(3):
        Q.record_turn(ts=now - 300 - i)         # five minutes ago
    snap = Q.snapshot(now=now, path=meter)
    assert snap["rpm"] == 5
    assert snap["rph"] == 8


def test_headroom_is_against_the_reference_line(meter, monkeypatch):
    monkeypatch.setattr(Q, "LIMIT_RPM", 100.0, raising=False)
    now = 1_000_000.0
    for i in range(30):
        Q.record_turn(ts=now - i)
    snap = Q.snapshot(now=now, path=meter)
    assert snap["rpm"] == 30
    assert snap["pct_rpm"] == pytest.approx(30.0)
    assert snap["headroom_rpm"] == pytest.approx(70.0)


def test_refusals_while_headroom_looks_fine_are_still_shown(meter, monkeypatch):
    """THE CASE THE GAUGE MUST NOT HIDE. The published limit is scoped per Dataverse
    environment and Microsoft says downstream services may impose lower ones. Refusals arriving
    under the line mean the line is wrong, and a gauge that derived safety from the line would
    report everything fine while the run was being refused."""
    monkeypatch.setattr(Q, "LIMIT_RPM", 100.0, raising=False)
    now = 1_000_000.0
    Q.record_turn(ts=now - 5)
    Q.record_refusal("rate", ts=now - 4)
    snap = Q.snapshot(now=now, path=meter)
    assert snap["headroom_rpm"] > 90        # looks comfortable
    assert snap["refusals_5m"] == 1         # and is not


def test_the_series_shows_a_burst_a_single_number_would_hide(meter):
    now = 1_000_000.0
    for i in range(40):
        Q.record_turn(ts=now - 1500 + i)    # 40 turns inside one minute, 25 minutes ago
    snap = Q.snapshot(now=now, path=meter)
    assert snap["rpm"] == 0                  # nothing in the last minute
    assert max(snap["series_rpm"]) >= 39     # but the burst is visible in the shape
    assert len(snap["series_rpm"]) == 60


def test_an_empty_meter_says_it_has_measured_nothing(meter):
    snap = Q.snapshot(path=meter)
    assert snap["measured"] is False and snap["rpm"] == 0


def test_an_unreadable_meter_does_not_raise(monkeypatch):
    monkeypatch.setattr(Q, "METER_PATH", "Z:/nope/quota.jsonl", raising=False)
    assert Q.snapshot()["measured"] is False


def test_recording_never_raises(monkeypatch):
    """It sits on the send path. A meter that can fail the turn it observes is worse than
    no meter."""
    monkeypatch.setattr(Q, "METER_PATH", "Z:/nope/quota.jsonl", raising=False)
    Q.record_turn(worker="w0")
    Q.record_refusal("rate")


# -- the derived concurrency ---------------------------------------------------------------

def test_sustainable_workers_is_derived_not_chosen(meter):
    snap = {"limit_rpm": 100.0}
    # 100 * 0.7 / 8.0 = 8.75, reported to one decimal for the gauge
    assert Q.sustainable_workers(snap, per_worker_rpm=8.0) == pytest.approx(8.8)


def test_it_refuses_to_divide_by_a_made_up_number(meter):
    """A made-up denominator is exactly how the previous estimate went wrong. With nothing
    measured, the honest answer is that there is no answer."""
    assert Q.sustainable_workers({"limit_rpm": 100.0}, per_worker_rpm=0) == 0.0
    assert Q.sustainable_workers({"limit_rpm": 100.0}, per_worker_rpm=None) == 0.0


# -- housekeeping ---------------------------------------------------------------------------

def test_old_records_are_pruned(meter):
    now = 1_000_000.0
    Q.record_turn(ts=now - 10)
    Q.record_turn(ts=now - Q.KEEP_S - 100)
    assert Q.prune(path=meter, now=now) == 1


def test_a_torn_line_does_not_lose_the_meter(meter):
    Q.record_turn(ts=1.0)
    with open(meter, "a", encoding="utf-8") as fh:
        fh.write('{"ts": 2.0, "event": "tur\n')
    Q.record_turn(ts=3.0)
    assert len(Q.read(meter)) == 2


# -- the wiring, which is the whole point ---------------------------------------------------

def test_the_meter_is_recorded_where_the_turn_is_actually_sent():
    """SOURCE-LEVEL, stated as such: driving a real turn needs a browser. What is asserted is
    that the record sits at the send, immediately after the turn counter increments -- not at
    admission. A worker holding a slot while it edits files and runs tests spends no quota, so
    admitting WORKERS was measuring the wrong thing; the turn is what the limiter counts."""
    import io
    import os
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(repo, "relay", "relay_fleet.py"), encoding="utf-8").read()
    i = src.index("self.turn += 1")
    window = src[i:i + 900]
    assert "quota_meter" in window, "the turn is no longer metered where it is spent"
    assert "record_turn" in window


def test_a_rate_refusal_is_recorded_as_its_own_class():
    import io
    import os
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(repo, "relay", "relay_fleet.py"), encoding="utf-8").read()
    i = src.index("note_upstream_throttle(now)")
    assert 'record_refusal("rate"' in src[i:i + 900]


def test_metering_cannot_fail_the_turn_it_observes():
    """Both call sites are wrapped. A meter that can take down a send is worse than no meter."""
    import io
    import os
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(repo, "relay", "relay_fleet.py"), encoding="utf-8").read()
    for anchor in ("record_turn", 'record_refusal("rate"'):
        i = src.index(anchor)
        before = src[max(0, i - 260):i]
        after = src[i:i + 260]
        assert "try:" in before and "except Exception:" in after, anchor

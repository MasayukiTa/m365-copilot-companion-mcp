# -*- coding: utf-8 -*-
"""The meter recorded everything the concurrency answer needs and never computed the answer.

`quota_meter.sustainable_workers(snap, per_worker_rpm)` says how many workers the reference line
supports, and its own docstring names the gap: *"the fleet's concurrency has been a number typed
on a command line; this is what the arithmetic says it should be."* It had no caller.

IT WAS NOT A FORGOTTEN CALL. `per_worker_rpm` had no default and `snapshot()` did not produce
one, so calling it with a snapshot alone returned 0.0 whatever the meter held -- the same shape
as `execution_profiles.validate_runtime`, whose INPUT does not exist. A caller would have had to
compute the denominator itself, and none did.

THE DENOMINATOR WAS ALREADY ON DISK. `record_turn` writes `worker` on every turn, and it has
done so for all 4,698 turn rows since 2026-09-01. Distinct workers per minute is a set size, not
an estimate. Measured on the busiest minute on record:

    66 turns / 56 distinct workers  ->  per_worker_rpm 1.18
    limit_rpm 100, headroom x0.7    ->  sustainable_workers 59.4

against a fleet whose concurrency was chosen by hand.

AND THE WINDOW BUG FOUND WHILE MEASURING IT. `rpm` counted `ts >= now - 60` with no upper
bound, so every row AFTER `now` counted as inside the minute. With `now=time.time()` there are
none, which is why it survived; with a past `now` -- the only reason the parameter exists --
`snapshot(now=<first row>)` reported **rpm 4698**. It reports 1.

WHAT IS DELIBERATELY NOT HERE: any change to admission. Capping concurrency from this number is
a policy change to a live control, and today already produced one threshold change that
measurement took back. The number and its reader are here; enforcing it is a decision.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import quota_meter as Q  # noqa: E402


def _meter(tmp_path, rows):
    import json

    p = tmp_path / "quota_meter.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(p)


def _turns(n, *, now, workers):
    """`n` turns inside the minute ending at `now`, spread over `workers` distinct names."""
    return [{"ts": now - 1 - (i % 50), "event": "turn", "worker": "w%d" % (i % workers)}
            for i in range(n)]


# ── the denominator the snapshot never produced ───────────────────────────────────────────

def test_the_snapshot_counts_the_workers_that_were_spending(tmp_path):
    now = 10_000.0
    snap = Q.snapshot(now=now, path=_meter(tmp_path, _turns(66, now=now, workers=56)))
    assert snap["rpm"] == 66
    assert snap["workers_1m"] == 56
    assert abs(snap["per_worker_rpm"] - 66 / 56.0) < 0.01


def test_the_answer_no_longer_needs_a_number_the_caller_has_to_invent(tmp_path):
    """THE DEFECT. With no default and no snapshot field, every call returned 0.0."""
    now = 10_000.0
    snap = Q.snapshot(now=now, path=_meter(tmp_path, _turns(66, now=now, workers=56)))
    n = Q.sustainable_workers(snap)
    assert n > 0, "still 0.0 with a fully measured snapshot"
    assert abs(n - round(snap["limit_rpm"] * 0.7 / snap["per_worker_rpm"], 1)) < 0.2


def test_an_explicit_rate_still_wins(tmp_path):
    """"What if each worker spent twice as much" is a real question; the argument stays."""
    now = 10_000.0
    snap = Q.snapshot(now=now, path=_meter(tmp_path, _turns(66, now=now, workers=56)))
    assert Q.sustainable_workers(snap, 2.0) == round(snap["limit_rpm"] * 0.7 / 2.0, 1)


def test_nothing_measured_still_means_no_answer(tmp_path):
    """A made-up denominator is how the last estimate went wrong -- its own docstring."""
    snap = Q.snapshot(now=10_000.0, path=_meter(tmp_path, []))
    assert snap["workers_1m"] == 0 and snap["per_worker_rpm"] == 0.0
    assert Q.sustainable_workers(snap) == 0.0


def test_turns_without_a_worker_name_do_not_inflate_the_denominator(tmp_path):
    """An empty name is one absent value, not one more worker -- and dividing by a phantom
    would UNDERSTATE the per-worker rate, which errs toward running too many."""
    now = 10_000.0
    rows = _turns(10, now=now, workers=5) + [{"ts": now - 5, "event": "turn", "worker": ""}]
    snap = Q.snapshot(now=now, path=_meter(tmp_path, rows))
    assert snap["workers_1m"] == 5


# ── the window, bounded at both ends ──────────────────────────────────────────────────────

def test_a_row_after_the_asked_for_moment_is_not_inside_the_minute(tmp_path):
    """THE BUG, in the shape it actually had. `>= now - 60` alone admits everything later,
    and `snapshot(now=<the first row>)` counted the whole file: rpm 4698."""
    now = 10_000.0
    rows = _turns(3, now=now, workers=3) + [
        {"ts": now + 600, "event": "turn", "worker": "future"},
        {"ts": now + 3600, "event": "turn", "worker": "later"},
    ]
    snap = Q.snapshot(now=now, path=_meter(tmp_path, rows))
    assert snap["rpm"] == 3, snap["rpm"]
    assert snap["rph"] == 3, snap["rph"]
    assert "future" not in str(snap)


def test_a_row_older_than_the_minute_is_still_in_the_hour(tmp_path):
    """The two windows are different questions and must not collapse into one."""
    now = 10_000.0
    rows = _turns(2, now=now, workers=2) + [
        {"ts": now - 600, "event": "turn", "worker": "wA"},
    ]
    snap = Q.snapshot(now=now, path=_meter(tmp_path, rows))
    assert snap["rpm"] == 2 and snap["rph"] == 3


# ── the reader ────────────────────────────────────────────────────────────────────────────

def test_the_reader_prints_the_answer(tmp_path, capsys, monkeypatch):
    """A number nobody prints is a number nobody acts on -- fourteen days of it here.

    The rows are written around the real clock rather than a fixed epoch, because `main()`
    asks `snapshot()` for NOW and a fixture in 1970 is simply outside the hour."""
    import time

    now = time.time()
    monkeypatch.setattr(Q, "METER_PATH", _meter(tmp_path, _turns(66, now=now, workers=56)))
    assert Q.main([]) == 0
    out = capsys.readouterr().out
    assert "sustainable workers" in out
    assert "56 active" in out, out


def test_the_reader_takes_a_what_if_rate(tmp_path, capsys, monkeypatch):
    import time

    now = time.time()
    monkeypatch.setattr(Q, "METER_PATH", _meter(tmp_path, _turns(66, now=now, workers=56)))
    assert Q.main(["--per-worker-rpm", "2"]) == 0
    assert "sustainable workers: 35.0" in capsys.readouterr().out


def test_an_empty_meter_says_so(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(Q, "METER_PATH", _meter(tmp_path, []))
    assert Q.main([]) == 0
    assert "no records" in capsys.readouterr().out

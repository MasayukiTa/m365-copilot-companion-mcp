# -*- coding: utf-8 -*-
"""`KEEP_S` said records older than two hours are dropped. Nothing dropped them.

    #: Records older than this are dropped when the file is rewritten. An hour of context is
    #: what the RPH figure needs; keeping more would make the meter itself the thing that fills
    #: a disk that HAS ALREADY STOPPED A RUN TONIGHT.
    KEEP_S = 7200.0

`prune()` is the rewriting, and it had no caller. Measured on the live meter 2026-09-14:

    4,845 rows spanning 12.8 DAYS   (oldest 2026-09-01 09:21)
    100% of them past the window

So the mitigation written for a real incident -- a full disk that stopped a run -- had never
run once, and the file it was meant to bound had been growing for a fortnight.

IT COSTS THE READERS TOO, which is the half that is not about disk. `read()` parses every line
and filters afterwards, and `snapshot()` uses it to answer a question about the last SIXTY
SECONDS. Two production call sites (`relay_fleet.py:255`, `fleet_runner.py:594`) therefore got
slower on every admission check, without bound, for as long as nobody pruned.

TRIGGERED ON THE OLDEST ROW. A byte threshold is a number somebody invents and a counter resets
with the process; "the first row is older than twice the window" is the policy restating itself,
and reading one line does not get slower as the file grows. Twice rather than once so an append
never rewrites a file that is merely at the edge of the window.
"""
from __future__ import annotations

import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import quota_meter as Q  # noqa: E402


def _seed(tmp_path, monkeypatch, ages_s):
    """A meter whose rows are `ages_s` seconds old, newest last. Returns (path, now)."""
    p = tmp_path / "quota_meter.jsonl"
    now = time.time()
    p.write_text("".join(
        json.dumps({"ts": now - a, "event": "turn", "worker": "w"}) + "\n" for a in ages_s),
        encoding="utf-8")
    monkeypatch.setattr(Q, "METER_PATH", str(p))
    return p, now


def _rows(p):
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]


# ── the retention enforces itself ─────────────────────────────────────────────────────────

def test_a_meter_older_than_the_window_is_pruned_by_the_next_record(tmp_path, monkeypatch):
    """THE DEFECT. On the live file every one of 4,845 rows was past the window."""
    p, now = _seed(tmp_path, monkeypatch, [3 * 86400 - i for i in range(300)])
    Q.record_turn(worker="w", ts=now)
    rows = _rows(p)
    assert len(rows) == 1, "the stale rows survived a record"
    assert now - float(rows[0]["ts"]) < Q.KEEP_S


def test_a_meter_inside_the_window_is_not_rewritten(tmp_path, monkeypatch):
    """Rewriting a file that is within its own policy is churn, and on the fleet's hot path."""
    p, now = _seed(tmp_path, monkeypatch, [3600 - i for i in range(50)])
    Q.record_turn(worker="w", ts=now)
    assert len(_rows(p)) == 51, "a fresh meter was rewritten anyway"


def test_the_threshold_is_twice_the_window_not_once(tmp_path, monkeypatch):
    """At exactly KEEP_S the file is at the edge of its policy, not past it. Pruning there would
    rewrite on almost every append during a busy hour."""
    p, now = _seed(tmp_path, monkeypatch, [Q.KEEP_S + 60])
    Q.record_turn(worker="w", ts=now)
    assert len(_rows(p)) == 2, "an edge-of-window meter was rewritten"

    p2, now2 = _seed(tmp_path, monkeypatch, [2 * Q.KEEP_S + 60])
    Q.record_turn(worker="w", ts=now2)
    assert len(_rows(p2)) == 1, "a meter past twice the window was not pruned"


def test_a_refusal_prunes_too(tmp_path, monkeypatch):
    """Both writers go through `_append`; putting the check on only one would leave a fleet
    that is being refused -- exactly when the meter matters -- unbounded."""
    p, now = _seed(tmp_path, monkeypatch, [3 * 86400])
    Q.record_refusal("rate", worker="w", ts=now)
    assert len(_rows(p)) == 1


# ── and it can never cost the caller a turn ───────────────────────────────────────────────

def test_a_broken_meter_never_raises_into_the_send_path(tmp_path, monkeypatch):
    """`record_turn` is called at the moment a send succeeds. Telemetry must not be able to
    fail a run -- a rule this repository has already paid for once."""
    p, now = _seed(tmp_path, monkeypatch, [3 * 86400])

    def boom(*a, **k):
        raise RuntimeError("cannot rewrite")

    monkeypatch.setattr(Q, "prune", boom)
    Q.record_turn(worker="w", ts=now)          # must not raise
    assert len(_rows(p)) == 2, "the row was lost when pruning failed"


def test_an_unreadable_first_line_is_not_a_crash(tmp_path, monkeypatch):
    """A truncated write leaves a partial line, and the check reads exactly that line."""
    p = tmp_path / "quota_meter.jsonl"
    p.write_text('{"ts": 1, "eve\n', encoding="utf-8")
    monkeypatch.setattr(Q, "METER_PATH", str(p))
    Q.record_turn(worker="w", ts=time.time())  # must not raise
    assert p.read_text(encoding="utf-8").count("\n") >= 2


def test_reading_the_oldest_row_does_not_read_the_file(tmp_path, monkeypatch):
    """THE REASON THE TRIGGER IS THE OLDEST ROW AND NOT A COUNT. This runs on every append, so
    it has to stay O(1) as the meter grows -- a rule that re-read the file to decide whether to
    rewrite it would be the cost it exists to remove."""
    p, now = _seed(tmp_path, monkeypatch, [3600 - i for i in range(2000)])
    reads = {"n": 0}
    real_read = Q.read

    def counted(*a, **k):
        reads["n"] += 1
        return real_read(*a, **k)

    monkeypatch.setattr(Q, "read", counted)
    Q.record_turn(worker="w", ts=now)
    assert reads["n"] == 0, "the append read the whole meter to decide not to prune it"

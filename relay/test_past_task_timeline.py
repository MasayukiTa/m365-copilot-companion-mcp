# -*- coding: utf-8 -*-
"""Which clock a finished task's timeline should read from.

THE DEFECT, AS THE OPERATOR SAW IT: expanding any past task showed the same three rows --
投入 / 開始 / 終了 -- with the same times, "not matching the actual task". Two separate causes,
and the second is the one that made the first hard to notice.

  1. 投入 and 開始 are both per-task and both correct, but the meta line and the first user turn
     are written within the same second, so at HH:mm resolution they always print the identical
     clock time. Two rows, one fact.

  2. 終了 read root["updated"], which is the RUN's last update. Measured on the last eight
     entries of .fleet/history.json: true ends 10:53, 10:53, 10:56, 10:57, 10:57, 10:59, 11:01,
     11:05 -- every one displayed as 11:05. Seven of eight wrong.

A timeline whose rows repeat each other reads as a template rather than as this task's history,
which is exactly why a wrong number in it went unremarked: identical output looks like the
widget's shape, not like a mistake about the data.

The rendering is C#. What is testable here is the RULE it now follows, against the real records,
which is where the wrongness was measurable in the first place.
"""
import io
import json
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY = os.path.join(REPO, ".fleet", "history.json")
STATUS = os.path.join(REPO, ".fleet", "status.json")

needs_records = pytest.mark.skipif(
    not (os.path.isfile(HISTORY) and os.path.isfile(STATUS)),
    reason="no local fleet records in this checkout")


def _load():
    return (json.load(io.open(HISTORY, encoding="utf-8")),
            json.load(io.open(STATUS, encoding="utf-8")))


def end_ts(entry, root, focused_count=1):
    """The rule FleetCockpit now follows for the 終了 marker.

    One task in view -> that task's own finish time. A live run's spine covers many workers,
    and there the run's clock is the right one.
    """
    if focused_count == 1:
        own = float(entry.get("ts") or 0)
        if own > 0:
            return own
    return float((root or {}).get("updated") or 0)


@needs_records
def test_the_old_rule_was_wrong_for_most_entries():
    """Guards against 'fixing' something that was never broken: if the run's clock had matched
    the tasks, this test fails and the change should be reverted rather than kept."""
    history, root = _load()
    entries = [e for e in history if e.get("ts")]
    if len(entries) < 4:
        pytest.skip("too few finished tasks recorded to say anything")
    run_updated = float(root.get("updated") or 0)
    wrong = [e for e in entries if abs(float(e["ts"]) - run_updated) >= 60.0]
    assert wrong, "the run clock matched every task; the old rule was not wrong here"
    assert len(wrong) >= len(entries) // 2, (
        "expected the run clock to be wrong for most tasks; %d of %d"
        % (len(wrong), len(entries)))


@needs_records
def test_the_new_rule_gives_each_task_its_own_end():
    history, root = _load()
    entries = [e for e in history if e.get("ts")][-8:]
    if len(entries) < 4:
        pytest.skip("too few finished tasks recorded")
    for e in entries:
        assert end_ts(e, root) == float(e["ts"]), e.get("name")
    distinct = {round(end_ts(e, root) / 60) for e in entries}
    assert len(distinct) > 1, "every task still ends at the same minute; the rule did not bind"


@needs_records
def test_a_multi_worker_spine_still_uses_the_run_clock():
    """The live run's panel covers every worker at once. There is no single task's end to show,
    and the run's own updated time is the honest answer."""
    history, root = _load()
    entries = [e for e in history if e.get("ts")]
    if not entries:
        pytest.skip("no finished tasks recorded")
    assert end_ts(entries[0], root, focused_count=5) == float(root.get("updated") or 0)


def test_a_missing_task_timestamp_falls_back_rather_than_showing_nothing():
    """A history entry written before this field existed has no ts. Showing an empty marker
    would be worse than showing the run's clock and being approximately right."""
    assert end_ts({}, {"updated": 1788161000.0}) == 1788161000.0
    assert end_ts({"ts": 0}, {"updated": 1788161000.0}) == 1788161000.0


def test_no_records_at_all_yields_zero_not_an_exception():
    """The marker is skipped when the value is 0; it must not raise on an empty run."""
    assert end_ts({}, {}) == 0
    assert end_ts({}, None) == 0


@needs_records
def test_queued_and_started_are_the_same_second_which_is_why_one_row_was_dropped():
    """The evidence for suppressing the duplicate 開始 marker. If these ever diverge -- a queue
    that actually waits -- the marker should come back, and this test says so by failing."""
    history, _root = _load()
    pairs = []
    for e in history[-8:]:
        path = e.get("transcript") or ""
        if not path or not os.path.isfile(path):
            continue
        meta = first = 0.0
        for line in io.open(path, encoding="utf-8", errors="replace"):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("meta"):
                meta = float(obj.get("ts") or 0)
                continue
            if obj.get("role") and obj.get("ts") and not first:
                first = float(obj["ts"])
                break
        if meta and first:
            pairs.append(first - meta)
    if not pairs:
        pytest.skip("no readable transcripts for the recorded tasks")
    assert max(pairs) < 60.0, (
        "queued and started now differ by up to %.0fs; the 開始 marker carries information "
        "again and should be restored in FleetCockpit's timeline" % max(pairs))

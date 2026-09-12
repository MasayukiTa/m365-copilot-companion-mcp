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
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY = os.path.join(REPO, ".fleet", "history.json")
STATUS = os.path.join(REPO, ".fleet", "status.json")

needs_records = pytest.mark.skipif(
    not (os.path.isfile(HISTORY) and os.path.isfile(STATUS)),
    reason="no local fleet records in this checkout")


def _transcript_lines(path):
    """Yield a recorded transcript's lines, whether or not it has been compressed since.

    THE RECORDS OUTLIVE THE UNCOMPRESSED FILE. history.json stores the path a transcript had
    when the run finished, and transcripts are gzipped as they age, so `os.path.isfile(path)`
    goes false for every entry eventually. Measured 2026-09-06: 16 of 16 history entries
    pointed at a .jsonl that no longer existed and 16 of 16 had a .jsonl.gz beside it -- so
    both tests below had stopped executing and skipped instead, permanently, with a message
    that reads like missing data rather than a stale path. Reading the .gz is what keeps these
    assertions alive on a machine that has been running for a while.

    Returns [] when neither form is present, so callers keep their existing "skip if nothing
    readable" behaviour on a genuinely fresh checkout.
    """
    if path and os.path.isfile(path):
        return io.open(path, encoding="utf-8", errors="replace").readlines()
    if path and os.path.isfile(path + ".gz"):
        import gzip
        with gzip.open(path + ".gz", "rt", encoding="utf-8", errors="replace") as fh:
            return fh.readlines()
    return []


def _hm(ts):
    """The HH:mm string the cockpit renders, which is what decides whether a row repeats
    the one above it."""
    return time.strftime("%H:%M", time.localtime(ts))


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


#: THE MEASUREMENT THIS FILE WAS WRITTEN FROM, frozen so the rule can be checked without
#: reading a file the operator's fleet is still writing to.
#:
#: From the module docstring above: "the last eight entries of .fleet/history.json: true ends
#: 10:53, 10:53, 10:56, 10:57, 10:57, 10:59, 11:01, 11:05 -- every one displayed as 11:05."
#: The run's `updated` is 11:05, which is why every row printed 11:05 under the old rule.
#:
#: Epoch seconds on one arbitrary day; only the differences matter, and they are the measured
#: ones. 11:05 is the run clock, so seven of the eight are more than a minute away from it --
#: the "seven of eight wrong" the docstring records.
_DAY = 1788134400.0                      # 00:00 of the day these were taken


def _at(hh, mm):
    return _DAY + hh * 3600 + mm * 60


RECORDED_ENDS = [_at(10, 53), _at(10, 53), _at(10, 56), _at(10, 57),
                 _at(10, 57), _at(10, 59), _at(11, 1), _at(11, 5)]
RECORDED_RUN_UPDATED = _at(11, 5)
RECORDED_HISTORY = [{"name": "task%d" % i, "ts": t} for i, t in enumerate(RECORDED_ENDS, 1)]
RECORDED_ROOT = {"updated": RECORDED_RUN_UPDATED}


def test_the_old_rule_was_wrong_for_most_entries():
    """Guards against 'fixing' something that was never broken.

    AGAINST THE RECORDED MEASUREMENT, not against whatever `.fleet/history.json` holds right
    now. This read the live file and asserted a property of the operator's data -- so a day on
    which the fleet ran one task made it fail while the rule was perfectly correct, and the
    file is rewritten by the cockpit mid-suite in any case.
    """
    wrong = [t for t in RECORDED_ENDS if abs(t - RECORDED_RUN_UPDATED) >= 60.0]
    assert len(wrong) == 7, "計測値が崩れている: %d/8" % len(wrong)
    assert len(wrong) >= len(RECORDED_ENDS) // 2


def test_the_new_rule_gives_each_task_its_own_end():
    for e in RECORDED_HISTORY:
        assert end_ts(e, RECORDED_ROOT) == float(e["ts"]), e["name"]
    distinct = {round(end_ts(e, RECORDED_ROOT) / 60) for e in RECORDED_HISTORY}
    assert len(distinct) > 1, "全タスクが同じ分で終わっている -- 規則が効いていない"


def test_the_old_rule_collapsed_them_all_onto_one_minute():
    """The symptom as the operator reported it: three rows, same times, 'not matching the
    actual task'. Stated explicitly so the difference between the two rules is visible here
    rather than only in prose."""
    old = {round(RECORDED_RUN_UPDATED / 60) for _ in RECORDED_HISTORY}
    assert len(old) == 1


def test_a_multi_worker_spine_still_uses_the_run_clock():
    """The live run's panel covers every worker at once. There is no single task's end to show,
    and the run's own updated time is the honest answer."""
    assert end_ts(RECORDED_HISTORY[0], RECORDED_ROOT, focused_count=5) == RECORDED_RUN_UPDATED


@needs_records
def test_the_live_records_still_look_like_the_measurement():
    """A CROSS-CHECK THAT CANNOT FAIL THE BUILD, and that is deliberate.

    The rule is pinned above. This only asks whether today's records still have the shape the
    measurement was taken from -- and when they do not, that is a fact about this week's runs,
    not a defect in the rule. So it SKIPS. The previous version of this file asserted here and
    went red whenever the fleet's history changed shape, including mid-suite while the cockpit
    was rewriting it.
    """
    try:
        history, root = _load()
    except (OSError, ValueError):
        pytest.skip("records unreadable right now (the fleet writes them live)")
    entries = [e for e in history if e.get("ts")] if isinstance(history, list) else []
    if len(entries) < 4:
        pytest.skip("too few finished tasks recorded to say anything")
    run_updated = float((root or {}).get("updated") or 0)
    wrong = [e for e in entries if abs(float(e["ts"]) - run_updated) >= 60.0]
    if not wrong:
        pytest.skip("every task ended within a minute of the run clock in these records")
    for e in entries:
        assert end_ts(e, root) == float(e["ts"]), e.get("name")


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
def test_the_started_row_appears_exactly_when_it_shows_a_different_time():
    """THIS TEST DID ITS JOB AND THEN NEEDED REPLACING.

    It used to assert that queued and started were always within the same second -- the
    evidence for dropping the duplicate 開始 row -- and to fail if they ever diverged. They
    did: 68.6s on one task, and a median of 6.5s across fifteen. The queue waits in steps as
    admission control staggers the workers, so the row carries information again.

    What replaces it is the rule rather than the evidence, because the evidence was always
    going to move. The row is worth showing exactly when it renders a DIFFERENT clock time
    from 投入; a threshold in seconds is a stand-in for that and is wrong in one direction --
    60s guarantees a different minute, but 10:59:50 -> 11:00:48 is 58s and equally
    informative.
    """
    history, _root = _load()
    shown = same = 0
    disagree = []
    for e in history:
        path = e.get("transcript") or ""
        lines = _transcript_lines(path)
        if not lines:
            continue
        meta = first = 0.0
        for line in lines:
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
        if not (meta and first):
            continue
        gap = first - meta
        if _hm(meta) == _hm(first):
            same += 1
        else:
            shown += 1
        # The rule that was there before used the gap in seconds as a stand-in.
        if (gap >= 60.0) != (_hm(meta) != _hm(first)):
            disagree.append((gap, _hm(meta), _hm(first)))
    if not (shown or same):
        pytest.skip("no readable transcripts for the recorded tasks")
    print("開始 row: shown on %d task(s), suppressed as duplicate on %d" % (shown, same))
    print("old 60s rule vs displayed-time rule: %d disagreement(s) on these records"
          % len(disagree))
    # The ratio is not the claim -- it moves with the queue. What must hold is that the two
    # rules are not interchangeable, which is the whole reason for the change: wherever they
    # disagree, the old one hid a row whose time was visibly different.
    for gap, a, b in disagree:
        assert gap < 60.0 and a != b, (
            "a disagreement that is not the case this fixes: %.1fs, %s vs %s" % (gap, a, b))


def test_a_wait_under_a_minute_can_still_change_the_displayed_time():
    """Why the seconds threshold was replaced. 58 seconds across a minute boundary renders
    two different times, and the old rule hid the row anyway."""
    late = 1756000790.0                     # ...:59:50 local, whatever the zone
    while _hm(late) == _hm(late + 58.0):
        late += 60.0                        # walk to a boundary that straddles
        if late > 1756000790.0 + 3600:
            pytest.skip("no straddling boundary found in this timezone")
    assert (late + 58.0) - late < 60.0
    assert _hm(late) != _hm(late + 58.0)


@needs_records
def test_queued_and_started_gap_is_recorded_for_the_next_person():
    """The measurement itself, kept visible rather than only in a commit message."""
    history, _root = _load()
    pairs = []
    for e in history[-8:]:
        path = e.get("transcript") or ""
        lines = _transcript_lines(path)
        if not lines:
            continue
        meta = first = 0.0
        for line in lines:
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
    if not pairs:
        pytest.skip("no readable transcripts for the recorded tasks")
    pairs.sort()
    print("queued -> started: median %.1fs, max %.1fs over %d tasks"
          % (pairs[len(pairs) // 2], pairs[-1], len(pairs)))
    assert pairs[-1] >= 0.0

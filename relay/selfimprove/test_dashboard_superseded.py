"""A re-measurement is not a second genome, and a correction is not an improvement.

A genome id is a content hash of the scaffold, so measuring the same genome twice appends a
second row with the same id -- archive.py states that the collision is deliberate. The feed
counted rows, so one genome measured twice read as two adopted genomes, and pass1_trend drew
a line from the first measurement to the second.

That is the live archive's actual state. It holds one genome measured at 0.34 and then at
0.50; the 0.34 was a grading-host artifact (19 instances silently produced no test output
under a concurrent grade) and the re-grade in isolation replaced it. The commit that landed
the correction says the dashboard would then show "0.34 -> 0.50", and it did -- a measurement
error rendered as a 16-point rise.

Run: pytest -q relay/selfimprove/test_dashboard_superseded.py
"""
import json

from relay.selfimprove.dashboard import _archive_sections


def _archive(tmp_path, rows):
    p = tmp_path / "entries.jsonl"
    with open(p, "w", encoding="utf-8", newline=chr(10)) as fh:
        for r in rows:
            fh.write(json.dumps(r) + chr(10))
    return str(p)


def _entry(gid, pass1, ts=None, parent=None):
    return {"id": gid, "parent_id": parent, "pass_at_1": pass1, "ts": ts,
            "gate_verdict": "measured", "descriptors": {"diff_bin": "broad"}}


THE_LIVE_CASE = [_entry("d7641b264e18", 0.34), _entry("d7641b264e18", 0.5)]


def test_one_genome_measured_twice_counts_as_one_genome(tmp_path):
    _, arc = _archive_sections(_archive(tmp_path, THE_LIVE_CASE))
    assert arc["count"] == 1
    assert len(arc["genomes"]) == 1


def test_the_row_shown_is_the_latest_measurement(tmp_path):
    _, arc = _archive_sections(_archive(tmp_path, THE_LIVE_CASE))
    assert arc["genomes"][0]["pass_at_1"] == 0.5


def test_having_measured_it_twice_is_not_hidden(tmp_path):
    """Deduplicating must not erase the fact. Both the per-genome count and the raw row
    count stay in the feed."""
    _, arc = _archive_sections(_archive(tmp_path, THE_LIVE_CASE))
    assert arc["genomes"][0]["measurements"] == 2
    assert arc["records"] == 2


def test_the_superseded_measurement_stays_in_the_trend_and_is_flagged(tmp_path):
    """Dropping it would be the other dishonesty: the loop did measure 0.34, and the record
    of having done so is what makes the correction auditable."""
    trend, _ = _archive_sections(_archive(tmp_path, THE_LIVE_CASE))
    assert [t["pass_at_1"] for t in trend] == [0.34, 0.5]
    assert [t["superseded"] for t in trend] == [True, False]


def test_distinct_genomes_are_all_kept(tmp_path):
    rows = [_entry("aaa", 0.3), _entry("bbb", 0.4), _entry("ccc", 0.5)]
    trend, arc = _archive_sections(_archive(tmp_path, rows))
    assert arc["count"] == 3
    assert [g["id"] for g in arc["genomes"]] == ["aaa", "bbb", "ccc"]
    assert all(t["superseded"] is False for t in trend)
    assert all(g["measurements"] == 1 for g in arc["genomes"])


def test_an_older_measurement_of_a_genome_listed_later_is_not_repeated(tmp_path):
    rows = [_entry("aaa", 0.3), _entry("bbb", 0.4), _entry("aaa", 0.6)]
    trend, arc = _archive_sections(_archive(tmp_path, rows))
    assert [g["id"] for g in arc["genomes"]] == ["bbb", "aaa"]
    assert [g["pass_at_1"] for g in arc["genomes"]] == [0.4, 0.6]
    assert [t["superseded"] for t in trend] == [True, False, False]


def test_a_missing_archive_still_degrades_to_the_same_shape(tmp_path):
    trend, arc = _archive_sections(str(tmp_path / "nope.jsonl"))
    assert trend == []
    assert arc == {"count": 0, "records": 0, "genomes": [], "qd_cells": 0}


def test_a_recorded_reason_reaches_the_screen(tmp_path):
    """The archive already says WHICH row a re-measurement replaces -- the id is a content
    hash, so an identical id is the statement. What it could not say was why, and the reason
    for the live correction survived only in a commit message."""
    rows = [dict(_entry("aaa", 0.34), note="grading host produced no test output for 19"),
            _entry("aaa", 0.5)]
    trend, _ = _archive_sections(_archive(tmp_path, rows))
    assert trend[0]["superseded"] is True
    assert trend[0]["note"] == "grading host produced no test output for 19"
    assert trend[1]["note"] is None


def test_a_note_is_optional(tmp_path):
    trend, _ = _archive_sections(_archive(tmp_path, THE_LIVE_CASE))
    assert all("note" in t for t in trend)
    assert all(t["note"] is None for t in trend)


def test_a_genome_whose_latest_measurement_is_past_the_cap_still_appears(tmp_path):
    """The cap used to be applied before the dedupe, while latest_at is computed over every
    row -- so a genome measured again past the 50th row matched nothing in the slice and
    vanished from the list while still being counted."""
    rows = [_entry("early", 0.1)]
    rows += [_entry("filler%d" % i, 0.2) for i in range(60)]
    rows += [_entry("early", 0.9)]          # the same genome, re-measured, past the cap
    trend, arc = _archive_sections(_archive(tmp_path, rows))
    ids = [g["id"] for g in arc["genomes"]]
    assert "early" in ids
    assert next(g for g in arc["genomes"] if g["id"] == "early")["pass_at_1"] == 0.9
    assert len(arc["genomes"]) <= 50

"""A measurement can record why it was taken.

The archive holds the same genome at 0.34 and at 0.50. The id is a content hash, so the
second row supersedes the first by construction -- the archive already says WHICH row was
replaced. It could not say WHY, and the reason (the first grade was corrupted by the grading
host and re-run in isolation) lived only in a commit message. A reader a month later saw two
measurements of one scaffold with no way to tell a correction from a real change.

Run: pytest -q relay/selfimprove/test_archive_note.py
"""
from relay.selfimprove.archive import Archive

GENOME = {"knobs": {"a": 1}, "cards": {"b": True}, "parent_id": None}


def _arc(tmp_path):
    return Archive(str(tmp_path / "entries.jsonl"))


def test_a_note_is_stored_with_the_measurement(tmp_path):
    arc = _arc(tmp_path)
    arc.add(GENOME, slice_ids=["i1"], pass_at_1=0.5, note="re-graded in isolation")
    assert arc.all()[0]["note"] == "re-graded in isolation"


def test_a_measurement_without_one_still_records(tmp_path):
    arc = _arc(tmp_path)
    arc.add(GENOME, slice_ids=["i1"], pass_at_1=0.5)
    assert arc.all()[0]["note"] is None


def test_the_note_is_not_written_into_the_descriptors(tmp_path):
    """Descriptors are behavioural coordinates and the QD map is built from them; prose there
    would invent cells."""
    arc = _arc(tmp_path)
    arc.add(GENOME, slice_ids=["i1"], pass_at_1=0.5,
            descriptors={"diff_bin": "broad"}, note="re-graded in isolation")
    e = arc.all()[0]
    assert e["descriptors"] == {"diff_bin": "broad"}
    assert "re-graded" not in str(e["descriptors"])

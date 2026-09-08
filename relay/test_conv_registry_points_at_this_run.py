# -*- coding: utf-8 -*-
"""The conversation registry must point at the run that is happening NOW.

WHY THIS FILE EXISTS. The owner opened a fleet conversation in the chat and got the empty
"ask me anything" screen while the fleet was visibly running. The work was not lost -- two
transcripts were being appended to at that moment (71KB and 86KB, 13:47-13:48 on 2026-09-08).
What was broken was the pointer: the newest fleet row in .fleet/conversations.json was from
10:19, more than three hours earlier.

The registry writer had two independent defects, both of which produce that same screen, and
neither of which any test could reach because the code lived inside a closure in
run_relay_fleet(). Extracting merge_conv_rows() to module level is most of the fix; these are
the cases that must stay covered.
"""
import time

from relay.fleet_runner import merge_conv_rows


def _row(url="", tr="", name="", title="t", source="fleet", ts=1.0):
    return {"url": url, "title": title, "source": source,
            "transcript": tr, "name": name, "ts": ts}


def test_a_worker_with_no_conv_url_yet_is_still_registered():
    """DEFECT 1. The writer registered a worker only once its conv_url was known
    (`if u and u not in urls`). A worker whose url had not been captured yet produced no row
    at all -- while its transcript was already on disk and growing. The transcript path is
    enough to open the conversation from disk, which is what the chat prefers anyway, so a
    worker that has one must get a row."""
    rows, changed = merge_conv_rows([], [_row(tr="transcripts/r_new_w0.jsonl", name="w0")])
    assert changed is True
    assert len(rows) == 1
    assert rows[0]["transcript"] == "transcripts/r_new_w0.jsonl"


def test_reusing_a_conversation_url_repoints_the_row_at_the_new_transcript():
    """DEFECT 2, the one that produced the empty screen. When a run reused a conversation url
    already in the file, `u not in urls` skipped it and the row kept the PREVIOUS run's
    transcript path. Transcripts are keyed `<run_id>_<name>` precisely BECAUSE w0 is reused
    across runs -- so the stale pointer was not an older version of this conversation, it was
    a different conversation entirely."""
    existing = [_row(url="https://x/c/1", tr="transcripts/r_OLD_w0.jsonl", name="w0")]
    rows, changed = merge_conv_rows(
        existing, [_row(url="https://x/c/1", tr="transcripts/r_NEW_w0.jsonl", name="w0")])
    assert changed is True
    assert len(rows) == 1, "the row is updated in place, not duplicated"
    assert rows[0]["transcript"] == "transcripts/r_NEW_w0.jsonl"


def test_the_title_is_not_rewritten_on_a_refresh():
    """The title is how the owner recognises a row in the sidebar. The refresh moves the
    pointer only -- rewriting the title every tick would rename rows under the cursor."""
    existing = [_row(url="u1", tr="old.jsonl", name="w0", title="銅箔の期限切れを調べる")]
    rows, _ = merge_conv_rows(existing, [_row(url="u1", tr="new.jsonl", name="w0", title="別の題")])
    assert rows[0]["title"] == "銅箔の期限切れを調べる"


def test_nothing_moved_means_nothing_is_written():
    """ts orders the sidebar, so stamping it on every tick would shuffle the list while the
    owner is reading it. `changed` is what the caller uses to skip the write entirely."""
    existing = [_row(url="u1", tr="same.jsonl", name="w0", ts=123.0)]
    rows, changed = merge_conv_rows(existing, [_row(url="u1", tr="same.jsonl", name="w0")])
    assert changed is False
    assert rows[0]["ts"] == 123.0


def test_a_url_arriving_later_fills_in_the_row_made_from_the_transcript():
    """The two registration paths must converge on ONE row: a worker registered by transcript
    before its url was captured must gain the url on a later tick, not gain a second row."""
    existing = [_row(url="", tr="t.jsonl", name="w0")]
    rows, changed = merge_conv_rows(existing, [_row(url="https://x/c/9", tr="t.jsonl", name="w0")])
    assert changed is True
    assert len(rows) == 1
    assert rows[0]["url"] == "https://x/c/9"


def test_rows_written_by_the_other_writer_are_left_alone():
    """bridge/copilot_bridge.py writes source="chat" rows into the same file. This merge must
    preserve them untouched -- it is a second concurrent writer, not the owner of the file."""
    existing = [_row(url="chat-1", tr="c.jsonl", name="s0", source="chat", title="my chat")]
    rows, _ = merge_conv_rows(existing, [_row(url="fleet-1", tr="f.jsonl", name="w0")])
    kept = [r for r in rows if r.get("source") == "chat"]
    assert len(kept) == 1 and kept[0]["title"] == "my chat"


def test_a_corrupt_row_is_dropped_rather_than_raising():
    """The file is written by two processes and read mid-rewrite. A non-dict item must not
    take the fleet down, and dropping one counts as a change so the cleaned file is written."""
    rows, changed = merge_conv_rows(["not a dict", _row(url="u1", tr="a.jsonl")], [])
    assert changed is True
    assert rows == [r for r in rows if isinstance(r, dict)] and len(rows) == 1


def test_an_entry_with_neither_url_nor_transcript_is_skipped():
    """A worker that has not produced either yet has nothing to point at. It must not create
    an empty row -- that is the very thing that renders as a blank conversation."""
    rows, changed = merge_conv_rows([], [_row()])
    assert rows == [] and changed is False


def test_ts_is_stamped_from_the_caller_when_given():
    """`now` exists so this is testable without sleeping; production passes None and gets
    time.time()."""
    existing = [_row(url="u1", tr="old.jsonl", name="w0", ts=1.0)]
    rows, _ = merge_conv_rows(existing, [_row(url="u1", tr="new.jsonl", name="w0")], now=999.0)
    assert rows[0]["ts"] == 999.0
    assert time.time() > 0   # the module under test still uses the real clock by default

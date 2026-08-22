"""Which refusals could have been about me -- asked of the log, not of a one-deep slot.

`matching_record` returned `read_state()`, and the slot holds exactly one refusal: whichever
landed last. Every reader that asked it "was I locked" was really asking "what happened most
recently anywhere", and the difference between those two questions has produced three separate
incidents, each patched on the symptom side:

  * one worker's refusal read as every worker's lock for a whole run
  * a 533-character meeting summary classified as a lock
  * a blank-ip refusal a remote caller could forge to blind detection

The unmeasured direction is the one these tests exist for: a context-less refusal arriving
AFTER a genuine one overwrites the slot, the reader filters it as "not about me", and concludes
not-locked while a real lock is standing. No test could have caught that while the evidence was
one record deep.

Run: pytest -q tools/test_lock_state_matching_records.py
"""
import json

import tools.lock_state as LS

NO_CTX = "[locked: no HTTP request context]"


def _tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(LS, "_LOG_FILE", tmp_path / "refusals.jsonl")
    monkeypatch.setattr(LS, "_STATE_FILE", tmp_path / "state.json")
    return tmp_path / "refusals.jsonl"


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline=chr(10)) as fh:
        for r in rows:
            fh.write(json.dumps(r) + chr(10))


def _refusal(ts, detail, ip=""):
    return {"event": "refused", "ts": ts, "client_ip": ip, "detail": detail, "site": "x"}


# ── the fault this replaces ─────────────────────────────────────────────────────────

def test_a_later_context_less_refusal_no_longer_hides_a_real_one(tmp_path, monkeypatch):
    """THE CASE THE SLOT COULD NOT REPRESENT. Both refusals are inside the turn; the genuine
    one came first. A reader looking at one record sees only the context-less one and calls
    the turn unlocked."""
    log = _tmp(tmp_path, monkeypatch)
    _write(log, [
        _refusal(100.0, "[locked client IP: '203.0.113.7'] ...", "203.0.113.7"),
        _refusal(101.0, NO_CTX + " Denied ..."),
    ])
    got = LS.matching_records(99.0, now=102.0)
    assert len(got) == 2
    assert any(not r["detail"].startswith(NO_CTX) for r in got), \
        "the genuine refusal must still be visible behind the context-less one"


def test_one_workers_refusal_does_not_become_evidence_for_an_earlier_turn(tmp_path, monkeypatch):
    log = _tmp(tmp_path, monkeypatch)
    _write(log, [_refusal(100.0, "[locked client IP: '203.0.113.7'] ...")])
    assert LS.matching_records(150.0, now=151.0) == []


def test_an_ancient_refusal_cannot_be_resurrected_by_a_clock_jump(tmp_path, monkeypatch):
    log = _tmp(tmp_path, monkeypatch)
    _write(log, [_refusal(100.0, "[locked client IP: 'x'] ...")])
    assert LS.matching_records(99.0, now=100.0 + LS.DEFAULT_FRESH_SEC + 1) == []


# ── reading a file several processes append to ──────────────────────────────────────

def test_a_missing_log_is_an_empty_answer_not_an_error(tmp_path, monkeypatch):
    _tmp(tmp_path, monkeypatch)
    assert LS.matching_records(1.0, now=2.0) == []


def test_a_torn_final_line_is_skipped(tmp_path, monkeypatch):
    log = _tmp(tmp_path, monkeypatch)
    _write(log, [_refusal(100.0, "[locked client IP: 'x'] ...")])
    with open(log, "a", encoding="utf-8", newline=chr(10)) as fh:
        fh.write('{"event": "refused", "ts": 101.0, "det')
    got = LS.matching_records(99.0, now=102.0)
    assert len(got) == 1


def test_the_readers_own_classification_rows_are_not_counted_as_refusals(tmp_path, monkeypatch):
    """record_classification writes to this same file. Counting those rows would let a
    reader's own note read back to it as fresh evidence of a lock."""
    log = _tmp(tmp_path, monkeypatch)
    _write(log, [
        {"event": "classified_locked", "ts": 100.0, "detail": "[locked client IP: 'x'] ..."},
    ])
    assert LS.matching_records(99.0, now=101.0) == []


def test_the_cost_of_reading_does_not_grow_with_the_log(tmp_path, monkeypatch):
    """A tail window, so a log that has been appended to all week still costs one read."""
    log = _tmp(tmp_path, monkeypatch)
    _write(log, [_refusal(50.0, "old padding " + "x" * 400) for _ in range(400)])
    _write(log, [_refusal(100.0, "[locked client IP: 'x'] ...")])
    assert log.stat().st_size > LS._LOG_TAIL_BYTES
    got = LS.matching_records(99.0, now=101.0)
    assert len(got) == 1 and got[0]["detail"].startswith("[locked client IP")


def test_the_window_never_yields_a_half_record(tmp_path, monkeypatch):
    log = _tmp(tmp_path, monkeypatch)
    _write(log, [_refusal(100.0, "y" * 500) for _ in range(400)])
    for r in LS.matching_records(99.0, now=101.0):
        assert r.get("event") == "refused" and "ts" in r


def test_rows_come_back_oldest_first(tmp_path, monkeypatch):
    log = _tmp(tmp_path, monkeypatch)
    _write(log, [_refusal(100.0, "a"), _refusal(101.0, "b"), _refusal(102.0, "c")])
    assert [r["detail"] for r in LS.matching_records(99.0, now=103.0)] == ["a", "b", "c"]


# ── the single-record helper still behaves ──────────────────────────────────────────

def test_matching_record_still_names_the_latest_one(tmp_path, monkeypatch):
    log = _tmp(tmp_path, monkeypatch)
    _write(log, [_refusal(100.0, "a"), _refusal(101.0, "b")])
    assert LS.matching_record(99.0, now=102.0)["detail"] == "b"


def test_matching_record_is_empty_when_nothing_matches(tmp_path, monkeypatch):
    _tmp(tmp_path, monkeypatch)
    assert LS.matching_record(99.0, now=102.0) == {}


def test_record_locked_writes_something_this_reader_can_find(tmp_path, monkeypatch):
    """End to end through the real writer, so the two halves cannot drift apart."""
    _tmp(tmp_path, monkeypatch)
    LS.record_locked("203.0.113.7", "[locked client IP: '203.0.113.7'] ...", ts=100.0)
    got = LS.matching_records(99.0, now=101.0)
    assert len(got) == 1 and got[0]["client_ip"] == "203.0.113.7"


def test_the_slot_is_no_longer_what_decides(tmp_path, monkeypatch):
    """The slot keeps working for the CLI and diagnostics; it is simply not the evidence."""
    _tmp(tmp_path, monkeypatch)
    LS.record_locked("203.0.113.7", "[locked client IP: 'x'] ...", ts=100.0)
    assert LS.read_state()["client_ip"] == "203.0.113.7"
    LS.clear()
    assert LS.read_state() == {}
    assert len(LS.matching_records(99.0, now=101.0)) == 1, \
        "clearing the slot must not erase the record of what happened"


def test_no_test_in_this_file_can_reach_the_real_records():
    """Same guard the sibling suites carry: these tests have wiped live records before."""
    src = open(__file__, encoding="utf-8").read()
    live = "." + "fleet"          # assembled, so this guard does not trip on its own text
    assert live not in src


def test_a_window_that_lands_on_a_line_boundary_keeps_that_record(tmp_path, monkeypatch):
    """The earlier version dropped the first line of the window to compensate for a torn
    record. When the cut lands exactly on a boundary that drop throws away a real refusal --
    and a torn line is not valid JSON, so the parse already handled the case it was for."""
    log = _tmp(tmp_path, monkeypatch)
    _write(log, [_refusal(50.0, "padding " + "x" * 300) for _ in range(20)])
    keep = [_refusal(100.0, "[locked client IP: 'a'] first"),
            _refusal(101.0, "[locked client IP: 'b'] second")]
    before = log.stat().st_size
    _write(log, keep)
    tail = log.stat().st_size - before          # the two records, whole lines, exactly
    monkeypatch.setattr(LS, "_LOG_TAIL_BYTES", tail)

    got = LS.matching_records(99.0, now=102.0)
    assert [r["detail"] for r in got] == [keep[0]["detail"], keep[1]["detail"]]

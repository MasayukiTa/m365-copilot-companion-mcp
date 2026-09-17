# -*- coding: utf-8 -*-
"""The rejection count raises the dot. This is what makes the dot worth acting on.

MEASURED 2026-09-17. The server dot went amber on four rejected /mcp calls. Everything the
instrument had kept was "four" and "127.0.0.1" -- and 127.0.0.1 is what EVERY caller looks like
here, because the devtunnel host forwards from localhost, so the address distinguished nothing.
Ten minutes later the sliding window rolled the count off and there was no record that it had
happened at all. The alarm fired correctly and the investigation had nothing to open.

That is the same shape as the rest of this day's work: an instrument whose success hid that
nobody could do anything with it. An alert that cannot be acted on teaches the person reading
it to clear it, and then the next one is cleared too.

So the rejections are kept durably, with the path and the user agent -- what actually separates
one caller from another -- and with nothing that was offered as a credential. A file recording
failed authentications is the last place a key should land, and a rejected key is still a key.
"""
from __future__ import annotations

import json
import time

import pytest

import tools.auth_stats as A


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_REJECTIONS_FILE", tmp_path / "auth_rejections.jsonl")
    monkeypatch.setattr(A, "_STATS_FILE", tmp_path / "auth_stats.json")
    return tmp_path


def test_a_rejection_outlives_the_window_that_raised_the_dot():
    """THE DEFECT. The count is a ten-minute window; the question comes later than that."""
    A.record_auth_failure(ts=1000.0, ip="127.0.0.1", path="/mcp",
                          agent="python-httpx/0.27.0")
    rows = A.recent_rejections()
    assert len(rows) == 1
    assert rows[0]["ts"] == 1000.0
    assert rows[0]["path"] == "/mcp"
    assert rows[0]["agent"] == "python-httpx/0.27.0"


def test_what_is_kept_can_tell_two_local_callers_apart():
    """The point of keeping anything. Both of these are 127.0.0.1 and they are not the same
    client; before this, the record said they were."""
    A.record_auth_failure(ts=1.0, ip="127.0.0.1", path="/mcp", agent="python-httpx/0.27.0")
    A.record_auth_failure(ts=2.0, ip="127.0.0.1", path="/mcp/messages",
                          agent="Mozilla/5.0 (Windows NT 10.0) Edge/120")
    rows = A.recent_rejections()
    assert len({(r["path"], r["agent"]) for r in rows}) == 2


def test_no_credential_is_written_even_when_one_was_offered():
    """A rejected key is still a key. record_auth_failure is not given the Authorization
    header and must not acquire a way to receive one."""
    A.record_auth_failure(ts=1.0, ip="127.0.0.1", path="/mcp",
                          agent="client/1.0 Authorization=Bearer-should-never-appear")
    text = (A._REJECTIONS_FILE).read_text(encoding="utf-8")
    row = json.loads(text.splitlines()[0])
    assert set(row) == {"ts", "ip", "path", "agent"}, row
    # The agent string is stored as given -- it is the caller's own label -- but nothing in
    # this module ever reads the header that carries the key.
    import inspect
    src = inspect.getsource(A)
    assert "authorization" not in src.lower().replace("authorization header", ""), \
        "this module gained a way to see the credential"


def test_the_record_is_bounded_and_keeps_the_recent_half():
    """Written from the request path, on a machine whose disk has been filled to zero once by a
    log nobody bounded. And when it is trimmed, the half worth keeping is the recent one: the
    question is always about the rejection that just happened."""
    A._REJECTIONS_FILE.write_text("x" * (A._REJECTIONS_MAX_BYTES + 10) + "\n", encoding="utf-8")
    for i in range(5):
        A.record_auth_failure(ts=float(i), ip="127.0.0.1", path="/mcp", agent="c/%d" % i)
    assert A._REJECTIONS_FILE.stat().st_size <= A._REJECTIONS_MAX_BYTES
    # The trim runs on the write that finds the file oversized, so the entry written just
    # before it is inside the discarded half. That is the bound doing its job, not a loss
    # worth preventing -- what must survive is the recent end.
    agents = [r["agent"] for r in A.recent_rejections()]
    assert agents[-4:] == ["c/1", "c/2", "c/3", "c/4"], agents
    assert "xxxx" not in A._REJECTIONS_FILE.read_text(encoding="utf-8"), \
        "the trimmed half is still there; the file is not actually bounded"


def test_recording_a_rejection_cannot_break_the_request():
    """It is bookkeeping on the request path. A caller must not be handed a 500 because the
    record could not be written."""
    A._REJECTIONS_FILE = A._REJECTIONS_FILE.parent / "no" / "such" / "dir" / "x.jsonl"
    A.record_auth_failure(ts=time.time(), ip="127.0.0.1", path="/mcp", agent="c")  # must not raise


def test_the_window_count_still_works_alongside_it():
    """The durable record is an addition, not a replacement. The dot still reads the window."""
    now = time.time()
    for _ in range(3):
        A.record_auth_failure(ts=now, ip="127.0.0.1", path="/mcp", agent="c")
    assert A.get_summary()["auth_fail_10m"] >= 3

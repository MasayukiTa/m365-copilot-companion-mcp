"""Tests for bridge.session_store -- hermetic, uses a temp dir via monkeypatch."""
import json
import os
import tempfile

import pytest

from bridge import session_store as ss


@pytest.fixture(autouse=True)
def temp_sess_dir(monkeypatch):
    """Redirect _base_dir() to a fresh temp directory for every test."""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(ss, "_base_dir", lambda: d)
        yield d


def test_create_load_roundtrip():
    sess = ss.new_session(title="hello")
    assert sess["title"] == "hello"
    assert sess["conv_url"] == ""
    assert sess["status"] == "active"
    assert sess["turns"] == 0
    assert sess["pending"] == []
    assert sess["transcript"] == "sessions/" + ss._sid_filename(sess["sid"], ".jsonl")
    assert sess["sid"].startswith("s")

    loaded = ss.load(sess["sid"])
    assert loaded == sess

    assert ss.load("nonexistent-sid") is None


def _set_last_active(sid, ts):
    """Place a session at a chosen point in the ordering, through the store's own API.

    This used to write the session file directly, behind the store's back. That stopped
    working when the store moved to SQLite -- and it should never have been necessary:
    needing to bypass the API to arrange a test is the API missing something.
    """
    ss.touch(sid, last_active_ts=ts)


def test_list_ordering():
    s1 = ss.new_session(title="first")
    _set_last_active(s1["sid"], 100.0)
    s2 = ss.new_session(title="second")
    _set_last_active(s2["sid"], 200.0)
    s3 = ss.new_session(title="third")
    _set_last_active(s3["sid"], 150.0)

    sessions = ss.list_sessions()
    sids_in_order = [s["sid"] for s in sessions]
    assert sids_in_order == [s2["sid"], s3["sid"], s1["sid"]]


def test_touch_merge():
    sess = ss.new_session(title="orig")
    old_ts = sess["last_active_ts"]

    updated = ss.touch(sess["sid"], conv_url="https://example.com/conv/1", status="idle")
    assert updated["conv_url"] == "https://example.com/conv/1"
    assert updated["status"] == "idle"
    assert updated["title"] == "orig"  # untouched fields preserved
    assert updated["last_active_ts"] >= old_ts

    reloaded = ss.load(sess["sid"])
    assert reloaded["conv_url"] == "https://example.com/conv/1"
    assert reloaded["status"] == "idle"


def test_transcript_meta_and_turn_numbering(temp_sess_dir):
    sess = ss.new_session(title="chat-title")
    sid = sess["sid"]

    ss.append_turn(sid, "user", "first message")
    ss.append_turn(sid, "assistant", "first reply")

    path = ss._transcript_path(sid)
    with open(path, "r", encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh.read().splitlines() if line.strip()]

    assert len(lines) == 3
    meta = lines[0]
    assert meta["meta"] is True
    assert meta["sid"] == sid
    assert meta["title"] == "chat-title"
    assert "ts" in meta

    turn1 = lines[1]
    assert turn1["turn"] == 1
    assert turn1["role"] == "user"
    assert turn1["text"] == "first message"
    assert "ts" in turn1

    turn2 = lines[2]
    assert turn2["turn"] == 2
    assert turn2["role"] == "assistant"
    assert turn2["text"] == "first reply"
    assert "ts" in turn2

    reloaded = ss.load(sid)
    assert reloaded["turns"] == 2


def test_fifo_queue_and_pop_including_empty():
    sess = ss.new_session()
    sid = sess["sid"]

    assert ss.pop_input(sid) is None

    ss.queue_input(sid, "one")
    ss.queue_input(sid, "two")
    ss.queue_input(sid, "three")

    assert ss.pop_input(sid) == "one"
    assert ss.pop_input(sid) == "two"
    assert ss.pop_input(sid) == "three"
    assert ss.pop_input(sid) is None

    reloaded = ss.load(sid)
    assert reloaded["pending"] == []


def test_invalid_sid_rejected_before_file_access(temp_sess_dir):
    assert ss.load("../outside") is None
    assert ss.pop_input("../outside") is None

    with pytest.raises(ValueError):
        ss.touch("../outside")
    with pytest.raises(ValueError):
        ss.queue_input("../outside", "nope")
    with pytest.raises(ValueError):
        ss.append_turn("../outside", "user", "nope")

    assert not os.path.exists(os.path.join(temp_sess_dir, "..", "outside.json"))


def test_latest_active_skips_empty_conv_url():
    s1 = ss.new_session(title="no-url")
    _set_last_active(s1["sid"], 300.0)  # newest but no conv_url

    s2 = ss.new_session(title="has-url")
    ss.touch(s2["sid"], conv_url="https://example.com/conv/2")
    _set_last_active(s2["sid"], 100.0)

    latest = ss.latest_active()
    assert latest is not None
    assert latest["sid"] == s2["sid"]

    # If nothing has a conv_url at all, expect None.
    ss.touch(s2["sid"], conv_url="")
    assert ss.latest_active() is None


def test_corrupt_json_tolerance_in_list_sessions(temp_sess_dir):
    good = ss.new_session(title="good")

    corrupt_path = os.path.join(temp_sess_dir, "sbad00000000dead.json")
    with open(corrupt_path, "w", encoding="utf-8") as fh:
        fh.write("{not valid json,,, ]")

    partial_path = os.path.join(temp_sess_dir, "spartial0000feed.json")
    with open(partial_path, "w", encoding="utf-8") as fh:
        fh.write('{"sid": "spartial0000feed"')  # truncated

    sessions = ss.list_sessions()
    sids = [s["sid"] for s in sessions]
    assert good["sid"] in sids
    assert len(sessions) == 1


def test_import_cleanly():
    import bridge.session_store  # noqa: F401

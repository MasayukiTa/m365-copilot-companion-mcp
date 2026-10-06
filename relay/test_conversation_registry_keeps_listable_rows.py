"""The conversation registry stops dropping rows the chat can still open, and stays small.

2026-10-05, "old conversations often cannot be opened from the main chat". Two of the three
causes live here (the third, the sidebar's startup-only cap and the 2 MB serializer ceiling, is
ui/test_the_conversation_list_shows_every_transcript.py):

  * relay/fleet_retention.conversations() dropped a row 24 h after nothing linked it to a
    session, while the transcript file was still on disk -- so the registry could not be a way
    back to an old conversation.
  * the file was never kept small: the full goal text of every row was 1.9 of its 2.4 million
    characters, which is what carried it past JavaScriptSerializer's 2,097,152 default.

Temporary directories only, never the live .fleet.
"""
import gzip
import io
import json
import os
import sqlite3
import time

from relay import fleet_retention as R
from relay import fleet_runner

DAY = 86400.0


def _store(d, sessions=()):
    os.makedirs(os.path.join(d, "sessions"), exist_ok=True)
    c = sqlite3.connect(os.path.join(d, "sessions", "sessions.sqlite3"))
    c.execute("CREATE TABLE sessions (sid TEXT PRIMARY KEY, conv_url TEXT)")
    c.executemany("INSERT INTO sessions VALUES (?,?)", list(sessions))
    c.commit()
    c.close()


def _transcript(d, name, age_h=100.0, gz=False, now=None):
    now = time.time() if now is None else now
    os.makedirs(os.path.join(d, "transcripts"), exist_ok=True)
    p = os.path.join(d, "transcripts", name + (".jsonl.gz" if gz else ".jsonl"))
    if gz:
        with gzip.open(p, "wb") as fh:
            fh.write(b'{"goal":"g"}\n')
    else:
        with io.open(p, "wb") as fh:
            fh.write(b'{"goal":"g"}\n')
    t = now - age_h * 3600.0
    os.utime(p, (t, t))
    return p


def _write(d, rows):
    with io.open(os.path.join(d, "conversations.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False)


def _read(d):
    return json.load(io.open(os.path.join(d, "conversations.json"), encoding="utf-8"))


def _row(d, name, ts, goal="", gz=False, transcript=True):
    r = {"url": "", "title": name, "source": "fleet", "name": "w0", "ts": ts, "goal": goal}
    if transcript:
        r["transcript"] = os.path.join(d, "transcripts", name + (".jsonl.gz" if gz else ".jsonl"))
    return r


# ------------------------------------------------------------ pruning

def test_a_row_is_kept_while_its_transcript_exists(tmp_path):
    d = str(tmp_path)
    now = time.time()
    _store(d)
    _transcript(d, "r1_a0_w0")
    _write(d, [_row(d, "r1_a0_w0", now - 30 * DAY)])
    _freed, dropped = R.conversations(d, now=now)
    assert dropped == [] and len(_read(d)) == 1


def test_a_gzipped_transcript_also_keeps_its_row(tmp_path):
    d = str(tmp_path)
    now = time.time()
    _store(d)
    _transcript(d, "r1_a0_w0", gz=True)
    # the row spells the plain name; retention compressed it afterwards
    _write(d, [_row(d, "r1_a0_w0", now - 30 * DAY, gz=False)])
    _freed, dropped = R.conversations(d, now=now)
    assert dropped == [] and len(_read(d)) == 1


def test_a_row_whose_transcript_is_gone_and_which_is_old_is_dropped(tmp_path):
    d = str(tmp_path)
    now = time.time()
    _store(d)
    _write(d, [_row(d, "r2_a0_w0", now - 30 * DAY)])          # no file written
    _freed, dropped = R.conversations(d, now=now)
    assert dropped == ["r2_a0_w0"] and _read(d) == []


def test_a_row_found_by_file_name_when_the_stored_path_is_another_checkouts(tmp_path):
    d = str(tmp_path)
    now = time.time()
    _store(d)
    _transcript(d, "r3_a0_w0")
    r = _row(d, "r3_a0_w0", now - 30 * DAY)
    r["transcript"] = "C:\\somewhere\\else\\.fleet\\transcripts\\r3_a0_w0.jsonl"
    _write(d, [r])
    assert R.conversations(d, now=now)[1] == []


def test_a_lineage_row_is_kept_if_any_segment_exists(tmp_path):
    d = str(tmp_path)
    now = time.time()
    _store(d)
    _transcript(d, "r4_a1_w0")
    r = _row(d, "r4_a0_w0", now - 30 * DAY)                    # first attempt: file gone
    r["transcripts"] = [r["transcript"], os.path.join(d, "transcripts", "r4_a1_w0.jsonl")]
    _write(d, [r])
    assert R.conversations(d, now=now)[1] == []


def test_a_row_inside_the_keep_window_is_still_kept_without_a_transcript(tmp_path):
    d = str(tmp_path)
    now = time.time()
    _store(d)
    _write(d, [_row(d, "r5_a0_w0", now - 3600)])
    assert R.conversations(d, now=now)[1] == []


# ------------------------------------------------------------ compaction and migration

def _big(d, n, now, goal_chars=2200, age_h=100.0):
    rows = []
    for i in range(n):
        name = "r%d_a0_w%d" % (i, i)
        _transcript(d, name, age_h=age_h, now=now)
        rows.append(_row(d, name, now - 100 * 3600, goal=("goal %d " % i) + "x" * goal_chars))
    return rows


def test_an_oversized_registry_is_compacted_under_a_megabyte_per_thousand_rows(tmp_path):
    d = str(tmp_path)
    now = time.time()
    _store(d)
    rows = _big(d, 1000, now)
    _write(d, rows)
    path = os.path.join(d, "conversations.json")
    before = os.path.getsize(path)
    assert before > 2 * 1024 * 1024, before      # past the ceiling the C# reader used to have
    R.conversations(d, now=now)
    after = os.path.getsize(path)
    assert after < 1024 * 1024, (before, after)
    got = _read(d)
    assert len(got) == 1000                      # every row survives: transcripts exist
    assert all(r["goal_cut"] and len(r["goal"]) == R.GOAL_COMPACT_CHARS for r in got)
    assert got[0]["goal"].startswith("goal 0 ")  # the head is kept: the sidebar still reads it


def test_the_previous_file_is_backed_up_once_and_only_once(tmp_path):
    d = str(tmp_path)
    now = time.time()
    _store(d)
    rows = _big(d, 5, now)
    _write(d, rows)
    original = io.open(os.path.join(d, "conversations.json"), encoding="utf-8").read()
    R.conversations(d, now=now)
    bak = os.path.join(d, "conversations.json.bak")
    assert io.open(bak, encoding="utf-8").read() == original
    # a second rewrite (new oversized row) must not replace the first backup
    more = _read(d) + _big(d, 1, now + 1)[:0] + [_row(d, "late", now - 100 * 3600, goal="y" * 900)]
    _transcript(d, "late", now=now)
    _write(d, more)
    R.conversations(d, now=now)
    assert io.open(bak, encoding="utf-8").read() == original


def test_compaction_is_idempotent_and_leaves_no_temp_file(tmp_path):
    d = str(tmp_path)
    now = time.time()
    _store(d)
    _write(d, _big(d, 3, now))
    R.conversations(d, now=now)
    first = io.open(os.path.join(d, "conversations.json"), encoding="utf-8").read()
    R.conversations(d, now=now)
    assert io.open(os.path.join(d, "conversations.json"), encoding="utf-8").read() == first
    assert not os.path.exists(os.path.join(d, "conversations.json.tmp"))


def test_a_row_whose_transcript_is_still_being_written_is_not_compacted(tmp_path):
    d = str(tmp_path)
    now = time.time()
    _store(d)
    _transcript(d, "live", age_h=0.01, now=now)
    _write(d, [_row(d, "live", now, goal="z" * 2000)])
    R.conversations(d, now=now)
    got = _read(d)
    assert len(got[0]["goal"]) == 2000 and "goal_cut" not in got[0]


def test_compaction_still_runs_when_the_session_store_cannot_be_read(tmp_path):
    """Dropping needs the session table (fail closed); shrinking does not."""
    d = str(tmp_path)
    now = time.time()
    _write(d, _big(d, 3, now))                                  # no sessions.sqlite3 at all
    R.conversations(d, now=now)
    got = _read(d)
    assert len(got) == 3 and all(r.get("goal_cut") for r in got)


def test_a_short_goal_and_a_chat_row_are_left_alone(tmp_path):
    d = str(tmp_path)
    now = time.time()
    _store(d, [("s1", "")])
    rows = [{"name": "s1", "title": "mine", "url": "", "ts": 0, "source": "chat"},
            _row(d, "short", now - 100 * 3600, goal="fix the thing")]
    _transcript(d, "short", now=now)
    _write(d, rows)
    R.conversations(d, now=now)
    assert _read(d) == rows


# ------------------------------------------------------------ the runner restores a cut goal

def test_a_live_worker_restores_the_full_goal_of_a_compacted_row():
    full = "the whole goal " * 40
    existing = [{"url": "", "title": "t", "source": "fleet", "transcript": "T.jsonl",
                 "goal": full[:240], "goal_cut": True, "ts": 1.0}]
    rows, changed = fleet_runner.merge_conv_rows(
        existing, [{"url": "", "transcript": "T.jsonl", "goal": full}], now=5.0)
    assert changed and rows[0]["goal"] == full and "goal_cut" not in rows[0]


def test_an_uncut_goal_is_still_never_overwritten():
    existing = [{"url": "", "title": "t", "source": "fleet", "transcript": "T.jsonl",
                 "goal": "original", "ts": 1.0}]
    rows, _ = fleet_runner.merge_conv_rows(
        existing, [{"url": "", "transcript": "T.jsonl", "goal": "different"}], now=5.0)
    assert rows[0]["goal"] == "original"

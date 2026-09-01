"""What the working-directory retention is allowed to remove.

Written against a temporary directory, never the live .fleet -- a test for a delete routine
that runs against the real store is the failure it is supposed to prevent.
"""
import io
import json
import os
import time

import pytest

from relay import fleet_retention as R


def _touch(path, size=64, age_days=0.0, now=None):
    now = time.time() if now is None else now
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "wb") as fh:
        fh.write(b"x" * size)
    t = now - age_days * 86400.0
    os.utime(path, (t, t))
    return path


def test_old_coordinator_logs_go(tmp_path):
    now = time.time()
    for i in range(30):
        _touch(str(tmp_path / ("coordinator_2026080%d_p%d.log" % (i % 9, i))),
               size=1000, age_days=40 + i, now=now)
    freed, removed = R.coordinator_logs(str(tmp_path), now=now)
    assert removed, "nothing was removed from 30 logs all older than the window"
    assert freed > 0


def test_the_newest_logs_survive_however_old_they_are(tmp_path):
    # A quiet fortnight must not leave the next investigation with nothing to read.
    now = time.time()
    for i in range(5):
        _touch(str(tmp_path / ("coordinator_x%d.log" % i)), age_days=400 + i, now=now)
    freed, removed = R.coordinator_logs(str(tmp_path), now=now, keep_min=20)
    assert removed == [], "the floor did not hold: %s" % removed
    assert freed == 0


def test_a_recent_log_is_never_removed(tmp_path):
    now = time.time()
    for i in range(40):
        _touch(str(tmp_path / ("coordinator_old%d.log" % i)), age_days=90, now=now)
    fresh = _touch(str(tmp_path / "coordinator_today.log"), age_days=0.1, now=now)
    R.coordinator_logs(str(tmp_path), now=now)
    assert os.path.exists(fresh)


def test_the_log_the_code_actually_reads_survives(tmp_path):
    # capture_budget.newest_log() takes max(mtime). Whatever else goes, THAT file must remain,
    # or the retention has broken the one reader it was checked against.
    now = time.time()
    for i in range(40):
        _touch(str(tmp_path / ("coordinator_%02d.log" % i)), age_days=100 - i, now=now)
    R.coordinator_logs(str(tmp_path), now=now)
    import glob
    left = glob.glob(str(tmp_path / "coordinator_*.log"))
    assert left, "every coordinator log was removed"
    newest_before = str(tmp_path / "coordinator_39.log")
    assert os.path.exists(newest_before)


def test_superseded_rotations_go_but_the_first_stays(tmp_path):
    _touch(str(tmp_path / "faulthandler.log"), size=500)
    _touch(str(tmp_path / "faulthandler.log.1"), size=500)
    _touch(str(tmp_path / "faulthandler.log.2"), size=500)
    _touch(str(tmp_path / "faulthandler.log.3"), size=500)
    freed, removed = R.rotations(str(tmp_path))
    assert sorted(removed) == ["faulthandler.log.2", "faulthandler.log.3"], removed
    assert os.path.exists(str(tmp_path / "faulthandler.log"))
    assert os.path.exists(str(tmp_path / "faulthandler.log.1"))
    assert freed == 1000


def test_scratch_ages_out_but_the_stores_are_not_entered(tmp_path):
    now = time.time()
    _touch(str(tmp_path / "_apply_cell_styles.py"), age_days=90, now=now)
    _touch(str(tmp_path / "_recent.py"), age_days=1, now=now)
    # a subdirectory whose files happen to start with an underscore: structured stores are
    # not scratch, and a name-prefix rule has no business reaching into them.
    keep = _touch(str(tmp_path / "sessions" / "_inside.json"), age_days=400, now=now)
    freed, removed = R.scratch(str(tmp_path), now=now)
    assert removed == ["_apply_cell_styles.py"], removed
    assert os.path.exists(str(tmp_path / "_recent.py"))
    assert os.path.exists(keep), "retention descended into a store directory"


def test_old_run_transcripts_age_out(tmp_path):
    now = time.time()
    old = _touch(str(tmp_path / "transcripts" / "r6a94_a0_w3.jsonl"), size=2000,
                 age_days=90, now=now)
    new = _touch(str(tmp_path / "transcripts" / "r7b00_a0_w1.jsonl"), size=2000,
                 age_days=2, now=now)
    freed, removed = R.stores(str(tmp_path), now=now)
    assert not os.path.exists(old)
    assert os.path.exists(new)
    assert freed == 2000


def test_the_store_rule_only_enters_directories_it_names(tmp_path):
    # An allow-list, not a deny-list: a rule that descends everywhere except what it remembers
    # to exclude fails open, and what it would eventually reach is the conversation database.
    now = time.time()
    keep = _touch(str(tmp_path / "sessions" / "old.jsonl"), age_days=400, now=now)
    keep2 = _touch(str(tmp_path / "guard" / "stack.jsonl"), age_days=400, now=now)
    R.stores(str(tmp_path), now=now)
    assert os.path.exists(keep), "the store rule entered the session directory"
    assert os.path.exists(keep2), "the store rule entered a directory it does not name"


def test_a_database_inside_a_named_store_is_still_refused(tmp_path):
    now = time.time()
    db = _touch(str(tmp_path / "transcripts" / "index.sqlite3"), size=4096,
                age_days=900, now=now)
    R.stores(str(tmp_path), now=now)
    assert os.path.exists(db)


def test_the_session_database_is_never_touched(tmp_path):
    # It has its OWN retention, with its own settings. Two policies on one file is how they
    # come to disagree about what is still live.
    db = _touch(str(tmp_path / "sessions" / "sessions.sqlite3"), size=5000, age_days=900)
    R.apply(fleet_dir=str(tmp_path))
    assert os.path.exists(db)
    assert os.path.getsize(db) == 5000


def test_a_capped_ledger_keeps_its_TAIL(tmp_path):
    # The recent end is the end every reader wants. Keeping the head would preserve the
    # bytes and lose the answer.
    p = str(tmp_path / "tool_events.jsonl")
    with io.open(p, "w", encoding="utf-8") as fh:
        for i in range(20000):
            fh.write(json.dumps({"i": i, "pad": "y" * 100}) + "\n")
    before = os.path.getsize(p)
    freed, trimmed = R.cap_jsonl(str(tmp_path), max_mb=0.5)
    assert trimmed == ["tool_events.jsonl"]
    assert os.path.getsize(p) < before
    lines = io.open(p, encoding="utf-8").read().splitlines()
    assert json.loads(lines[-1])["i"] == 19999, "the newest line did not survive"


def test_every_surviving_line_is_still_parsable(tmp_path):
    # Half a JSON object at the head of the file is a parse error for every reader, which is
    # worse than the bytes it saved.
    p = str(tmp_path / "activity.jsonl")
    with io.open(p, "w", encoding="utf-8") as fh:
        for i in range(20000):
            fh.write(json.dumps({"i": i, "pad": "z" * 100}) + "\n")
    R.cap_jsonl(str(tmp_path), max_mb=0.5)
    for ln in io.open(p, encoding="utf-8").read().splitlines():
        json.loads(ln)


def test_a_ledger_under_the_ceiling_is_left_exactly_alone(tmp_path):
    p = _touch(str(tmp_path / "small.jsonl"), size=1000)
    freed, trimmed = R.cap_jsonl(str(tmp_path), max_mb=64)
    assert trimmed == []
    assert os.path.getsize(p) == 1000


def test_a_dry_run_removes_nothing(tmp_path):
    now = time.time()
    for i in range(40):
        _touch(str(tmp_path / ("coordinator_%d.log" % i)), size=1000, age_days=100, now=now)
    _touch(str(tmp_path / "faulthandler.log.9"), size=1000)
    _touch(str(tmp_path / "_old.py"), size=1000, age_days=100, now=now)
    rep = R.apply(fleet_dir=str(tmp_path), now=now, dry_run=True)
    # In bytes, not rounded megabytes: 22 KB of small files rounds to 0.0 MB, which reads as
    # "no rule matched" -- the one thing a dry run has to be able to distinguish.
    assert rep["freed_bytes"] > 0, "a dry run that reports nothing cannot be reviewed"
    assert len(os.listdir(tmp_path)) == 42, "a dry run deleted something"


def test_a_missing_directory_is_not_an_error(tmp_path):
    rep = R.apply(fleet_dir=str(tmp_path / "nope"))
    assert rep["freed_mb"] == 0


# --------------------------------------------------------------------------- compression


def test_a_finished_log_is_compressed_not_deleted(tmp_path):
    now = time.time()
    p = str(tmp_path / "coordinator_old.log")
    with io.open(p, "w", encoding="utf-8") as fh:
        fh.write("captured: 3 min of token, agent A\n" * 20000)
    t = now - 3 * 86400
    os.utime(p, (t, t))
    before = os.path.getsize(p)
    saved, done = R.compress(str(tmp_path), now=now, keep_plain=0)
    assert not os.path.exists(p), "the original was left beside the archive"
    gz = p + ".gz"
    assert os.path.exists(gz)
    assert os.path.getsize(gz) < before / 5, "no meaningful compression"
    assert saved > 0
    import gzip
    assert gzip.open(gz, "rt", encoding="utf-8").read().count("captured:") == 20000


def test_the_newest_logs_are_never_compressed(tmp_path):
    # capture_budget.newest_log() reads the newest, and a live run is still appending to it.
    now = time.time()
    for i in range(5):
        p = str(tmp_path / ("coordinator_%d.log" % i))
        _touch(p, size=5000, age_days=10 + i, now=now)
    R.compress(str(tmp_path), now=now, keep_plain=3)
    plain = sorted(n for n in os.listdir(tmp_path) if n.endswith(".log"))
    assert len(plain) == 3, plain


def test_a_recent_log_is_never_compressed(tmp_path):
    now = time.time()
    p = _touch(str(tmp_path / "coordinator_live.log"), size=5000, age_days=0.01, now=now)
    R.compress(str(tmp_path), now=now, keep_plain=0, after_hours=6)
    assert os.path.exists(p), "a log written minutes ago was compressed"


def test_compression_keeps_the_original_mtime(tmp_path):
    # Every age rule here, and newest_log(), decide by mtime. A rewrite stamped "now" would
    # make the whole history look freshly written and exempt itself from every later rule.
    now = time.time()
    p = _touch(str(tmp_path / "coordinator_z.log"), size=5000, age_days=40, now=now)
    want = os.path.getmtime(p)
    R.compress(str(tmp_path), now=now, keep_plain=0)
    assert abs(os.path.getmtime(p + ".gz") - want) < 2


def test_a_compressed_log_is_still_reachable_by_the_retention(tmp_path):
    # Compression must not disable the deletion running beside it.
    now = time.time()
    for i in range(30):
        _touch(str(tmp_path / ("coordinator_%02d.log.gz" % i)), size=1000,
               age_days=100, now=now)
    freed, removed = R.coordinator_logs(str(tmp_path), now=now, keep_min=20)
    assert len(removed) == 10, removed


def test_the_budget_check_still_reads_a_compressed_log(tmp_path):
    # The reader, not the writer. This is the pair that breaks silently: newest_log() returning
    # None reads exactly like a run that captured nothing.
    import importlib, sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "scripts", "win"))
    cb = importlib.import_module("capture_budget")
    now = time.time()
    p = str(tmp_path / "coordinator_run.log")
    with io.open(p, "w", encoding="utf-8") as fh:
        fh.write("captured: 3 min of token, agent AGENT_X\nworker_done\n" * 5)
    t = now - 3 * 86400
    os.utime(p, (t, t))
    R.compress(str(tmp_path), now=now, keep_plain=0)
    newest = cb.newest_log(str(tmp_path))
    assert newest and newest.endswith(".gz"), newest
    got = cb.read_log(newest, elapsed_s=600)
    assert got and got["captures"] == 5, got
    assert got["socket_workers"] == 5

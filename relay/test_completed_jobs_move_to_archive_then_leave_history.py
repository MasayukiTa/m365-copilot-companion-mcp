"""Completed fleet jobs are compressed into .fleet/archive and then dropped from the
history list -- without ever losing a record, and without touching a job that is still
running, queued, or interrupted (resumable).

WHY THIS EXISTS. .fleet/history.json is the cockpit's archive of finished workers and it
had no ceiling: it stacked until a human pressed Clear. Measured 2026-10-29 it held 941
rows / 4.88 MB and kept growing, and the cockpit re-reads it every idle tick. The fix is a
retention rule (relay/fleet_retention.history_archive) that moves the OLDEST completed rows
out to a gzip file under .fleet/archive and trims history.json to the most recent `keep`,
so the list stops growing while every row ever recorded is still on disk.

THE INVARIANTS, pinned here so a future change cannot quietly weaken them:
  1. history.json + every archive file together still contain every original row (no loss).
  2. a row that is NOT a terminal/completed status is never archived and never dropped.
  3. dry_run changes nothing on disk.
  4. the archive file is gzip and round-trips to the exact rows that left history.json.
"""
from __future__ import annotations

import gzip
import json
import os

from relay import fleet_retention


# The statuses the fleet treats as terminal (mirror of relay.fleet_reaper.TERMINAL_STATUSES).
TERMINAL = ("done", "stuck", "maxturns", "error", "cancelled", "content_refused")
NON_TERMINAL = ("running", "ready", "pending", "interrupted", "freed")


def _row(i, status):
    return {"key": "run%d#w%d" % (i, i), "goal": "goal %d" % i, "status": status,
            "outcome": status.upper(), "turn": i, "ts": 1000 + i, "name": "w%d" % i}


def _write_history(fleet_dir, rows):
    with open(os.path.join(fleet_dir, "history.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)


def _read_history(fleet_dir):
    with open(os.path.join(fleet_dir, "history.json"), encoding="utf-8-sig") as f:
        return json.load(f)


def _archive_rows(fleet_dir):
    """Every row across every gzip archive file under .fleet/archive."""
    adir = os.path.join(fleet_dir, "archive")
    out = []
    if not os.path.isdir(adir):
        return out
    for name in sorted(os.listdir(adir)):
        if not name.endswith(".jsonl.gz"):
            continue
        with gzip.open(os.path.join(adir, name), "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def test_old_completed_rows_move_to_gzip_archive_and_leave_history(tmp_path):
    fleet_dir = str(tmp_path)
    rows = [_row(i, "done") for i in range(10)]
    _write_history(fleet_dir, rows)

    freed, moved = fleet_retention.history_archive(fleet_dir, keep=3)

    kept = _read_history(fleet_dir)
    assert len(kept) == 3, "history trimmed to the most recent keep rows"
    assert [r["key"] for r in kept] == ["run7#w7", "run8#w8", "run9#w9"]
    assert len(moved) == 7
    assert freed > 0

    archived = _archive_rows(fleet_dir)
    assert [r["key"] for r in archived] == ["run%d#w%d" % (i, i) for i in range(7)]

    # INVARIANT 1: nothing was lost -- archive + history reconstructs every original row.
    all_keys = sorted(r["key"] for r in archived) + sorted(r["key"] for r in kept)
    assert sorted(all_keys) == sorted(r["key"] for r in rows)


def test_a_running_or_interrupted_row_is_never_archived_or_dropped(tmp_path):
    fleet_dir = str(tmp_path)
    # 6 completed + a running + an interrupted, interleaved. keep=1 would drop a lot of the
    # completed ones, but the two live rows must survive in history regardless of position.
    rows = [
        _row(0, "done"), _row(1, "running"), _row(2, "done"), _row(3, "interrupted"),
        _row(4, "done"), _row(5, "done"), _row(6, "done"), _row(7, "done"),
    ]
    _write_history(fleet_dir, rows)

    fleet_retention.history_archive(fleet_dir, keep=2)

    kept = _read_history(fleet_dir)
    kept_keys = {r["key"] for r in kept}
    # the two non-terminal rows are still in history, never archived
    assert "run1#w1" in kept_keys, "a running row is never dropped"
    assert "run3#w3" in kept_keys, "an interrupted (resumable) row is never dropped"
    archived = _archive_rows(fleet_dir)
    archived_keys = {r["key"] for r in archived}
    assert "run1#w1" not in archived_keys
    assert "run3#w3" not in archived_keys
    for r in archived:
        assert r["status"] in TERMINAL, "only terminal rows are ever archived"


def test_dry_run_changes_nothing_on_disk(tmp_path):
    fleet_dir = str(tmp_path)
    rows = [_row(i, "done") for i in range(10)]
    _write_history(fleet_dir, rows)
    before = _read_history(fleet_dir)

    freed, moved = fleet_retention.history_archive(fleet_dir, keep=3, dry_run=True)

    assert freed == 0
    assert _read_history(fleet_dir) == before, "dry_run must not rewrite history.json"
    assert not os.path.isdir(os.path.join(fleet_dir, "archive")), "dry_run writes no archive"
    # it still REPORTS what it would move, so a dry run is informative
    assert len(moved) == 7


def test_under_the_cap_is_a_noop(tmp_path):
    fleet_dir = str(tmp_path)
    rows = [_row(i, "done") for i in range(3)]
    _write_history(fleet_dir, rows)

    freed, moved = fleet_retention.history_archive(fleet_dir, keep=50)

    assert freed == 0
    assert moved == []
    assert _read_history(fleet_dir) == rows


def test_missing_history_file_is_a_clean_noop(tmp_path):
    fleet_dir = str(tmp_path)
    freed, moved = fleet_retention.history_archive(fleet_dir, keep=3)
    assert (freed, moved) == (0, [])


def test_archive_round_trips_as_gzip_jsonl(tmp_path):
    fleet_dir = str(tmp_path)
    rows = [_row(i, "done") for i in range(5)]
    _write_history(fleet_dir, rows)

    fleet_retention.history_archive(fleet_dir, keep=1)

    adir = os.path.join(fleet_dir, "archive")
    names = [n for n in os.listdir(adir) if n.endswith(".jsonl.gz")]
    assert len(names) == 1, "one archive file per run"
    # it is genuinely gzip (magic bytes) and each line is one JSON object
    with open(os.path.join(adir, names[0]), "rb") as f:
        assert f.read(2) == b"\x1f\x8b", "gzip magic"
    archived = _archive_rows(fleet_dir)
    assert [r["key"] for r in archived] == ["run%d#w%d" % (i, i) for i in range(4)]


def test_apply_runs_history_archive_without_raising(tmp_path):
    """apply() wires history_archive into the rule set and never raises on it."""
    fleet_dir = str(tmp_path)
    rows = [_row(i, "done") for i in range(260)]
    _write_history(fleet_dir, rows)

    rep = fleet_retention.apply(fleet_dir=fleet_dir)

    assert "history_archive" in rep["rules"], "apply registers the history_archive rule"
    assert "error" not in rep["rules"]["history_archive"]
    # default keep is 200, so 260 rows -> 60 archived, 200 kept
    kept = _read_history(fleet_dir)
    assert len(kept) == 200

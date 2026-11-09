# -*- coding: utf-8 -*-
"""Archiving completed history rows must also keep them from coming back.

WHAT WAS WRONG. ``relay/fleet_retention.history_archive`` moved the oldest completed rows into a
gzip file under ``.fleet/archive`` and trimmed ``history.json`` to the most recent ``keep`` -- but
``history.json`` is not the source of truth. The cockpit rebuilds it at startup, and
``python -m tools.rebuild_history`` rebuilds it on demand, both from ``.fleet/socket_route.jsonl``,
which is APPEND-ONLY and still holds every ``worker_done``. So the trim freed disk for one tick
and the next rebuild refilled it: measured on 2026-11-05, 504 completed rows pulled out of
``history.json`` were back 45 seconds later.

THE FIX, AND WHY IT IS THE EXISTING MECHANISM. The cockpit's own 履歴を空にする already records a
watermark in ``.fleet/history_cleared.jsonl`` and ``tools.rebuild_history.cleared_through`` already
withholds any reconstructed row at or before it. ``history_archive`` now writes the same watermark
when it archives -- the max ts among the moved rows -- so a rebuild withholds exactly what was
archived and keeps exactly what stayed on screen. Nothing new is invented; the archive joins the
clear in speaking the one language the rebuild already reads.

THESE TESTS pin the join: archive, then rebuild from a socket ledger that still has every row, and
assert the archived rows do NOT return while the kept rows do.
"""
from __future__ import annotations

import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import fleet_retention  # noqa: E402
from tools import rebuild_history as R  # noqa: E402


def _write_history(fleet_dir, rows):
    with io.open(os.path.join(fleet_dir, "history.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)


def _row(i, status="done", ts=None):
    return {"key": "run%d#w%d" % (i, i), "goal": "goal %d" % i, "status": status,
            "outcome": status.upper(), "turn": i, "ts": (1000.0 + i) if ts is None else ts,
            "name": "w%d" % i}


def _socket_ledger(fleet_dir, rows):
    """A socket_route.jsonl whose worker_done rows mirror the history rows (same ts, same goal),
    so a rebuild from it would reconstruct every one of them -- which is the condition the
    watermark has to survive."""
    path = os.path.join(fleet_dir, "socket_route.jsonl")
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps({
                "event": "worker_done",
                "conv_client": r["key"],
                "goal": r["goal"],
                "ts": r["ts"],
                "worker": r["name"],
                "status": r["status"],
                "outcome": r["outcome"],
                "turns": r["turn"],
            }) + "\n")
    return path


def _cleared_log(fleet_dir):
    return os.path.join(fleet_dir, fleet_retention.HISTORY_CLEARED_LOG_NAME)


def _rebuild(fleet_dir):
    """Rebuild the way the cockpit and the CLI do: from the socket ledger, honouring the clear
    watermark in this store. Returns the reconstructed rows."""
    ledger = os.path.join(fleet_dir, "socket_route.jsonl")
    since = R.cleared_through(log=_cleared_log(fleet_dir),
                             kept_glob=os.path.join(fleet_dir, "history.json.cleared-*"))
    return R.build(socket_route=ledger, transcripts=os.path.join(fleet_dir, "none"), since=since)


def test_archived_rows_do_not_return_on_a_rebuild(tmp_path):
    """THE DEFECT, closed. Ten completed rows, keep=3: seven are archived and must not be rebuilt
    from the append-only socket ledger; the three kept must."""
    fleet_dir = str(tmp_path)
    rows = [_row(i) for i in range(10)]
    _write_history(fleet_dir, rows)
    _socket_ledger(fleet_dir, rows)

    freed, moved = fleet_retention.history_archive(fleet_dir, keep=3)
    assert len(moved) == 7 and freed > 0

    rebuilt = _rebuild(fleet_dir)
    keys = {r["resume_guid"] for r in rebuilt}
    # resume_guid is conv_client, which we set to the row key.
    assert keys == {"run7#w7", "run8#w8", "run9#w9"}, (
        "archived rows came back on rebuild: %s" % sorted(keys))
    assert rebuilt.withheld == 7


def test_the_watermark_is_the_max_ts_of_the_moved_rows(tmp_path):
    """The mark must sit exactly between what left and what stayed: at the newest archived row,
    so every archived row is withheld and every kept row survives."""
    fleet_dir = str(tmp_path)
    rows = [_row(i) for i in range(10)]
    _write_history(fleet_dir, rows)

    fleet_retention.history_archive(fleet_dir, keep=3)

    lines = [json.loads(l) for l in io.open(_cleared_log(fleet_dir), encoding="utf-8")
             if l.strip()]
    assert len(lines) == 1
    rec = lines[0]
    # oldest seven are ts 1000..1006; newest archived is 1006.
    assert rec["ts"] == 1006.0
    assert rec["rows"] == 7
    assert rec["by"] == fleet_retention.HISTORY_ARCHIVE_CLEAR_BY
    assert rec["iso"]  # a human-readable stamp is present


def test_no_watermark_is_written_when_nothing_is_archived(tmp_path):
    """Under the cap there is no move, so there must be no watermark -- an empty archive must not
    withhold anything on a later rebuild."""
    fleet_dir = str(tmp_path)
    rows = [_row(i) for i in range(3)]
    _write_history(fleet_dir, rows)

    freed, moved = fleet_retention.history_archive(fleet_dir, keep=50)
    assert (freed, moved) == (0, [])
    assert not os.path.exists(_cleared_log(fleet_dir)), \
        "a no-op archive wrote a clear watermark"


def test_dry_run_writes_no_watermark(tmp_path):
    """dry_run changes nothing on disk, the watermark included."""
    fleet_dir = str(tmp_path)
    rows = [_row(i) for i in range(10)]
    _write_history(fleet_dir, rows)

    fleet_retention.history_archive(fleet_dir, keep=3, dry_run=True)
    assert not os.path.exists(_cleared_log(fleet_dir))


def test_the_watermark_is_jsonl_no_bom_and_appends(tmp_path):
    """Same shape the cockpit and tools.rebuild_history already read: one JSON object per line,
    UTF-8 with no BOM, appended (a prior clear is not overwritten)."""
    fleet_dir = str(tmp_path)
    # a pre-existing clear from the cockpit
    with io.open(_cleared_log(fleet_dir), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"ts": 1.0, "iso": "x", "rows": 1,
                             "by": "cockpit/clear_history"}) + "\n")
    rows = [_row(i) for i in range(10)]
    _write_history(fleet_dir, rows)

    fleet_retention.history_archive(fleet_dir, keep=3)

    raw = io.open(_cleared_log(fleet_dir), "rb").read()
    assert not raw.startswith(b"\xef\xbb\xbf"), "a BOM would corrupt the first key"
    lines = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
    assert len(lines) == 2, "the archive appended rather than overwriting the prior clear"
    assert lines[0]["by"] == "cockpit/clear_history"
    assert lines[1]["by"] == fleet_retention.HISTORY_ARCHIVE_CLEAR_BY


def test_a_later_clear_still_wins_over_an_archive_watermark(tmp_path):
    """The two writers coexist: cleared_through takes the newest entry whichever wrote it, so a
    human clear after an archive still withholds everything up to the clear."""
    fleet_dir = str(tmp_path)
    rows = [_row(i) for i in range(10)]
    _write_history(fleet_dir, rows)
    _socket_ledger(fleet_dir, rows)

    fleet_retention.history_archive(fleet_dir, keep=3)  # watermark at ts 1006
    # a human presses Clear afterwards, discarding what was left on screen too (ts up to 1009)
    with io.open(_cleared_log(fleet_dir), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"ts": 1009.0, "iso": "y", "rows": 3,
                             "by": "cockpit/clear_history"}) + "\n")

    rebuilt = _rebuild(fleet_dir)
    assert list(rebuilt) == [], "a clear after the archive must withhold the whole store"
    assert rebuilt.withheld == 10

# -*- coding: utf-8 -*-
"""The operator emptied the fleet history and a rebuild put all 3,469 rows back.

WHY IT COULD. An absent `.fleet/history.json` has two causes that look identical on disk -- the
file was lost, or 履歴を空にする was pressed -- and `tools/rebuild_history.py` was written for the
first. The rebuild is perfect because `.fleet/socket_route.jsonl` is append-only and never stops
holding a conversation, so the ledger the repair reads from is exactly the ledger the clear could
not touch. The cockpit was already moving the file aside instead of deleting it, which protected
the BYTES and not the DECISION; a rebuild reads neither.

WHAT THE OPERATOR ASKED FOR, in their words: 履歴を参照できるのはフリートで履歴を空にするまで。
メインチャットではそのチャットを削除操作するまで。フリートで履歴を空にしたときにメインチャット
からも消えるのはだめね。Two lifetimes, ended by two different actions, neither one ending the
other. This file pins the first sentence and the third; the second belongs to the chat window.

THE WATERMARK IS THE CLEAR'S WALL TIME, and that is sound rather than convenient: everything a
clear discarded had already finished when the button was pressed, so "older than the press" is
the same set as "was on screen at the press". Anything newer is a conversation the clear never
saw and must survive -- otherwise a single clear would go on erasing the future, which is a worse
bug than the one being fixed here.
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import rebuild_history as R  # noqa: E402

COCKPIT = os.path.join(REPO, "ui", "FleetCockpit.cs")


def _ledger(tmp_path, rows):
    """A socket_route.jsonl holding `rows` as (ts, guid) pairs."""
    path = tmp_path / "socket_route.jsonl"
    with io.open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        for ts, guid in rows:
            fh.write(json.dumps({"event": "worker_done", "conv_client": guid,
                                 "goal": "goal " + guid, "ts": ts, "worker": "w1",
                                 "status": "done", "outcome": "ok", "turns": 1}) + "\n")
    return str(path)


# ── the clear is honoured ─────────────────────────────────────────────────────────────────

def test_rows_from_before_a_clear_are_not_rebuilt(tmp_path):
    """THE DEFECT, in one assertion."""
    ledger = _ledger(tmp_path, [(1000.0, "a"), (1100.0, "b")])
    log = tmp_path / "history_cleared.jsonl"
    log.write_text(json.dumps({"ts": 1200.0, "rows": 2}) + "\n", encoding="utf-8")

    rows = R.build(socket_route=ledger, transcripts=str(tmp_path / "none"),
                   since=R.cleared_through(log=str(log), kept_glob=str(tmp_path / "nothing-*")))
    assert list(rows) == [], "a cleared conversation was rebuilt"
    assert rows.withheld == 2, "the count of what was withheld is how anyone finds out"


def test_a_conversation_that_happened_after_the_clear_survives_it(tmp_path):
    """The other half, and the one that makes the watermark safe to keep forever: a clear ends
    the history that existed, not the fleet."""
    ledger = _ledger(tmp_path, [(1000.0, "old"), (1300.0, "new")])
    log = tmp_path / "history_cleared.jsonl"
    log.write_text(json.dumps({"ts": 1200.0}) + "\n", encoding="utf-8")

    rows = R.build(socket_route=ledger, transcripts=str(tmp_path / "none"),
                   since=R.cleared_through(log=str(log), kept_glob=str(tmp_path / "nothing-*")))
    assert [r["resume_guid"] for r in rows] == ["new"]
    assert rows.withheld == 1


def test_the_file_the_cockpit_moved_aside_is_read_as_a_clear_too(tmp_path):
    """THE CLEARS THAT ALREADY HAPPENED. The log above did not exist when the 2026-09-13 clear
    was made, and a fix that only honoured clears recorded after the fix would leave that one
    resurrectable -- the very rows this is about. The name the cockpit renames to carries the
    time, so it is a second source, and it is why `--dry-run` reported 3,469 withheld on a
    repository whose log was still empty."""
    (tmp_path / "history.json.cleared-20260914-075653").write_text("[]", encoding="utf-8")
    got = R.cleared_through(log=str(tmp_path / "no-log.jsonl"),
                            kept_glob=str(tmp_path / "history.json.cleared-*"))
    assert time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(got)) == "2026-09-14 07:56:53"


def test_the_latest_clear_wins_whichever_source_it_came_from(tmp_path):
    """Both directions, because the two sources overlap from now on -- every clear writes the log
    AND leaves a renamed file -- and the answer has to be the most recent one either way.

    THE FIRST DRAFT OF THIS TEST WAS WRONG in a way worth keeping written down: it put epoch
    4000 and 9000 in the log against a file stamped 20260101, and asserted the log won. The
    filename won, correctly -- 1970 is not later than 2026 -- and the code was right while the
    fixture was comparing two different eras. Times in a fixture about which time is later have
    to be times.
    """
    jan = R._stamp_to_ts("20260101-000000")
    (tmp_path / "history.json.cleared-20260101-000000").write_text("[]", encoding="utf-8")
    log = tmp_path / "history_cleared.jsonl"

    log.write_text(json.dumps({"ts": jan + 86400}) + "\n", encoding="utf-8")
    assert R.cleared_through(log=str(log),
                             kept_glob=str(tmp_path / "history.json.cleared-*")) == jan + 86400

    log.write_text(json.dumps({"ts": jan - 86400}) + "\n", encoding="utf-8")
    assert R.cleared_through(log=str(log),
                             kept_glob=str(tmp_path / "history.json.cleared-*")) == jan


def test_never_cleared_means_rebuild_everything(tmp_path):
    """The repair this tool was written for still works. A repository that has never had a clear
    must not have its history withheld by a watermark that does not exist."""
    ledger = _ledger(tmp_path, [(1000.0, "a")])
    assert R.cleared_through(log=str(tmp_path / "absent.jsonl"),
                             kept_glob=str(tmp_path / "absent-*")) == 0.0
    rows = R.build(socket_route=ledger, transcripts=str(tmp_path / "none"), since=0.0)
    assert [r["resume_guid"] for r in rows] == ["a"]


def test_the_watermark_belongs_to_the_store_the_ledger_came_from(tmp_path):
    """A DEFECT THIS FILE CAUSED AND CAUGHT. The first version read the repository's own .fleet
    no matter which ledger it was handed, so a clear made on this machine withheld the rows of
    every synthetic ledger any test built -- nine tests in tools/test_a_lost_archive_can_be_
    rebuilt.py went red at once, all of them describing a rebuild that was working fine.

    Both halves are asserted through the DEFAULT argument, because the default is where the bug
    was: a store with no clear rebuilds everything, and a clear in THAT store is honoured."""
    ledger = _ledger(tmp_path, [(1000.0, "a")])
    rows = R.build(socket_route=ledger, transcripts=str(tmp_path / "none"))
    assert [r["resume_guid"] for r in rows] == ["a"], (
        "a clear somewhere else silenced this store")

    (tmp_path / "history.json.cleared-20260914-075653").write_text("[]", encoding="utf-8")
    assert list(R.build(socket_route=ledger, transcripts=str(tmp_path / "none"))) == []


def test_the_clear_markers_are_names_rather_than_paths(tmp_path):
    """WHY THEY ARE NOT `os.path.join(FLEET, ...)`, which is how they were first written.

    Nothing uses them as a path -- the directory always comes from the caller, derived from the
    store the ledger was read from. Spelt as full paths they read as "the operator's clear log"
    and tripped relay/test_live_record_isolation.py, which requires every module-level constant
    naming .fleet to be redirected for tests or declared safe. Neither answer fits a value that
    is only ever a basename: redirecting it would change the FILENAME and leave the directory
    exactly where it was. The gate was right and the constant was wrong.
    """
    for name in (R.CLEARED_LOG_NAME, R.CLEARED_KEPT_GLOB_NAME):
        assert os.sep not in name and "/" not in name, (
            "%r names a directory; the store is the caller's to choose" % (name,))
    # and they are still the names the cockpit actually writes
    assert R.cleared_through(fleet=str(tmp_path)) == 0.0
    (tmp_path / R.CLEARED_LOG_NAME).write_text(json.dumps({"ts": 7.0}) + "\n", encoding="utf-8")
    assert R.cleared_through(fleet=str(tmp_path)) == 7.0


# ── the cockpit's half ────────────────────────────────────────────────────────────────────

def _clear_history_body():
    """The text of ClearHistory(), located by its own signature rather than by any line inside
    it -- an anchor on a line that may be edited is an anchor that fails for the wrong reason."""
    src = io.open(COCKPIT, encoding="utf-8").read()
    i = src.index("void ClearHistory()")
    j = src.index("\n    void ", i + 1)
    return src[i:j]


def test_the_decision_is_recorded_before_the_data_is_touched():
    """ORDER IS THE PROPERTY, not presence. A clear interrupted between moving the file aside
    and writing the record leaves the history gone and the reason unrecorded -- which is the
    exact state that made the resurrection possible, reached by a different route."""
    body = _clear_history_body()
    assert "_clearedLogPath" in body, "a clear that records nothing can be undone by a rebuild"
    assert body.index("_clearedLogPath") < body.index("File.Move"), (
        "the record is written after the file is moved: an interrupted clear loses it")


def test_clearing_the_fleet_history_does_not_touch_the_chat_conversations():
    """フリートで履歴を空にしたときにメインチャットからも消えるのはだめね.

    The two stores are separate files and this pins that the clear path knows it: the chat
    window's list lives in .fleet/conversations.json (`_convsPath`) and its transcripts are
    fetched through the bridge, neither of which ClearHistory may name."""
    body = _clear_history_body()
    assert "_convsPath" not in body and "conversations.json" not in body


def test_the_rebuild_tool_is_the_only_thing_that_writes_the_archive_from_the_ledger():
    """THE SWEEP, because fixing one resurrection path is worth nothing if there is a second.
    `relay/fleet_reaper.py` also opens history.json -- it only rewrites rows that are already
    there (`_finalize_history` walks the list it read), and it creates nothing. Asserted by AST
    so a future reaper that starts BUILDING entries trips this rather than passing quietly."""
    src = io.open(os.path.join(REPO, "relay", "fleet_reaper.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_finalize_history":
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            assert "SOCKET_ROUTE" not in names and "build" not in names, (
                "the reaper has started reconstructing history entries; it needs the same "
                "watermark tools/rebuild_history.py has")
            return
    raise AssertionError("_finalize_history is gone -- re-check what writes history.json")

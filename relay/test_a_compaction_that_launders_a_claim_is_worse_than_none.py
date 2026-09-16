# -*- coding: utf-8 -*-
"""A recycle may carry what was attempted, but must not turn a claim into a fact.

WHAT A RECYCLE DOES. When a Copilot conversation exhausts its context, the relay opens a
brand new chat and re-anchors with RECYCLE_PREFIX, which tells the agent its memory is gone
and to read its output files and continue. Everything the previous conversation reasoned out
is discarded. That is safe and wasteful: the fresh agent spends its first turns rediscovering
dead ends the old one already walked into.

WHY A SUMMARY IS NOT THE OBVIOUS FIX. Neither store distinguishes a verified finding from an
unchecked claim -- .fleet/transcripts/*.jsonl and the sqlite fleet_turns table both hold
opaque prose, and a worker that reported success it never achieved is recorded exactly like
one that did the work. A summary presented as progress would carry a false claim into a fresh
context and give it a second life, where the new conversation cannot tell it came from a
guess. That is strictly worse than dropping it.

SO THE NOTE IS CARRIED AND LABELLED. It says what it is -- the previous conversation's own
words, unverified -- and says to check against disk before relying on any of it. Disk stays
the ground truth; the note only exists so the same approach is not tried twice.

Sizing, measured 2026-09-16: concatenating every assistant turn of the longest run on record
(74 turns) is 17,888 characters. A recycle happens BECAUSE context ran out, so the note is
capped in the low thousands -- carrying several attempts without recreating the condition it
recovers from.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import relay_fleet as F  # noqa: E402

GOAL = "テスト用のゴール本文"


def _worker(tmp_path, turns, monkeypatch):
    monkeypatch.setattr(F, "_unlock_password", lambda: "pw-placeholder-not-a-credential")
    w = F.RelayWorker(GOAL, "w0")
    p = tmp_path / "t.jsonl"
    with io.open(str(p), "w", encoding="utf-8", newline="\n") as fh:
        for i, text in enumerate(turns):
            fh.write(json.dumps({"role": "assistant", "turn": i, "text": text},
                                ensure_ascii=False) + "\n")
    w.transcript = str(p)
    w._recycles = 1
    return w


def test_the_note_says_the_content_is_unverified(tmp_path, monkeypatch):
    """The whole safety property. Without this line the note is a laundering machine."""
    w = _worker(tmp_path, ["ファイルAを直しました。"], monkeypatch)
    note = w._compaction_note()
    assert "未検証" in note
    assert "確かめて" in note


def test_what_was_attempted_actually_travels(tmp_path, monkeypatch):
    w = _worker(tmp_path, ["approach-one をやった", "approach-two をやった"], monkeypatch)
    note = w._compaction_note()
    assert "approach-two" in note


def test_the_note_is_bounded_however_long_the_conversation_was(tmp_path, monkeypatch):
    """A recycle happens because context ran out; an unbounded note recreates that."""
    w = _worker(tmp_path, ["あ" * 9000 for _ in range(40)], monkeypatch)
    note = w._compaction_note()
    assert len(note) < w.COMPACT_MAX_CHARS + 400, len(note)


def test_the_most_recent_attempts_are_the_ones_kept(tmp_path, monkeypatch):
    """Older attempts are likelier to be superseded; the newest state is what matters."""
    w = _worker(tmp_path, ["OLDEST"] + ["filler %d" % i for i in range(8)] + ["NEWEST"],
                monkeypatch)
    note = w._compaction_note()
    assert "NEWEST" in note
    assert "OLDEST" not in note


def test_the_goal_still_survives_intact_and_the_note_follows_it(tmp_path, monkeypatch):
    """_composed_prefix is taken as a suffix slice of the composition, so the goal must stay
    contiguous. The note is an addendum after it -- which is also what it is epistemically."""
    w = _worker(tmp_path, ["やったこと"], monkeypatch)
    job = w._recycle_job()
    assert job.count(GOAL) == 1
    assert job.index(GOAL) < job.index("未検証")


def test_a_recycle_still_works_when_there_is_nothing_to_compact(tmp_path, monkeypatch):
    """No transcript, an empty one, or an unreadable one must never block the recovery."""
    w = _worker(tmp_path, [], monkeypatch)
    assert w._compaction_note() == ""
    assert w._recycle_job().endswith(GOAL)

    w.transcript = str(tmp_path / "nope.jsonl")
    assert w._compaction_note() == ""

    bad = tmp_path / "bad.jsonl"
    io.open(str(bad), "w", encoding="utf-8").write("{not json\n")
    w.transcript = str(bad)
    assert w._compaction_note() == ""
    assert w._recycle_job().endswith(GOAL)


def test_only_the_workers_own_words_travel(tmp_path, monkeypatch):
    """The transcript is per worker, so no attribution question arises -- but the note must
    still carry the ASSISTANT side only. Our own prompts are not findings, and feeding them
    back is how a conversation grows without learning anything (measured: the user side of
    one 74-turn run was 547,743 characters, almost all of it resent goal text)."""
    w = F.RelayWorker(GOAL, "w0")
    p = tmp_path / "mixed.jsonl"
    with io.open(str(p), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"role": "user", "text": "PROMPT-TEXT-DO-NOT-CARRY"}) + "\n")
        fh.write(json.dumps({"role": "assistant", "text": "REPLY-TEXT-CARRY-ME"}) + "\n")
        fh.write(json.dumps({"role": "metric", "text": "METRIC-DO-NOT-CARRY"}) + "\n")
    w.transcript = str(p)
    note = w._compaction_note()
    assert "REPLY-TEXT-CARRY-ME" in note
    assert "PROMPT-TEXT-DO-NOT-CARRY" not in note
    assert "METRIC-DO-NOT-CARRY" not in note

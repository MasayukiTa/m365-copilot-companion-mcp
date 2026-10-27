# -*- coding: utf-8 -*-
"""Two SHORT human requests that differ only in the thing they name must both be accepted.

WHAT HAPPENED. fleet_submit's duplicate guard compares goals on their content words
(_dup_words), and _dup_words only counts Latin runs of three letters or more (its floor keeps
stray fragments out of the Jaccard measure). A human request typed on a phone is mostly shared
template-and-verb vocabulary -- "<X> を解析してレポートを作成" -- and the ONE token that tells
two such jobs apart is often a single letter, a short name, a filename or a version: 'ファイルA'
vs 'ファイルB', 'sales.csv' vs 'orders.csv', 'v2' vs 'v3'. Those are exactly the tokens the
three-letter floor drops, so with the distinguishing token gone the two goals score a word
overlap of 1.0 and the second is refused as a resubmission of the first while the first is still
in flight.

WHAT THIS FILE PINS. (a) Two goals whose only difference is a single-letter name are BOTH
accepted. (b) The same for a goal distinguished by a filename, and (c) by a version string.
(d) An exact resend of a goal already in flight is still refused. (e) A resend of the SAME job
with a few words reworded -- same identifier -- is still refused. The guard now treats two
goals that each name identifiers of their own and share NONE of them as different work, before
the word measure runs; a real resend keeps the same identifiers (or names none) and is still
caught by the word measure.

The requests below are invented stand-ins -- no real product, file or business name belongs in
a tracked file (see this repo's history on that).
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import task_router as TR  # noqa: E402
from tools import fleet_intake as FI  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """A queue AND a fleet-state directory of its own -- the same isolation the sibling
    duplicate-guard suites use, so this file never reads the live fleet's state."""
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    TR.ensure_dirs()
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(tmp_path / "state"))
    os.makedirs(str(tmp_path / "state"), exist_ok=True)
    return tmp_path


def _write_status(tmp_path, workers):
    with open(os.path.join(str(tmp_path / "state"), "status.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"running": True, "workers": workers}, fh, ensure_ascii=False)


def _running_worker(jid, goal):
    return {"jid": jid, "goal": goal, "status": "ready", "closed": False, "outcome": None}


def _jobs():
    d = os.path.join(TR.TASKS, "pending")
    out = []
    for name in sorted(os.listdir(d)):
        if name.endswith(".json"):
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                out.append(json.load(fh))
    return out


# -- the unit the fix turns on ------------------------------------------------------------------

def test_goal_specific_identifiers_keeps_single_letters_and_filenames():
    """_dup_words drops one- and two-letter runs; the identifier extractor must keep them, or the
    guard has nothing to tell two otherwise-identical short goals apart."""
    assert FI._goal_specific_identifiers("ファイルAを解析", "ファイルBを解析") == {"a"}
    assert FI._goal_specific_identifiers("ファイルBを解析", "ファイルAを解析") == {"b"}
    assert FI._goal_specific_identifiers("sales.csv を解析", "orders.csv を解析") == {"sales.csv"}
    assert FI._goal_specific_identifiers("v2 を直す", "v3 を直す") == {"v2"}


def test_a_shared_tail_does_not_leak_its_identifiers_into_the_specific_set():
    """The appended common-rules paragraph names git, diff, ... in EVERY goal that carries it.
    Those must be stripped with the tail, or two unrelated goals would look as though they share
    specifics purely because of the boilerplate."""
    tail = " 共通の守ること git は読み取り専用 diff の全文を出す"
    a = "ファイルAを解析" + tail
    b = "ファイルBを解析" + tail
    ia = FI._goal_specific_identifiers(a, b)
    ib = FI._goal_specific_identifiers(b, a)
    assert "git" not in ia and "diff" not in ia, ia
    assert ia == {"a"} and ib == {"b"}, (ia, ib)


# -- (a)(b)(c) different specific thing -> accepted ---------------------------------------------

def test_a_single_letter_name_makes_a_second_short_goal_its_own_job(isolated_state, monkeypatch):
    """(a) 'ファイルAを解析してレポートを作成' is in flight; 'ファイルBを解析してレポートを作成'
    is a DIFFERENT file and must be accepted. With the guard off by default this holds for any
    second goal; the guard is pinned off explicitly so the test does not depend on the machine's
    settings file."""
    monkeypatch.setattr(FI, "_dup_guard_on", lambda: False)
    _write_status(isolated_state, [_running_worker("run1", "ファイルAを解析してレポートを作成")])
    out = FI.fleet_submit("ファイルBを解析してレポートを作成")
    assert "queued" in out, out
    assert len(_jobs()) == 1


def test_a_different_filename_makes_its_own_job(isolated_state, monkeypatch):
    """(b) Distinguished by a filename rather than a bare letter."""
    monkeypatch.setattr(FI, "_dup_guard_on", lambda: False)
    _write_status(isolated_state, [_running_worker("run1", "sales.csv を解析して集計を出す")])
    out = FI.fleet_submit("orders.csv を解析して集計を出す")
    assert "queued" in out, out
    assert len(_jobs()) == 1


def test_a_different_version_string_makes_its_own_job(isolated_state, monkeypatch):
    """(c) Distinguished by a version identifier."""
    monkeypatch.setattr(FI, "_dup_guard_on", lambda: False)
    _write_status(isolated_state, [_running_worker("run1", "v2 の設定ファイルを直して確認する")])
    out = FI.fleet_submit("v3 の設定ファイルを直して確認する")
    assert "queued" in out, out
    assert len(_jobs()) == 1


# -- (d)(e) the property the guard exists for must survive --------------------------------------

def test_an_exact_resend_of_a_short_goal_is_still_refused_when_the_guard_is_on(
        isolated_state, monkeypatch):
    """(d) The two goals name the SAME thing (or none), so the identifier check does not fire and
    the word measure would refuse the resend as before -- but only WHEN THE GUARD IS ON. By owner
    order 2026-10-26 it is off by default, so this resend is now accepted and recorded in the
    background; this test forces the guard on to pin that the short-goal word measure still
    refuses when an operator asks for it."""
    monkeypatch.setattr(FI, "_dup_guard_on", lambda: True)
    _write_status(isolated_state, [_running_worker("run1", "ファイルAを解析してレポートを作成")])
    out = FI.fleet_submit("ファイルAを解析してレポートを作成")
    assert "refused" in out and "run1" in out, out
    assert _jobs() == []


def test_a_reworded_resend_naming_the_same_thing_is_still_refused_when_the_guard_is_on(
        isolated_state, monkeypatch):
    """(e) Same identifier (v2), a few words changed: still the same job, still refused WHEN THE
    GUARD IS ON. The identifier sets are NOT disjoint (both name v2), so the guard falls through
    to the word measure, which still clears the bar. Off by default now, so the guard is forced
    on here -- this is exactly the brief's test (e): turning the setting back on restores PR
    #293's refusal."""
    monkeypatch.setattr(FI, "_dup_guard_on", lambda: True)
    _write_status(isolated_state, [_running_worker("run1", "v2 の設定ファイルを直して確認する")])
    out = FI.fleet_submit("v2 の設定ファイルを修正して確認する")
    assert "refused" in out and "run1" in out, out
    assert _jobs() == []

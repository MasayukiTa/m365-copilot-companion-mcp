# -*- coding: utf-8 -*-
"""The reader written for the crash path was never called on it.

`fanout.campaigns_from_ledger` is 43 lines, tested, exported, and its docstring names the case
it exists for: on FleetContextLost the fleet re-enters `run_relay_fleet` in a fresh process,
`campaigns` starts empty, `_unfinished()` rebuilds goals and never families -- so a campaign
split before the crash is never merged again. Its children may all finish and the answer they
were collected for is never assembled.

That was still true. Repo-wide grep found no caller outside tests, and this repository's own
unreached inventory had carried

    "relay/fanout.py::campaigns_from_ledger",                    # 43 lines

in NO_CALLER_BUT_TESTED since the day it was written. A test referencing a function says it was
worth writing; it says nothing about anything reaching it.

IT GOT WORSE THE SAME DAY IT WAS FOUND. The header now carries `checks` (the whole goal's
acceptance check, moved off the children) and `partial` (the work a mid-run-split parent had
already finished). Both exist ONLY on disk once the process is gone, so unwired, a restart lost
them silently -- on the one path where nobody is watching.

AND `merged` LIVED ONLY IN MEMORY, so rehydration on its own would have re-queued the merge for
every campaign the fleet had ever finished. The flag goes on the ledger for the same reason the
header did: the thing that outlives the process is the file.
"""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import fanout as fo                              # noqa: E402
from relay.relay_fleet import _campaigns_from_disk          # noqa: E402


def _ledger(tmp_path, rows):
    """A .fleet-shaped tree: the ledger sits beside the transcripts directory."""
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    with open(str(tmp_path / "campaigns.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(tdir)


HEADER = {"kind": "campaign", "campaign_id": "cA", "goal": "1〜3月を一覧化する", "n": 3,
          "cwd": "C:/work", "checks": [{"type": "pytest", "args": "-q"}],
          "partial": "1月分 120件は分割前に取得済み"}


# ── the family comes back ─────────────────────────────────────────────────────────────────

def test_a_split_family_survives_the_process_that_split_it(tmp_path):
    fams = _campaigns_from_disk(_ledger(tmp_path, [HEADER]))
    assert "cA" in fams, (
        "分割済みキャンペーンが再起動で消える -- 子が全部終わっても統合は永久に走らない")
    assert fams["cA"]["goal"] == "1〜3月を一覧化する"
    assert fams["cA"]["n"] == 3
    assert fams["cA"]["cwd"] == "C:/work"


def test_the_acceptance_check_comes_back_with_it(tmp_path):
    """It lives nowhere else once the process is gone: the children no longer carry it, by
    design, and the merge is the only worker that can answer it."""
    fams = _campaigns_from_disk(_ledger(tmp_path, [HEADER]))
    assert fams["cA"]["checks"] == [{"type": "pytest", "args": "-q"}]


def test_the_pre_split_parents_work_comes_back_with_it(tmp_path):
    fams = _campaigns_from_disk(_ledger(tmp_path, [HEADER]))
    assert "120件" in fams["cA"]["partial"], (
        "分割前に親が終えた作業がクラッシュで永久に失われる")


# ── and is not delivered twice ────────────────────────────────────────────────────────────

def test_a_family_already_assembled_is_not_queued_again(tmp_path):
    rows = [HEADER, {"kind": "merged", "campaign_id": "cA"}]
    assert _campaigns_from_disk(_ledger(tmp_path, rows)) == {}


def test_the_merged_note_is_carried_across_the_header(tmp_path):
    """Two runs append to one file, so the note can land before the header it refers to."""
    rows = [{"kind": "merged", "campaign_id": "cA"}, HEADER]
    fams = fo.campaigns_from_ledger([json.dumps(r, ensure_ascii=False) for r in rows])
    assert fams["cA"]["merged"] is True


def test_the_merge_writes_that_note():
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    i = src.index('_camp["merged"] = True')
    assert "_note_merged(_cid)" in src[i:i + 200], (
        "統合してもその事実が台帳に残らない -- 再起動のたびに同じ統合が積まれる")
    j = src.index("def _note_merged(cid):")
    assert '"kind": "merged"' in src[j:j + 1400]


# ── failure is not a crash, and is not a silent pass ──────────────────────────────────────

def test_a_run_with_no_transcript_dir_is_unchanged():
    assert _campaigns_from_disk("") == {}
    assert _campaigns_from_disk(None) == {}


def test_a_ledger_that_does_not_exist_yet_is_not_an_error(tmp_path):
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    assert _campaigns_from_disk(str(tdir)) == {}


def test_a_torn_final_line_does_not_lose_the_families_above_it(tmp_path):
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    with open(str(tmp_path / "campaigns.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(HEADER, ensure_ascii=False) + "\n")
        fh.write('{"kind": "campaign", "campaign_id": "cB"')
    assert "cA" in _campaigns_from_disk(str(tdir))


def test_a_family_whose_header_never_landed_is_not_resurrected(tmp_path):
    """Child rows alone cannot rebuild a merge -- the parent goal is what the merge needs and
    it is only on the header. Returning a campaign with an empty goal would merge into
    nothing, which is why campaigns_from_ledger drops it."""
    rows = [{"campaign_id": "cC", "task_id": "t1", "subtask_index": 1, "text": "x"}]
    assert _campaigns_from_disk(_ledger(tmp_path, rows)) == {}


def test_a_rehydrated_family_with_no_children_here_cannot_merge_into_nothing():
    """The safety this rests on, checked rather than assumed. A campaign carried over from an
    old run has no live children, so its record list is empty -- and empty is deliberately NOT
    ready ('aggregating nothing would produce a confident summary of work that never ran')."""
    assert fo.ready_to_aggregate([]) is False

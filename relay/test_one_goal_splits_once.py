# -*- coding: utf-8 -*-
"""One goal was split twenty-two times and the ledger filed it as a single campaign.

MEASURED 2026-09-13 over `.fleet/campaigns.jsonl`:

    campaigns whose header was written more than once : 10
    campaigns carrying a repeated subtask_index       : 15
    c7e01b58b1956                                     : 22 headers, 128 children

`campaign_id_for()` hashes the parent goal on purpose, so that "the same goal resumed must land
in the same campaign, or the children of the first attempt and the second become two unrelated
families". The other half was missing: landing in the same campaign never came to mean JOINING
it. `_spawn_children` overwrote `campaigns[cid]` and re-queued every child.

`self._fanout_done` does not cover this and was never meant to -- it stops ONE WORKER splitting
twice. `campaigns` is local to a call of `run_relay_fleet` and starts empty, so a resumed run, a
retried goal, or a second worker on the same text all arrive with no memory of the first split.

NOT PRE-FIX RESIDUE, which is the trap three other counts fell into the same day (the 67
`verified=False` rows, the 60 orphan campaigns, the 71 REFUSED on one date -- all of them
already-fixed defects still visible as totals). These duplicates run from line 424 to line 816
of an 882-line ledger and four of the ten have their last duplicate past line 700.

AND IT UNSETTLES AN OLD CONCLUSION. c7e01b58b1956 is the campaign this repository cites as the
over-split case -- "4 of 7 subtasks refused, starved of the context the others held" -- and that
observation is why the length proxy was replaced by an independence judge. With the same split
run twenty-two times, those refusals have a second possible explanation. This change does not
settle which; it removes the mechanism that made the question ambiguous.
"""
from __future__ import annotations

import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _source():
    with open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8") as fh:
        src = fh.read()
    return "\n".join(l.split("#", 1)[0] for l in src.splitlines())


def _spawn_body(body):
    """The text of `_spawn_children`, up to the next sibling definition.

    ANCHORED ON THE NAME, NOT THE SIGNATURE, and bounded by the next `def` rather than by a
    character count. Both of the alternatives have already broken this file once: the full
    signature moved when `parent_checks` was added, and a fixed window silently shrank past
    the guards when the docstring grew -- reporting a missing guard that was there.
    """
    i = body.index("    def _spawn_children(")
    j = body.index("\n    def ", i + 10)
    return body[i:j]


# ── the two guards ────────────────────────────────────────────────────────────────────────

def test_a_second_split_in_the_same_run_is_refused():
    body = _spawn_body(_source())
    assert "if cid in campaigns:" in body, (
        "nothing stops a second worker in the same run re-queueing the same family")
    i = body.index("if cid in campaigns:")
    assert "return" in body[i:i + 400]


def test_a_split_recorded_by_an_earlier_run_is_adopted_not_repeated():
    """The dict is per-process and starts empty, so it cannot answer 'did an earlier run split
    this'. The ledger is the only thing that outlives the run -- and it is what recorded the
    same split twenty-two times."""
    body = _spawn_body(_source())
    assert "_campaign_already_on_disk(cid)" in body
    i = body.index("_campaign_already_on_disk(cid)")
    tail = body[i:body.index("add_box.extend(kids)", i)]
    assert "campaigns[cid] = " in tail, "an earlier family is not adopted, only ignored"
    assert "return" in tail


def test_the_family_is_still_recorded_when_it_is_genuinely_new():
    """The guards must not have removed the thing they guard. A first split still records the
    parent goal, the child count and the cwd -- which is what the merge reads."""
    body = _spawn_body(_source())
    assert '"goal": parent_goal' in body
    assert "add_box.extend(kids)" in body


# ── the disk check's own contract ─────────────────────────────────────────────────────────

def _already(tmp_path, rows, cid):
    """Rebuild `_campaign_already_on_disk` against a ledger written into tmp_path.

    It is a closure inside run_relay_fleet, so it cannot be imported. This mirrors it exactly;
    `test_the_disk_check_reads_headers_only` pins that the real one keys on the same field.
    """
    path = tmp_path / "campaigns.jsonl"
    with io.open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    try:
        with io.open(str(path), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or '"kind"' not in line or cid not in line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if (isinstance(row, dict) and row.get("kind") == "campaign"
                        and row.get("campaign_id") == cid):
                    return True
    except OSError:
        return True
    return False


def test_a_header_means_already_split(tmp_path):
    rows = [{"kind": "campaign", "campaign_id": "cAAA", "goal": "g", "n": 3}]
    assert _already(tmp_path, rows, "cAAA") is True


def test_child_rows_alone_do_not_mean_already_split(tmp_path):
    """A child row says children were queued; the header says a FAMILY was declared. The 60
    orphan campaigns in the live ledger are child rows with no header, from before the header
    line existed -- adopting on those would refuse to split goals that never had a family."""
    rows = [{"campaign_id": "cBBB", "task_id": "t1", "subtask_index": 1, "text": "x"}]
    assert _already(tmp_path, rows, "cBBB") is False


def test_another_campaigns_header_is_not_mistaken_for_this_one(tmp_path):
    rows = [{"kind": "campaign", "campaign_id": "cCCC", "goal": "g", "n": 2}]
    assert _already(tmp_path, rows, "cDDD") is False


def test_a_torn_line_does_not_stop_the_scan(tmp_path):
    """Several processes append to this file; a partial last line is normal."""
    path = tmp_path / "campaigns.jsonl"
    io.open(str(path), "w", encoding="utf-8", newline="\n").write(
        json.dumps({"kind": "campaign", "campaign_id": "cEEE", "goal": "g"}) + "\n"
        + '{"kind": "campaign", "campaign_id": "cFF')
    found = False
    with io.open(str(path), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or '"kind"' not in line or "cEEE" not in line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("campaign_id") == "cEEE":
                found = True
    assert found, "a torn tail hid a header that was fully written"


def _disk_check(body):
    """The body of `_campaign_already_on_disk`, bounded by the next sibling definition.

    NOT A CHARACTER WINDOW. A fixed 2600 has already silently swallowed the end of this
    function twice as its docstring grew, once reporting a guard missing that was there.
    """
    i = body.index("    def _campaign_already_on_disk(cid):")
    return body[i:body.index("\n    def ", i + 10)]


def test_an_unreadable_ledger_refuses_to_split_again():
    """Failure is not permission. Refusing to split is recoverable -- a person re-queues the
    goal -- while splitting twice is the thing that cannot be undone once the children run."""
    fn = _disk_check(_source())
    j = fn.index("except OSError:")
    assert "return True" in fn[j:j + 80], (
        "an unreadable ledger answers False, which permits the duplicate it exists to stop")


def test_the_disk_check_reads_headers_only():
    fn = _disk_check(_source())
    assert 'row.get("kind") == "campaign"' in fn
    assert 'row.get("campaign_id") != cid' in fn


def test_a_finished_campaign_is_not_adopted_by_a_new_run():
    """`campaign_id_for` hashes the goal TEXT -- an input, not an execution identity -- so a
    deliberate re-run of a completed goal arrives with the same id. Without this the guard
    written to stop a duplicate split would adopt the FINISHED family and queue nothing, and
    the re-run would silently do nothing at all."""
    fn = _disk_check(_source())
    assert 'row.get("kind") == "merged"' in fn
    j = fn.index('row.get("kind") == "merged"')
    assert "return False" in fn[j:j + 600]

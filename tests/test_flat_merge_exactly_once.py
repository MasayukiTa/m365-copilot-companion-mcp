# -*- coding: utf-8 -*-
"""A flat family's merge is queued exactly once -- pinned by behaviour, not source layout.

relay/test_a_family_survives_the_process_that_split_it.py pins that `_note_merged(` sits next to
`_camp["merged"] = True` in the source. That is a layout check: it failed when a nested-family
edit moved the call away, without any behaviour change. This file holds the guarantee itself,
through run_relay_fleet with fake workers:

  * a flat family whose children all finish queues ONE merge and writes ONE `merged` ledger line;
  * a restart over the same ledger (children gone, header and `merged` line on disk) queues no
    second merge and adds no second `merged` line.
"""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import relay.relay_fleet as rf  # noqa: E402
from tests import test_hierarchical_merge as thm  # noqa: E402


def _merged_lines(tmp_path, cid):
    return [r for r in thm._ledger(tmp_path)
            if r.get("kind") == "merged" and r.get("campaign_id") == cid]


def test_a_flat_family_merges_once_and_a_restart_does_not_merge_again(tmp_path, monkeypatch):
    h = thm._install(monkeypatch, {})
    monkeypatch.setattr(thm.fanout, "HIERARCHICAL_MERGE_READY", False)
    calls = thm._spy(monkeypatch)
    cid = "cFLATONCE"
    rows = [thm._hdr(cid, 3, "flat goal text")]
    goals = [thm._kid(cid, i, 3) for i in (1, 2, 3)]

    tdir = h._crashed_run(tmp_path, rows)
    rf.run_relay_fleet(h._FakeContext(), goals, "http://agent", max_concurrent=8, poll_s=0,
                       transcript_dir=tdir, notify=lambda *a, **k: None, fanout=False)
    assert len(thm._of(calls, cid)) == 1, "first run: exactly one merge"
    first = _merged_lines(tmp_path, cid)
    assert len(first) == 1, "the merge must leave its `merged` line on the ledger"

    # restart: a fresh process re-enters over the same ledger with no children to run
    rf.run_relay_fleet(h._FakeContext(), [], "http://agent", max_concurrent=8, poll_s=0,
                       transcript_dir=tdir, notify=lambda *a, **k: None, fanout=False)
    assert len(thm._of(calls, cid)) == 1, "restart must not queue the same merge again"
    assert len(_merged_lines(tmp_path, cid)) == 1

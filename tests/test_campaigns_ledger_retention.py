# -*- coding: utf-8 -*-
"""SPEC (not yet built) for compacting campaigns.jsonl, plus what is built today.

relay/fleet_retention.py does not compact campaigns.jsonl: only cap_jsonl's 64 MB tail cut bounds
it, and a warning row (`campaigns_ledger_large`) fires past 8 MB. Reading it no longer depends on
its size (relay/fanout_budget.py), which is what made the growth urgent. Compaction itself waits
for a slice that can prove the safety conditions below, so each is written here as a skipped
test that names what it must show.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import fleet_retention as FRET  # noqa: E402

_WAITING = "compaction of campaigns.jsonl is specified here and not built"


def test_the_ledger_is_never_cut_by_the_default_retention_before_the_ceiling(tmp_path):
    p = tmp_path / "campaigns.jsonl"
    p.write_bytes(b'{"kind": "campaign", "campaign_id": "a", "n": 2}\n' * 100000)   # ~5 MB
    before = p.read_bytes()
    FRET.apply(str(tmp_path), dry_run=False)
    assert p.read_bytes() == before                      # under the 64 MB ceiling: untouched


@pytest.mark.skip(reason=_WAITING)
def test_spec_only_finished_old_families_move_to_the_rotation_file():
    """A family (header, children, results, merged rows) whose `merge_done` row exists and which is
    older than N days moves, whole, to campaigns.jsonl.1; the main file keeps every other line in
    order."""


@pytest.mark.skip(reason=_WAITING)
def test_spec_unfinished_families_and_their_nested_roots_stay():
    """A family with no merge_done row, and any family whose root_id is named by a nested header
    that is itself unfinished, is never moved (resume and campaigns_from_ledger read them)."""


@pytest.mark.skip(reason=_WAITING)
def test_spec_a_family_named_by_an_interrupted_run_snapshot_or_a_live_worker_stays():
    """The campaign ids in the interrupted-run snapshot and in live workers' rows are a hard
    exclusion list, whatever the age."""


@pytest.mark.skip(reason=_WAITING)
def test_spec_compaction_is_atomic_and_losing_nothing():
    """The rotation file is written and flushed before the main file is replaced; the union of
    both files equals the original line multiset; a crash between the two steps leaves the
    original readable."""

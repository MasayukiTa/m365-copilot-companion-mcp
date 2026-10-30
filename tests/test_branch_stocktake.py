"""The branch stocktake explains '14 left but 5 declared' instead of asserting a number.

Ledger line #209 is an operator asking why the leftover branch count (14) does not
match the declared count (5). A correct answer is a reconciliation: every leftover
branch lands in exactly one bucket, and the gap is shown to be settled-but-un-pruned
branches rather than hidden in-flight work. These tests pin that behaviour with pure
inputs so they run the same on any machine.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.branch_stocktake import (  # noqa: E402
    reconcile_branches,
    strip_remote_prefix,
    format_report,
)


def _pr(ref, state, number=None):
    return {"headRefName": ref, "state": state, "number": number}


def test_strip_remote_prefix_is_idempotent_and_keeps_slashes():
    assert strip_remote_prefix("origin/feature/x") == "feature/x"
    assert strip_remote_prefix("feature/x") == "feature/x"
    assert strip_remote_prefix("remotes/origin/foo") == "foo"
    # The 'origin/HEAD -> origin/main' pointer resolves to its left side ('origin/HEAD'),
    # then the remote prefix is stripped to the bare 'HEAD' marker, which reconcile_branches
    # drops as never-leftover. It is not a branch, so it must not resolve to 'main'.
    assert strip_remote_prefix("origin/HEAD -> origin/main") == "HEAD"


def test_main_and_head_pointer_are_never_leftover():
    branches = ["origin/main", "main", "origin/HEAD -> origin/main", "origin"]
    rec = reconcile_branches(branches, prs=[], declared=0)
    assert rec.leftover_count == 0
    assert rec.reconciles


def test_the_209_shape_reconciles_when_the_gap_is_settled_branches():
    # 14 leftover (incl. main) -> 13 real branches. 5 are in flight (open PR / no PR),
    # the other 8 are branches whose PR already merged/closed and were never pruned.
    branches = ["origin/main"]
    prs = []
    # five in-flight: three OPEN PRs + two branches with no PR yet
    for i in range(3):
        name = f"feature/open-{i}"
        branches.append(f"origin/{name}")
        prs.append(_pr(name, "OPEN", number=300 + i))
    for i in range(2):
        branches.append(f"origin/wip/no-pr-{i}")
    # eight settled-but-un-pruned
    for i in range(8):
        name = f"done/merged-{i}"
        branches.append(f"origin/{name}")
        prs.append(_pr(name, "MERGED", number=200 + i))

    rec = reconcile_branches(branches, prs, declared=5)
    assert rec.leftover_count == 13  # main excluded
    assert rec.in_flight_count == 5
    assert rec.settled_count == 8
    assert rec.reconciles is True
    assert rec.unexplained_gap == 0


def test_a_closed_pr_branch_counts_as_settled_not_in_flight():
    branches = ["origin/abandoned"]
    prs = [_pr("abandoned", "CLOSED", number=76)]
    rec = reconcile_branches(branches, prs, declared=0)
    assert rec.settled_count == 1
    assert rec.in_flight_count == 0
    assert rec.reconciles


def test_more_in_flight_than_declared_is_flagged_as_unexplained():
    # Declared 1, but two branches are genuinely in flight -> the gap is real.
    branches = ["origin/a", "origin/b"]
    prs = [_pr("a", "OPEN", 10), _pr("b", "OPEN", 11)]
    rec = reconcile_branches(branches, prs, declared=1)
    assert rec.in_flight_count == 2
    assert rec.reconciles is False
    assert rec.unexplained_gap == 1


def test_a_branch_appearing_twice_is_counted_once():
    # Same branch from remotes/ and from a bare name; and listed twice.
    branches = ["remotes/origin/dup", "origin/dup", "dup"]
    prs = [_pr("dup", "OPEN", 5)]
    rec = reconcile_branches(branches, prs, declared=1)
    assert rec.leftover_count == 1
    assert rec.in_flight_count == 1
    assert rec.reconciles


def test_a_reused_branch_prefers_its_open_pr_record():
    # The branch was closed once and reopened; the OPEN record must win so it is
    # reported as in flight, not settled.
    branches = ["origin/reused"]
    prs = [_pr("reused", "CLOSED", 1), _pr("reused", "OPEN", 2)]
    rec = reconcile_branches(branches, prs, declared=1)
    assert rec.in_flight_count == 1
    assert rec.settled_count == 0
    assert rec.leftover[0].pr_number == 2


def test_an_unknown_pr_state_is_kept_as_in_flight_never_dropped():
    branches = ["origin/weird"]
    prs = [_pr("weird", "", 9)]
    rec = reconcile_branches(branches, prs, declared=1)
    assert rec.in_flight_count == 1
    assert rec.settled_count == 0


def test_format_report_is_deterministic_and_names_the_buckets():
    branches = ["origin/main", "origin/open-x", "origin/merged-y"]
    prs = [_pr("open-x", "OPEN", 50), _pr("merged-y", "MERGED", 40)]
    rec = reconcile_branches(branches, prs, declared=1)
    text = format_report(rec)
    assert "leftover branches: 2" in text
    assert "declared: 1" in text
    assert "in flight" in text
    assert "settled" in text
    assert "reconciles" in text
    assert "open-x" in text and "merged-y" in text

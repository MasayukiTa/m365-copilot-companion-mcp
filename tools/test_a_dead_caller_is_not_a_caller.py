# -*- coding: utf-8 -*-
"""A function called only by another unreached function was counted as reached.

`tools/unreached.py` counts references, and a reference count is not a reachability analysis.
So a cluster of mutually-calling dead code showed only whichever member nothing else called:
the tool reported dead LEAVES, and the branch behind each leaf stayed invisible until the leaf
was removed and the scan run again.

THREE WERE CONFIRMED BY HAND BEFORE THIS WAS BUILT, each found while chasing something else:

    tools/coding_ops.py::worktree_add / worktree_remove   called only from worktree_scope
    bench/skill_use_log.py::observe                       called only from the `if False`
                                                          branch inside compare_runs
    relay/selfimprove/compare.py::versions_differ         called only from
                                                          transport_versions_differ

So: report, drop what was reported, count again, repeat. Measured on this repository it settles
in FIVE rounds and reports 16 more names -- 80 to 96.

WHAT IT DOES NOT FIX, and the expectation that was wrong. `fleet_toolset::check` and `::mode`
are dead and this does NOT reveal them: they are blocked by the ambiguity bucket -- names defined
in several modules with a call the AST cannot attribute -- which is a different mechanism and is
already reported separately by `scan().ambiguous`. Iterating cannot resolve a name it was never
allowed to judge.

AND IT SURFACED A COHERENT CLUSTER RATHER THAN SCATTERED NAMES. `relay/solve_policy.py::
plan_solve` has no production caller, and the call to `selfimprove.diversify.diversify` sits
inside it (solve_policy.py:56, within plan_solve at 29-84). `diversify` had been removed from
the inventory on 2026-09-13 as a false positive -- correctly, because the REFERENCE exists.
Both statements are true at their own level: the reference is real and the referrer is
unreachable.
"""
from __future__ import annotations

import ast
import os
import sys
import textwrap

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import unreached as U  # noqa: E402


def _scan(tmp_path, monkeypatch, files_src):
    """Run the real `scan()` over a synthetic tree."""
    rels = []
    for rel, src in files_src.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src), encoding="utf-8")
        rels.append(rel)
    monkeypatch.setattr(U, "REPO", str(tmp_path))
    monkeypatch.setattr(U, "ROOTS", tuple({r.split("/")[0] for r in rels if "/" in r}))
    monkeypatch.setattr(U, "cross_language_text", lambda: [])
    return {r[0] for r in U.scan(files=rels)}


# ── the property ──────────────────────────────────────────────────────────────────────────

def test_a_function_called_only_by_a_dead_one_is_reported(tmp_path, monkeypatch):
    """THE DEFECT. `inner` was counted as reached because `outer` names it, and `outer` is
    reached by nothing."""
    rows = _scan(tmp_path, monkeypatch, {
        "pkg/m.py": """
            def inner():
                return 1

            def outer():
                return inner()
            """,
    })
    assert "pkg/m.py::outer" in rows, "the leaf itself must still be reported"
    assert "pkg/m.py::inner" in rows, "the branch behind the dead leaf is still hidden"


def test_a_function_called_by_a_live_one_is_not_reported(tmp_path, monkeypatch):
    """The other half. Dropping a dead caller must not drop a live one with it.

    THE CALLER HAS TO BE `main`, and the first draft of this test got that wrong: it used a
    `go()` that nothing called, so `go` was dead, so `outer` was dead, so `inner` was dead --
    and the test read as a bug in the loop when the loop was right. In a synthetic tree nothing
    is reachable unless it is an ENTRYPOINT, and `main` is the only root this scanner has
    (PROTOCOL). A fixture that forgets to give the chain a root is asserting that dead code is
    alive."""
    rows = _scan(tmp_path, monkeypatch, {
        "pkg/m.py": """
            def inner():
                return 1

            def outer():
                return inner()
            """,
        "pkg/user.py": """
            from pkg.m import outer

            def main():
                return outer()
            """,
    })
    assert "pkg/m.py::outer" not in rows
    assert "pkg/m.py::inner" not in rows, "a live chain was pruned"


def test_a_chain_three_deep_is_followed_to_the_end(tmp_path, monkeypatch):
    """One round reveals one layer; the fixed point is what reveals the rest."""
    rows = _scan(tmp_path, monkeypatch, {
        "pkg/m.py": """
            def c():
                return 1

            def b():
                return c()

            def a():
                return b()
            """,
    })
    assert {"pkg/m.py::a", "pkg/m.py::b", "pkg/m.py::c"} <= rows


def test_mutual_recursion_is_NOT_caught_and_that_is_stated_rather_than_claimed(tmp_path,
                                                                                monkeypatch):
    """THE LIMIT OF THIS ALGORITHM, asserted so nobody has to rediscover it.

    Iterating works by removing what has been REPORTED, and a name is reported only when its
    reference count is zero. Two dead functions that call each other each hold the other's count
    above zero, so neither is ever a leaf, the first round reports neither, and there is nothing
    to remove -- the loop terminates immediately having found nothing.

    Catching that needs a real reachability analysis: mark from the entrypoints and report what
    was never marked. That is a different algorithm, not a longer loop, and pretending otherwise
    is how a tool comes to be trusted for a question it cannot answer. The first draft of this
    test asserted the pair WOULD be caught; it fails, and the honest assertion is this one.
    """
    rows = _scan(tmp_path, monkeypatch, {
        "pkg/m.py": """
            def ping(n):
                return pong(n)

            def pong(n):
                return ping(n)
            """,
    })
    assert "pkg/m.py::ping" not in rows and "pkg/m.py::pong" not in rows, (
        "mutual recursion is now caught -- if that is deliberate, this scanner has become a "
        "reachability analysis and its docstring should say so")


def test_an_entrypoint_can_never_seed_the_iteration(tmp_path, monkeypatch):
    """`main` is in PROTOCOL and is therefore never reported, so its body is never dropped.
    That is what keeps `bench/retry_floor.py::report`, reached only from its own `main()`,
    correctly out of the inventory -- and it is a property of PROTOCOL, not an accident."""
    assert "main" in U.PROTOCOL
    rows = _scan(tmp_path, monkeypatch, {
        "pkg/m.py": """
            def report():
                return 1

            def main():
                return report()
            """,
    })
    assert "pkg/m.py::report" not in rows, "an entrypoint's callee was pruned away"


# ── it settles, and it says so ────────────────────────────────────────────────────────────

def test_it_reaches_a_fixed_point_well_inside_the_bound():
    """The bound is a guard against a bug in the loop, not a property of the data. If this
    starts needing every round, the loop is no longer converging and the count is not a fixed
    point -- which would be a worse state than the leaves-only one it replaced."""
    calls = {"n": 0}
    real = U._without

    def counted(tree, dead):
        calls["n"] += 1
        return real(tree, dead)

    try:
        U._without = counted
        rows = U.scan()
    finally:
        U._without = real
    files = len(U.tracked_files() or [])
    assert files, "git could not list the tracked files"
    rounds = calls["n"] / float(files)
    assert rounds <= 8, "the scan is not settling: %.1f rounds" % rounds
    assert len(rows) > 80, "the iteration is reporting no more than a single pass did"


def test_the_confirmed_subgraphs_that_are_still_dead_are_reported():
    """PINNED BY NAME, because each was found by hand and each would go quiet again if the
    iteration were removed.

    `bench/skill_use_log.py::observe` WAS THE THIRD AND IS NOT PINNED ANY MORE, because it is
    no longer an example of anything. It was reachable only from `compare_runs`, which nothing
    called; on 2026-09-19 it gained a live caller (`report`, the reader that log had gone
    three weeks without) and the scan correctly stopped reporting it. Holding a fixed instance
    in a list of expected findings is the same defect the encoding inventory was caught with
    the same day: an entry that was paid keeps reading as the current state.

    WHAT IS PINNED IS THE MECHANISM, NOT THE CENSUS. Two genuinely dead subgraphs remain and
    they still exercise it. If both are ever fixed, the honest move is another live example or
    a constructed one -- not keeping these names after they stop being true.
    """
    rows = {r[0] for r in U.scan()}
    for name in ("tools/coding_ops.py::worktree_add",
                 "tools/coding_ops.py::worktree_remove"):
        assert name in rows, "%s is hidden behind its dead caller again" % name
    assert "bench/skill_use_log.py::observe" not in rows, (
        "observe is unreached again -- `report` was the caller that took it off this list, "
        "and a pin that silently comes back true is not evidence")

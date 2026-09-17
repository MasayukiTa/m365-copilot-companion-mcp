# -*- coding: utf-8 -*-
"""main.py states the staleness rule inline; this is what stops it drifting from the original.

WHY IT IS NOT JUST IMPORTED. scripts/stale_server_check.classify_staleness is the canonical
statement of "is the running server on the checkout's commit", it is pure and pytest-covered,
and importing it into main.py would be the obvious move. It is the wrong one:
tools/deploy_freshness.WATCHED lists the packages whose changes make the running server
stale, and tools/test_deploy_freshness.py asserts WATCHED equals what main.py actually
imports -- so importing from scripts/ would force "scripts" into that list. scripts/ is
mostly standalone files the server never loads, so every doctor.ps1 edit would then report
the server as stale. deploy_freshness' own docstring records the same crying-wolf happening
with bench/, and CI made the point by failing the moment the import landed.

So the rule is three lines in main.py, and duplication without a pin is how two statements of
one rule quietly stop agreeing. This is the pin. It lives in its own file because
test_deploy_freshness.py's other tests leave global state that makes `import main` fail
there -- a shared fixture would have made this test's result depend on its neighbours.
"""
from __future__ import annotations

import os

import pytest

# main IS IMPORTED IN A MODULE-SCOPED FIXTURE, matching the neighbouring
# test_a_catalogue_you_cannot_search_is_not_a_catalogue.py. Importing it inside the test
# function instead made registration raise "Functions with *args are not supported as tools"
# from main.py:1043. I do not know why the two differ -- I guessed sys.path duplication and
# then MCP_ALLOWED_BASE, and measured both wrong -- so this copies the shape that works and
# says plainly that the cause is unexplained rather than inventing a third guess.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def main_mod():
    """Imported the way the neighbouring catalogue test imports it -- module scope, env set
    first. main.py reads MCP_API_KEY at import and raises without one; the value authenticates
    nothing here because nothing in this file starts a server or makes a request."""
    os.environ.setdefault("MCP_API_KEY", "test-placeholder-not-a-credential")
    import main
    return main


def test_main_agrees_with_the_canonical_staleness_rule(main_mod):
    main = main_mod
    from scripts.stale_server_check import classify_staleness

    real_boot, real_head = main._BOOT_HEAD, main._git_head_sha
    real_watched = main._watched_code_changed
    try:
        # Every combination that matters: agreeing, disagreeing, and each side unreadable.
        # "unknown" is a real state -- a detached head or a worktree reads as empty -- and it
        # must not collapse into either a pass or a failure.
        #
        # AND BOTH VALUES OF "DID THIS SERVER'S OWN CODE CHANGE", added 2026-09-17. A different
        # SHA used to be the whole rule, so a docs-only or cockpit-only commit reported the
        # server as stale and told the operator its fixes were not live -- three times in one
        # afternoon, each needing a person to clear it. Both statements of the rule now take
        # that into account and must still agree on every combination of the two.
        for boot, now in (("aaa", "aaa"), ("aaa", "bbb"), ("", "bbb"), ("aaa", ""), ("", "")):
            for watched in (True, False):
                main._BOOT_HEAD = boot
                main._git_head_sha = (lambda repo_root=None, _v=now: _v)
                main._watched_code_changed = (lambda _v=watched: _v)
                main._watched_cache["changed"] = None      # the cache must not answer for it
                assert main._server_identity()["server_code"] == \
                    classify_staleness(boot, now, True, watched_changed=watched), \
                    (boot, now, watched)
    finally:
        main._BOOT_HEAD, main._git_head_sha = real_boot, real_head
        main._watched_code_changed = real_watched
        main._watched_cache["changed"] = None


def test_a_commit_that_touches_nothing_the_server_loads_is_not_stale(main_mod):
    """THE CASE THAT MADE THIS CHANGE. A docs-only commit moves HEAD and changes nothing the
    running process imports; saying "fixes are not live until it restarts" is then false, and
    the person reading it learns to discount the dot."""
    main = main_mod
    real_boot, real_head = main._BOOT_HEAD, main._git_head_sha
    real_watched = main._watched_code_changed
    try:
        main._BOOT_HEAD = "aaa"
        main._git_head_sha = (lambda repo_root=None: "bbb")
        main._watched_code_changed = (lambda: False)
        main._watched_cache["changed"] = None
        assert main._server_identity()["server_code"] == "current"
        # ...and the opposite half, so this is not merely "it says current now".
        main._watched_code_changed = (lambda: True)
        main._watched_cache["changed"] = None
        assert main._server_identity()["server_code"] == "stale"
    finally:
        main._BOOT_HEAD, main._git_head_sha = real_boot, real_head
        main._watched_code_changed = real_watched
        main._watched_cache["changed"] = None


def test_an_unreadable_answer_still_reports_stale(main_mod):
    """Conservative where it matters: if the filesystem cannot be consulted, the SHA rule's
    answer stands. Reporting stale when it might be is the right side of a question about
    whether a fix is live."""
    main = main_mod
    real_newer = None
    try:
        import tools.deploy_freshness as DF
        real_newer = DF.newer_than

        def _boom(*_a, **_k):
            raise OSError("cannot walk the tree")

        DF.newer_than = _boom
        main._watched_cache["changed"] = None
        assert main._watched_code_changed() is True
    finally:
        if real_newer is not None:
            import tools.deploy_freshness as DF
            DF.newer_than = real_newer
        main._watched_cache["changed"] = None


def test_the_identity_block_names_the_process_and_its_uptime(main_mod):
    """The other half of what /health gained: WHICH server answered. The tunnel dot compares
    this pid against the one loopback reports, which is how "something answered" became "the
    thing I meant answered"."""
    main = main_mod

    ident = main._server_identity()
    assert ident["server_pid"] == os.getpid()
    assert ident["server_uptime_s"] >= 0
    assert ident["server_code"] in ("current", "stale", "unknown")

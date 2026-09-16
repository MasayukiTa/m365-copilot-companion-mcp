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
    try:
        # Every combination that matters: agreeing, disagreeing, and each side unreadable.
        # "unknown" is a real state -- a detached head or a worktree reads as empty -- and it
        # must not collapse into either a pass or a failure.
        for boot, now in (("aaa", "aaa"), ("aaa", "bbb"), ("", "bbb"), ("aaa", ""), ("", "")):
            main._BOOT_HEAD = boot
            main._git_head_sha = (lambda repo_root=None, _v=now: _v)
            assert main._server_identity()["server_code"] == \
                classify_staleness(boot, now, True), (boot, now)
    finally:
        main._BOOT_HEAD, main._git_head_sha = real_boot, real_head


def test_the_identity_block_names_the_process_and_its_uptime(main_mod):
    """The other half of what /health gained: WHICH server answered. The tunnel dot compares
    this pid against the one loopback reports, which is how "something answered" became "the
    thing I meant answered"."""
    main = main_mod

    ident = main._server_identity()
    assert ident["server_pid"] == os.getpid()
    assert ident["server_uptime_s"] >= 0
    assert ident["server_code"] in ("current", "stale", "unknown")

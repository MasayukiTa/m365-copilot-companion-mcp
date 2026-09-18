# -*- coding: utf-8 -*-
"""Staleness is a fact worth showing and not a condition a person must act on.

THE COMPLAINT, 2026-09-18, and it is correct. Every commit that touches a watched package makes
the running server genuinely stale, so on a machine where an agent is improving the code all day
the server dot is amber almost all of the time. In the operator's words: if that is the
condition, the colour is only a false report.

MY EARLIER DEFENCE WAS WRONG, and the way it was wrong is worth keeping. I argued the dot was a
TRUE report -- the process really is running older code -- and that is true about the fact and
beside the point about the dot. A colour that is on in the normal working state distinguishes
nothing, and a reader learns to clear it. That is the same failure this repository fixed for the
liveness-probe line (191 warnings, 100% of the warning volume, slightly ANTI-correlated with
trouble) and for the empty state that claimed there were no tasks.

WHAT MAKES IT ACTIONABLE. The supervisor cycles the server itself once the fleet is idle
(scripts/supervisor.ps1, Invoke-StaleServerCycle). So the condition a person can do something
about is not "stale", it is "stale for longer than the machine should have needed" -- the cycle
could not run, or ran and did not work. Below that threshold the fact is still shown, in the
detail line, on a green dot that says nothing is required.

WHAT IS PINNED HERE: the server reports HOW LONG, the cockpit colours on the duration rather
than the state, and an absent duration -- an older server that does not publish it -- is treated
as worth a colour rather than as zero. That last one matters: reading absence as zero would make
every stale server green forever, which is the opposite failure and a worse one.
"""
from __future__ import annotations

import io
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COCKPIT = os.path.join(REPO, "ui", "FleetCockpit.cs")


def _cs():
    """Cockpit source without // comment lines -- the prose here explains the mistake it fixed,
    and three checks in this repository tripped on their own explanations in one day."""
    src = io.open(COCKPIT, encoding="utf-8", errors="replace").read()
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))


@pytest.fixture(scope="module")
def main_mod():
    """MODULE SCOPE, copying tools/test_a_rule_stated_twice_must_be_pinned_once.py.

    Importing main inside a function-scoped fixture makes registration raise "Functions with
    *args are not supported as tools" from main.py. That file records the same thing happening
    and says plainly that the cause is unexplained -- so this copies the shape that works rather
    than inventing a fourth guess. MCP_API_KEY is read at import and authenticates nothing here:
    nothing in this file starts a server or makes a request."""
    os.environ.setdefault("MCP_API_KEY", "test-placeholder-not-a-credential")
    import main
    return main


# ---- the server reports how long ------------------------------------------------------------

def test_a_current_server_reports_no_stale_duration(main_mod):
    main = main_mod
    real_boot, real_head = main._BOOT_HEAD, main._git_head_sha
    try:
        main._BOOT_HEAD = "aaa"
        main._git_head_sha = (lambda repo_root=None: "aaa")
        main._STALE_SINCE = None
        out = main._server_identity()
        assert out["server_code"] == "current"
        assert out["server_stale_for_s"] is None
    finally:
        main._BOOT_HEAD, main._git_head_sha = real_boot, real_head
        main._STALE_SINCE = None


def test_a_stale_server_starts_the_clock_and_keeps_it(main_mod):
    """THE MEASUREMENT THE COLOUR NEEDS. Without it the panel cannot tell a commit that landed a
    moment ago from a server nothing has been able to cycle."""
    main = main_mod
    real_boot, real_head, real_watched = (main._BOOT_HEAD, main._git_head_sha,
                                          main._watched_code_changed)
    try:
        main._BOOT_HEAD = "aaa"
        main._git_head_sha = (lambda repo_root=None: "bbb")
        main._watched_code_changed = (lambda: True)
        main._watched_cache["changed"] = None
        main._STALE_SINCE = None

        first = main._server_identity()
        assert first["server_code"] == "stale"
        assert first["server_stale_for_s"] is not None
        assert first["server_stale_for_s"] >= 0

        started = main._STALE_SINCE
        main._watched_cache["changed"] = None
        second = main._server_identity()
        assert main._STALE_SINCE == started, "the clock restarted instead of running"
        assert second["server_stale_for_s"] >= first["server_stale_for_s"]
    finally:
        main._BOOT_HEAD, main._git_head_sha = real_boot, real_head
        main._watched_code_changed = real_watched
        main._watched_cache["changed"] = None
        main._STALE_SINCE = None


def test_becoming_current_again_clears_the_clock(main_mod):
    """A restart is what clears staleness, and the next stale period is a new one -- a clock
    that kept running across a cycle would report the machine had failed when it had worked."""
    main = main_mod
    real_boot, real_head, real_watched = (main._BOOT_HEAD, main._git_head_sha,
                                          main._watched_code_changed)
    try:
        main._BOOT_HEAD = "aaa"
        main._git_head_sha = (lambda repo_root=None: "bbb")
        main._watched_code_changed = (lambda: True)
        main._watched_cache["changed"] = None
        main._STALE_SINCE = None
        main._server_identity()
        assert main._STALE_SINCE is not None

        main._git_head_sha = (lambda repo_root=None: "aaa")
        main._watched_cache["changed"] = None
        out = main._server_identity()
        assert out["server_code"] == "current"
        assert out["server_stale_for_s"] is None
        assert main._STALE_SINCE is None
    finally:
        main._BOOT_HEAD, main._git_head_sha = real_boot, real_head
        main._watched_code_changed = real_watched
        main._watched_cache["changed"] = None
        main._STALE_SINCE = None


# ---- the cockpit colours on the duration ----------------------------------------------------

def test_the_dot_is_not_amber_merely_for_being_stale():
    """THE DEFECT. `codeState == "stale"` alone used to be the whole condition."""
    code = _cs()
    assert 'codeState == "stale" && StaleLongEnoughToMatter(srvBody)' in code, \
        "the amber branch no longer requires the duration"
    # and the plain-stale case must still be shown, on green, with its own line
    assert "hs_srv_detail_stale_recent" in code, \
        "a recently stale server says nothing at all now"


def test_the_threshold_is_past_a_healthy_cycle():
    """The supervisor checks about every 30 s and needs two consecutive observations before it
    cycles. A threshold near that would fire on every commit again."""
    code = _cs()
    m = re.search(r"STALE_AMBER_AFTER_S\s*=\s*([0-9.]+)", code)
    assert m, "the threshold is gone"
    assert float(m.group(1)) >= 300.0, "the threshold is short enough to fire on a normal commit"


def test_an_older_server_that_does_not_publish_the_duration_is_still_coloured():
    """Absence must not read as zero. It would make every stale server green forever -- the
    opposite failure, and worse than the one being fixed."""
    code = _cs()
    body = code[code.index("static bool StaleLongEnoughToMatter"):]
    body = body[:body.index("\n    static ", 10)] if "\n    static " in body[10:] else body
    assert "return true;" in body
    assert 'raw == "null"' in body, "a null duration is not handled"
    assert "TryParse" in body and "return true;" in body.split("TryParse", 1)[1][:400], \
        "an unparseable duration falls through to green"


def test_the_green_line_says_no_action_is_needed():
    """The point of the change: the fact is still reported, and it is reported as something the
    machine is handling."""
    code = _cs()
    i = code.index('"hs_srv_detail_stale_recent"')
    line = code[i:code.index("\n", i)]
    assert "自動" in line or "automatic" in line.lower()

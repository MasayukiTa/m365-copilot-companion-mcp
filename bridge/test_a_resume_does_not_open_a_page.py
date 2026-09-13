# -*- coding: utf-8 -*-
"""Resuming a conversation opened a browser tab, which made "resumable" untrue in practice.

`.fleet/history.json` was rebuilt from the ledgers that outlived it -- 3,469 conversations, every
one with its id. That restored the DATA and not the capability, and checking the GUI rather than
the data is what showed it:

    CopilotChat send, target has a ConvUrl  ->  GET /switch?url=...
    _do_switch                              ->  release_socket_driver(); _goto_settled(url)
    _do_resume, ref is a sessref            ->  _resume_to_ref -> PAGE.goto(AGENT_URL) + click

Both paths open a page, and `/switch` RELEASES the websocket first. So every archived
conversation was reachable only by the route that costs a tab -- and the archive's own ids, had
they gone into `conv_url`, would have turned 3,469 rows into 3,469 invitations to open one.

WHY THE PAGE WAS NEVER NEEDED FOR THE SOCKET. `ensure_driver()` builds its driver from
`S.load(ACTIVE_SID)["conv_url"]` -> guid -> `socket_route.driver_for(conversation_id=guid)`, and
that continues the conversation over the websocket with no page in it at all. `_do_resume` set
ACTIVE_SID only `if ok`, where `ok` meant the navigation had settled -- a precondition belonging
to the page path, applied to both.

CONTINUING BY ID IS MEASURED, NOT ASSUMED (socket_route.driver_for, 2026-08-24): a passphrase
planted in one process came back verbatim in a second that had only the id, across a fresh token
and a fresh session id, while the control arm -- an unused id -- answered "I do not know".

THE FALLBACK IS KEPT AND DEMOTED. A bridge that refuses to resume when the route is down is
worse than one that opens a tab. It is the exception now, and the response says `via` so a
caller can tell which happened instead of assuming.
"""
from __future__ import annotations

import ast
import io
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SRC = io.open(os.path.join(REPO, "bridge", "copilot_bridge.py"), encoding="utf-8").read()


def _resume_body():
    """The `_do_resume` handler, by name rather than by a line the change might move."""
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_do_resume":
            return ast.get_source_segment(SRC, node) or ""
    raise AssertionError("_do_resume is gone; re-derive this whole file")


# ── the socket comes first, and without a page ────────────────────────────────────────────

def test_a_sessref_asks_for_a_socket_before_anything_else():
    """THE DEFECT. A sessref is exactly what driver_for(conversation_id=) continues, and the
    handler navigated instead."""
    body = _resume_body()
    i_socket = body.index("_bridge_socket_driver()")
    i_page = body.index("_resume_to_ref(ref)")
    assert i_socket < i_page, "the page path is still reached before the socket is offered"


def test_the_socket_branch_touches_no_page_function():
    """`PAGE.goto`, `_goto_settled` and `_resume_to_ref` are the three ways this file opens a
    conversation in a browser. None may sit between choosing the socket and keeping it."""
    body = _resume_body()
    start = body.index("_bridge_socket_driver()")
    end = body.index("else:", start)
    between = body[start:end]
    for page_call in ("PAGE.goto", "_goto_settled", "_resume_to_ref", "_reap_orphan_tabs"):
        assert page_call not in between, "%s runs on the socket path" % page_call


def test_the_resident_tab_is_released_when_the_socket_takes_over():
    """"No tab needed for turns" was printed while a tab stayed resident once before, in
    ensure_driver. The same sentence has to be true here."""
    body = _resume_body()
    assert "release_resident_page(" in body


def test_the_caller_is_told_which_route_it_got():
    """A resume that silently fell back to a page is indistinguishable from one that did not,
    and "no fallback" is a condition somebody has to be able to CHECK."""
    body = _resume_body()
    assert '"via": via' in body or "'via': via" in body
    assert 'via = "page"' in body and 'via = "socket"' in body or '"socket"' in body


# ── the fallback is kept, because refusing is worse ───────────────────────────────────────

def test_the_page_path_still_exists_for_when_there_is_no_socket():
    body = _resume_body()
    assert "_resume_to_ref(ref)" in body, "the fallback was removed, not demoted"


def test_the_active_session_is_restored_when_the_socket_could_not_be_had():
    """Moving ACTIVE_SID and then failing would point the bridge at a conversation it cannot
    reach -- turns would land somewhere nobody is looking."""
    body = _resume_body()
    assert "ACTIVE_SID = _prev" in body


# ── the switch path is what it is, and is not used for this ───────────────────────────────

def test_switch_still_releases_the_socket_and_navigates():
    """NOT A BUG TO FIX HERE -- a fact to keep visible. /switch is the page path by
    construction, which is why resuming must not go through it. If this ever stops being
    true, the reasoning above needs re-deriving rather than quietly inheriting."""
    tree = ast.parse(SRC)
    body = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_do_switch":
            body = ast.get_source_segment(SRC, node) or ""
    assert body, "_do_switch is gone"
    assert 'release_socket_driver("/switch")' in body
    assert "_goto_settled(url)" in body

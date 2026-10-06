# -*- coding: utf-8 -*-
"""The sign-in check must not be able to open a tab.

WHAT HAPPENED. The fleet has been websocket-driven since the socket migration and opens NO TABS;
zero is the healthy state. To answer "is this profile signed in" on a machine with no tabs, a
probe was added that PUT /json/new -- which CREATES a tab. When the build ignores the url
parameter, or the close does not land, an about:blank tab is left in a browser that is supposed
to have none. One was found the next day, by the operator, not by any test.

The close was best-effort inside a finally, which is not a guarantee: an interrupted process or
a 404 both leave the tab. So the rule is not "close what you open" -- it is that a checker has no
way to open anything at all.

These read the AST, not the text. The file explains this history in a comment that necessarily
contains the string "/json/new", and a substring search matches the explanation and passes while
the call is still there.
"""
import ast
import io
import os

SRC = os.path.join(os.path.dirname(__file__), "ensure_m365_signin.py")


def _string_constants_in_calls(path):
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in list(node.args) + [k.value for k in node.keywords]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out.append(sub.value)
    return out


def test_the_check_cannot_open_a_browser_tab():
    """/json/new is the tab-creating endpoint. Nothing in a read-only check may reach it."""
    for value in _string_constants_in_calls(SRC):
        assert "/json/new" not in value, value


def test_the_check_cannot_close_a_browser_tab_either():
    """Closing exists only to clean up after opening. If it is here, something opens."""
    for value in _string_constants_in_calls(SRC):
        assert "/json/close" not in value, value


def test_the_comment_that_records_this_is_still_a_comment():
    """Guards the guard. These tests parse Calls precisely because the file names the endpoint
    in prose; if that prose ever disappears the next person loses the reason, and if a
    substring test were used instead it would match the prose and pass regardless."""
    text = io.open(SRC, encoding="utf-8").read()
    assert "/json/new" in text, "the explanation was removed"
    assert "/json/new" not in "".join(_string_constants_in_calls(SRC))


# ---- the window has to go back ------------------------------------------------

def test_the_surface_is_paired_with_a_rehide_on_every_exit():
    """AN ABOUT:BLANK WINDOW APPEARING NOW AND THEN, and this is where it came from.

    This helper calls edge_recover.surface() so a person can complete a sign-in. surface() kills
    a HEADLESS companion Edge and relaunches it HEADED, which starts on about:blank before it
    navigates -- and nothing put it back. Both exits returned straight out: `return 0` on
    success, `return 1` on timeout. So a headed Edge was left on screen and in the taskbar until
    something else happened to hide it.

    relay/edge_auth.py already states the rule and names where it was learned -- "EVERY
    surface() NEEDS A PAIRED REHIDE", after bridge/copilot_bridge.py's fire-and-forget one. This
    is the same defect in a third place.

    It is rare because start_all.ps1 escalates to this helper only on exit code 1, a real
    sign-in wall; "cannot tell" stays silent. That is why sampling window state for three hours
    caught nothing, and why it is pinned here by shape rather than by observation.
    """
    import re
    src = io.open(SRC, encoding="utf-8", errors="replace").read()
    assert "edge_recover.surface(" in src, "this test no longer describes the file"
    # the rehide must be in a finally, so success and timeout both reach it
    body = src[src.index("edge_recover.surface("):]
    assert re.search(r"finally:\s*\n\s*try:\s*\n\s*edge_recover\.rehide\(", body), \
        "the surface is not paired with a rehide on every exit path"


def test_the_rehide_happens_after_the_wait_not_during_it():
    """Hiding the window while somebody is typing an MFA code is the other way to get this
    wrong. The wait is its own function so the rehide can sit outside it."""
    src = io.open(SRC, encoding="utf-8", errors="replace").read()
    assert "def _wait_for_signin(" in src
    assert src.index("def _wait_for_signin(") > src.index("edge_recover.surface("), \
        "the wait must be called from the guarded block, not before the surface"
    wait_body = src[src.index("def _wait_for_signin("):]
    assert "rehide" not in wait_body, "the rehide is inside the wait loop"

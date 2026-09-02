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

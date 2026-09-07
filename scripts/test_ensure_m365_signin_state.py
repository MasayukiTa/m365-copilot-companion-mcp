# -*- coding: utf-8 -*-
"""state() must not call a tab "signed in" until it is actually past authentication.

WHAT THIS COVERS. test_ensure_m365_signin.py already proves the check cannot OPEN a tab.
This file proves the other half -- that it reads the tabs correctly -- by calling state()
against crafted tab lists instead of a live browser. The gap it closes: the old rule was
"any m365/copilot URL that is not a login wall means signed in", so an m365 tab still on the
account picker or the CsrToSSR bounce reported a machine as signed in when it was not.

The three answers are load-bearing and are asserted as three, not two:
  True  -> signed in           (the app is loaded)
  False -> a sign-in wall is up (send the person to sign in)
  None  -> cannot tell         (browser unreachable, or a tab that has not settled)
Collapsing None into False is the specific mistake the reason strings warn against, because
it tells somebody to sign in when there is nothing to act on.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ensure_m365_signin as m  # noqa: E402


def _patch_tabs(monkeypatch, urls):
    """Make state() see exactly these tab URLs (or None for an unreachable browser)."""
    if urls is None:
        monkeypatch.setattr(m, "tabs", lambda port: None)
    else:
        monkeypatch.setattr(m, "tabs", lambda port: [{"url": u} for u in urls])


def test_unreachable_browser_is_cannot_tell_not_not_signed_in(monkeypatch):
    _patch_tabs(monkeypatch, None)
    ready, why = m.state(9222)
    assert ready is None, why


def test_loaded_m365_chat_is_signed_in(monkeypatch):
    _patch_tabs(monkeypatch, ["https://m365.cloud.microsoft/chat"])
    ready, why = m.state(9222)
    assert ready is True, why


def test_copilot_page_is_signed_in(monkeypatch):
    _patch_tabs(monkeypatch, ["https://m365.cloud.microsoft/chat/?something", "https://www.bing.com/chat/copilot"])
    ready, why = m.state(9222)
    assert ready is True, why


def test_explicit_login_wall_is_not_signed_in(monkeypatch):
    _patch_tabs(monkeypatch, ["https://login.microsoftonline.com/common/oauth2/authorize?client_id=x"])
    ready, why = m.state(9222)
    assert ready is False, why


def test_adfs_wall_is_not_signed_in(monkeypatch):
    _patch_tabs(monkeypatch, ["https://adfs.contoso.com/adfs/ls/?wa=wsignin1.0"])
    ready, why = m.state(9222)
    assert ready is False, why


def test_no_m365_tab_is_cannot_tell(monkeypatch):
    # The fleet is websocket-driven and opens no tabs; a signed-in machine can show none.
    # Absence of an m365 tab is not evidence of anything, so it must be None, never False.
    _patch_tabs(monkeypatch, ["about:blank"])
    ready, why = m.state(9222)
    assert ready is None, why


def test_empty_tab_list_is_cannot_tell(monkeypatch):
    _patch_tabs(monkeypatch, [])
    ready, why = m.state(9222)
    assert ready is None, why


def test_m365_tab_on_csrtossr_bounce_is_not_yet_signed_in(monkeypatch):
    # THE FALSE POSITIVE. This URL is on the m365 host and carries no login-wall token, so the
    # old rule returned True. It is the mid-authentication bounce, not the loaded app: it must
    # NOT report signed in. None ("cannot tell / not settled") is the safe answer -- the caller
    # waits or surfaces the window rather than declaring setup done.
    _patch_tabs(monkeypatch, ["https://m365.cloud.microsoft/chat/?redirfrom=CsrToSSR&auth=2"])
    ready, why = m.state(9222)
    assert ready is not True, why


def test_m365_tab_on_account_picker_is_not_yet_signed_in(monkeypatch):
    _patch_tabs(monkeypatch, ["https://m365.cloud.microsoft/chat/?prompt=select_account"])
    ready, why = m.state(9222)
    assert ready is not True, why


def test_settled_m365_tab_wins_over_a_second_mid_auth_m365_tab(monkeypatch):
    # If ANY m365 tab has reached the app, the machine is signed in even while another m365 tab
    # is still bouncing -- the mid-auth guard only fires when EVERY m365 tab is unsettled.
    _patch_tabs(monkeypatch, [
        "https://m365.cloud.microsoft/chat/?redirfrom=CsrToSSR&auth=2",
        "https://m365.cloud.microsoft/chat",
    ])
    ready, why = m.state(9222)
    assert ready is True, why


def test_login_wall_beats_a_settled_m365_tab(monkeypatch):
    # A real wall anywhere is decisive: report not-signed-in even if a stale m365 tab lingers.
    _patch_tabs(monkeypatch, [
        "https://m365.cloud.microsoft/chat",
        "https://login.microsoftonline.com/common/oauth2/authorize",
    ])
    ready, why = m.state(9222)
    assert ready is False, why

# -*- coding: utf-8 -*-
"""The manual reconnect button sat amber from the moment the settings panel opened.

REPORTED BY THE OPERATOR: 「再接続が不要な時は灰色であるべき」against a settings panel where
`_reconnectChatBtn` (チャット再接続 / Reconnect chat) was permanently tinted `Theme.Warning`,
regardless of whether anything was actually wrong with the chat route. A control lit the same
way in every state stops being read as a state at all -- see the button's own construction
comment: "A warning that is always on is not a warning".

THE FIX, in `RefreshReconnectChatTint()`: the tint now follows the Tool dot (`_health[5]`) --
amber only when it reads Yellow or Red, muted otherwise. GRAY IS MUTED, NOT AMBER: gray means
the probe has not run or does not apply on this machine, which is "no opinion", not "trouble".
Treating gray as amber is exactly how the control ended up permanently lit, since an unprobed
machine starts in Gray and can live there for the whole session.

WHAT MUST NOT CHANGE ALONGSIDE THE COLOUR: the button stays clickable in every state. The
operator may want to force a reconnect on a machine whose self-probe is not active yet (where
the dot will never leave Gray), so `IsEnabled` must not be wired to the health state -- only to
the button's own busy/not-busy run guard.

CALL-SITE ORDERING: `ApplyHealthToUi()` has an early return (`if (_healthDot == null) return;`)
that fires on any machine where the health strip has not been built. The reconnect control lives
in the *settings* panel, built independently of the health strip, so the tint refresh must run
BEFORE that guard -- otherwise the button is stuck on its birth colour on exactly the machines
where the guard is reachable at all.

SOURCE ASSERTIONS: these read FleetCockpit.cs as text and cannot run the compiled control. What
they catch is the shape of regression that created this bug in the first place -- an
unconditional assignment, a Gray-is-amber branch, an early return placed before the refresh, or
an IsEnabled wired to the same state as the colour.
"""
from __future__ import annotations

import os
import re

UI = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(UI, "FleetCockpit.cs")


def _executable(cs: str) -> str:
    """`cs` with // and /* */ comments removed -- the prose above names every identifier this
    file asserts on, so without this the module could pass by matching its own explanation."""
    out, i, n = [], 0, len(cs)
    while i < n:
        if cs.startswith("//", i):
            j = cs.find("\n", i)
            i = n if j < 0 else j
        elif cs.startswith("/*", i):
            j = cs.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(cs[i]); i += 1
    return "".join(out)


def _src() -> str:
    with open(SRC, encoding="utf-8") as fh:
        return fh.read()


SOURCE = _executable(_src())


def _method_body(name_snippet: str) -> str:
    """Brace-matched body of the method whose declaration contains `name_snippet`."""
    i = SOURCE.index(name_snippet)
    i = SOURCE.index("{", i)
    depth = 0
    for j in range(i, len(SOURCE)):
        if SOURCE[j] == "{":
            depth += 1
        elif SOURCE[j] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[i:j + 1]
    raise AssertionError("unbalanced braces reading %r" % name_snippet)


def _tint_body() -> str:
    return _method_body("void RefreshReconnectChatTint()")


def test_the_tint_is_decided_by_the_tool_dot():
    """The regression: `Theme.Warning` assigned with no condition on `_health[5]` at all."""
    body = _tint_body()
    assert "_health[5]" in body or "_health [ 5 ]" in body, (
        "RefreshReconnectChatTint no longer reads the Tool dot -- re-derive this test if the "
        "dot index changed")
    m = re.search(r"Foreground\s*=\s*([^;]+);", body)
    assert m, "no Foreground assignment found in RefreshReconnectChatTint"
    assert m.group(1).strip() != "Theme.Br(Theme.Warning(_dark))", (
        "the button's Foreground is assigned Theme.Warning unconditionally -- this is the "
        "'always amber' regression")
    m2 = re.search(r"BorderBrush\s*=\s*([^;]+);", body)
    assert m2, "no BorderBrush assignment found in RefreshReconnectChatTint"
    assert m2.group(1).strip() != "Theme.Br(Theme.Warning(_dark))", (
        "the button's BorderBrush is assigned Theme.Warning unconditionally")
    # Both must be driven by the same computed variable, not by two independent literals.
    assert m.group(1).strip() == m2.group(1).strip(), (
        "Foreground and BorderBrush are tinted from different expressions -- they can disagree")


def test_gray_is_muted_not_amber():
    """Gray means 'no opinion' (unprobed / not applicable), not 'something is wrong'. Coding it
    as amber is exactly how the control came to be lit on every machine that had never been
    probed at all."""
    body = _tint_body()
    needed = re.search(r"\bneeded\s*=\s*([^;]+);", body)
    assert needed, "no 'needed' condition found -- re-derive this test if it was renamed"
    condition = needed.group(1)
    assert "HealthState.Gray" not in condition, (
        "Gray is included in the 'needed' (amber) condition -- gray must fall through to muted")
    assert "HealthState.Yellow" in condition and "HealthState.Red" in condition, (
        "amber is no longer tied to Yellow/Red -- re-derive this test if the rule changed")
    tint = re.search(r"\btint\s*=\s*([^;]+);", body)
    assert tint, "no 'tint' expression found"
    assert re.search(r"needed\s*\?\s*Theme\.Br\(Theme\.Warning\(_dark\)\)\s*:\s*Muted", tint.group(1)), (
        "the non-amber branch is not Muted -- Gray (and every other non-Yellow/Red state) must "
        "land on Muted, not on some other tint")


def test_is_enabled_is_not_wired_to_health_state():
    """The button must stay clickable in every health state -- that was a deliberate decision
    (the Tool dot can sit Gray forever on a machine without the self-probe feature, and the
    operator may still want to force a reconnect)."""
    tint_body = _tint_body()
    assert "IsEnabled" not in tint_body, (
        "RefreshReconnectChatTint touches IsEnabled -- the colour and the availability must not "
        "be decided by the same code path again")
    # Every IsEnabled write on this control must live inside the busy/not-busy run guard
    # (RunBridgeReconnectManual), not in anything conditioned on _health / HealthState.
    run_body = _method_body("void RunBridgeReconnectManual()")
    all_is_enabled = [m.start() for m in re.finditer(r"_reconnectChatBtn\.IsEnabled", SOURCE)]
    assert all_is_enabled, "no IsEnabled writes found on _reconnectChatBtn at all"
    run_start = SOURCE.index(run_body)
    run_end = run_start + len(run_body)
    for pos in all_is_enabled:
        assert run_start <= pos < run_end, (
            "an IsEnabled write on _reconnectChatBtn sits outside RunBridgeReconnectManual -- "
            "check it is not conditioned on the health state")
    assert "_health" not in run_body and "HealthState" not in run_body, (
        "RunBridgeReconnectManual now references health state -- IsEnabled must stay tied only "
        "to the button's own busy flag")


def test_the_refresh_runs_before_apply_health_to_uis_early_return():
    """`ApplyHealthToUi` bails out early on any machine where `_healthDot` is null. The reconnect
    control lives in the settings panel, built independently of the health strip, so putting the
    refresh after that guard leaves it stuck on its birth colour on exactly the machines where
    the guard is reachable -- which is the bug this reordering fixed."""
    body = _method_body("void ApplyHealthToUi()")
    call_pos = body.find("RefreshReconnectChatTint();")
    guard_pos = body.find("if (_healthDot == null) return;")
    assert call_pos != -1, "ApplyHealthToUi no longer calls RefreshReconnectChatTint()"
    assert guard_pos != -1, "the _healthDot null guard is gone -- re-derive this test"
    assert call_pos < guard_pos, (
        "RefreshReconnectChatTint() is called AFTER the early return in ApplyHealthToUi -- on "
        "any machine that takes that early return, the button will never be re-tinted")


def test_the_button_is_tinted_once_at_construction_too():
    """The dispatcher-driven refresh only fires on the next health poll; without an initial call
    at construction the button would show its default (born) colour until that poll lands."""
    i = SOURCE.index("_reconnectChatBtn = new Button();")
    j = SOURCE.index("col.Children.Add(_reconnectChatBtn);", i)
    construction_block = SOURCE[i:j]
    assert "RefreshReconnectChatTint();" in construction_block, (
        "the reconnect button is added to the panel without ever being tinted first")

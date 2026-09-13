# -*- coding: utf-8 -*-
"""AUTOFIX_MAX_ATTEMPTS bounded nothing, and scripts\\repair.ps1 relaunched without end.

WHAT WAS OBSERVED. Instances of `repair.ps1 -Auto -ResultJson` six seconds and 159 seconds old
in the same minute, windowless, nobody having asked for them.

WHY THE CAP DID NOT STOP IT. `MaybeAutoFix` gives each distinct fault its own budget of three
attempts -- a fix in its own right, because one persistent high-priority failure used to eat all
three and leave every later fault unrepaired. It identified the fault by the DOT: `"dot" + dot`.
But `RunFix` handles `server == Red || tunnel == Red` in ONE branch, so dots 0 and 1 are the
same repair seen through two probes. Any cause that takes the backend down reddens both, and
they flap between each other. Every flip changed the fault string, which reset the counter,
which handed out three more attempts. Measured in .fleet/autofix.jsonl, 2026-09-14 07:49-07:51:

    tunnel#1  tunnel#2  server#1  tunnel#1  tunnel#2  tunnel#3
                                  ^ the cap had been reached, and was then reset

roughly every 186 seconds -- repair.ps1's own run time plus the post-repair `_healthWake`
re-poll, not the nominal 15s health tick.

THE FIX IS THE IDENTITY, NOT A COOLDOWN. A delay between attempts would have made the same
unbounded loop slower, which is worse: it would have looked fixed. The budget is charged to the
repair that will actually run, so two dots sharing a repair share one budget and two dots with
different repairs keep their own -- preserving what the earlier fix was for.

WHAT THIS DOES NOT FIX, stated so nobody reads more into it. It bounds the RETRYING. It says
nothing about why server and tunnel went red: the server was logging `invalid_token (status=401)`
from the MCP SDK's own bearer middleware throughout that window, `/health` answered 200 the whole
time, and the caller presenting the bad token was not identified. That remains open.
"""
from __future__ import annotations

import io
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COCKPIT = os.path.join(REPO, "ui", "FleetCockpit.cs")


def _src():
    return io.open(COCKPIT, encoding="utf-8").read()


def _body(signature, src=None):
    """One method's text, located by its own signature.

    An anchor on a line INSIDE the method is an anchor that breaks when that line is edited --
    and it then reads as the property having broken rather than the anchor."""
    src = src if src is not None else _src()
    i = src.index(signature)
    j = src.index("\n    ", src.index("\n", i))
    # walk to the next member declaration at class indentation
    m = re.search(r"\n    (?:static |void |bool |int |string |public |private )", src[i + 1:])
    j = i + 1 + m.start() if m else len(src)
    return src[i:j]


# ── the merge is grounded in RunFix, not in a claim about it ──────────────────────────────

def test_server_and_tunnel_really_are_one_repair():
    """THE PREMISE, CHECKED. Merging two budgets is only right if the two dots lead to the same
    repair, and that is a fact about RunFix rather than about this test. If somebody gives the
    tunnel its own tier, this fails and the merge below has to be revisited -- which is the
    whole point of asserting a premise instead of assuming it."""
    src = _src()
    assert "if (server == HealthState.Red || tunnel == HealthState.Red)" in src, (
        "server and tunnel no longer share a RunFix branch -- AutoFixRepairKey merges two "
        "budgets on the strength of that branch and must be re-derived")


def test_the_two_dots_that_share_a_repair_share_a_budget():
    body = _body("static string AutoFixRepairKey(int dot)")
    assert "dot == 0 || dot == 1" in body, "the shared-repair pair is no longer merged"
    assert "repair:stack" in body


def test_dots_with_different_repairs_keep_their_own_budget():
    """The other half, and the reason this is not just `return "repair"`. sign-in, edge and
    agent take different RunFix branches with different arguments; collapsing them would restore
    the defect the per-fault budget was introduced to fix, from the opposite direction."""
    body = _body("static string AutoFixRepairKey(int dot)")
    assert "repair:dot" in body, "every other dot must keep a distinct key"
    # sign-in (3) and edge (2) both relaunch :9222 and might LOOK mergeable; they are not
    # merged, because they take different branches. Inference is what this must not do.
    assert "dot == 2" not in body and "dot == 3" not in body


def test_the_reset_is_keyed_on_the_repair_and_not_on_the_dot():
    """THE DEFECT ITSELF. `string fault = "dot" + dot;` is what reset the counter on every flip.

    Anchored on the assignment to `fault`, which is the thing whose identity is in question --
    not on the `_autoFixAttempts = 0` beside it, which is correct and unchanged."""
    body = _body("void MaybeAutoFix()")
    assert 'string fault = AutoFixRepairKey(dot);' in body, (
        "the retry budget is keyed on the observation again, so any two dots that share a "
        "repair will hand each other a fresh budget for ever")
    assert 'string fault = "dot" + dot;' not in body


def test_the_cap_is_still_consulted():
    """A budget nobody checks is the same as no budget. Cheap, and it would have caught a fix
    that renamed the key and dropped the comparison.

    THE FIRST DRAFT ASSERTED A SPELLING and failed on the declaration's column alignment --
    `AUTOFIX_MAX_ATTEMPTS      = 3;`. The property is "it is declared and it is a small positive
    number", so that is what is matched."""
    body = _body("void MaybeAutoFix()")
    assert "_autoFixAttempts >= AUTOFIX_MAX_ATTEMPTS" in body
    m = re.search(r"AUTOFIX_MAX_ATTEMPTS\s*=\s*(\d+)", _src())
    assert m, "the cap is no longer declared"
    assert 1 <= int(m.group(1)) <= 10, "an unattended repair budget of %s" % m.group(1)


def test_a_green_streak_is_what_clears_a_budget():
    """The one legitimate reset, which must survive this change: a fault that actually recovers
    gives its budget back after AUTOFIX_GREEN_POLLS_TO_RESET consecutive green polls. Without
    it the cap would be permanent and a machine that healed would stay unrepaired."""
    body = _body("void MaybeAutoFix()")
    assert "_autoFixGreenPolls >= AUTOFIX_GREEN_POLLS_TO_RESET" in body
    assert "_autoFixAttempts = 0" in body.split("_autoFixGreenPolls >=", 1)[1][:400]

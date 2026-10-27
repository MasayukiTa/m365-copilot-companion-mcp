"""docs/research/fleet_operation_routes.md must carry two things the owner asked for in
row #120 of the batch-08 ledger ("GUI に見える形での投入方法と、不変則 3 の修正をよろしく。
実態に合っていないので"):

  1. A described, human-visible (GUI) submission route -- i.e. the cockpit UI path through
     scripts/win/submit_via_ui.ps1, with the mandatory -ReadOnly dry run first and the
     InvokePattern (not keystroke) mechanism that replaced Ctrl+Enter on 2026-08-30.

  2. Invariant #3 reconciled with reality. The old text asserted a flat "anything submitted
     while running becomes a steer" (必ず steer になる). That was wrong: what a mid-run
     submission becomes depends on the ROUTE, and the document now says so and lists the
     branches in a table. This test pins the correction so a future edit cannot quietly
     regress invariant #3 back to the flat, wrong claim.

WHAT THIS DOES AND DOES NOT CATCH. It is a content-contract test on one prose document: it
asserts that the specific, verified corrections are present and that the old wrong wording
is explicitly marked as wrong. It does not re-verify the file:line citations inside the
document (that is the job of the architecture-docs existence test) and it does not execute
the PowerShell route.
"""
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(REPO, "docs", "research", "fleet_operation_routes.md")


@pytest.fixture(scope="module")
def text():
    # docs/research/ is .gitignored (an uncommitted local research tree), so this document is
    # not present in a clean CI checkout. When it is absent the content contract cannot be
    # evaluated, so skip rather than fail: a red CI here would be reporting the gitignore
    # policy, not a regression in the document. On the owner's machine, where the file lives,
    # the assertions run for real.
    if not os.path.isfile(DOC):
        pytest.skip("docs/research/fleet_operation_routes.md is not present (gitignored tree)")
    with open(DOC, encoding="utf-8-sig") as fh:
        return fh.read()


def test_gui_visible_submission_route_is_described(text):
    """Row #120 asked for the submission method 'as it is visible in the GUI'. The only route
    that puts text where a human types and clicks the button a human clicks is the cockpit UI
    route via submit_via_ui.ps1. Assert the document names that route and the section that
    frames it as the human-visible one."""
    assert "scripts/win/submit_via_ui.ps1" in text or "submit_via_ui.ps1" in text, (
        "the GUI submission script submit_via_ui.ps1 is not named in the document"
    )
    # The section that explicitly frames route A as the human-visible submission.
    assert "GUI に見える形での投入" in text, (
        "the heading that frames the UI route as 'GUI に見える形での投入' is missing"
    )


def test_gui_route_requires_readonly_dry_run_first(text):
    """The documented GUI procedure must still require the -ReadOnly dry run before any real
    submission -- that is the step that catches a broken cockpit or a missing goal field
    before a single character is submitted."""
    assert "-ReadOnly" in text, "the mandatory -ReadOnly dry-run step is not documented"
    assert "-Goal" in text, "the -Goal submission argument is not documented"


def test_gui_route_uses_invokepattern_not_keystrokes(text):
    """The 2026-08-30 rewrite replaced the Ctrl+Enter keystroke with InvokePattern on the
    Start/Send button. The document must reflect that mechanism, because 'sends the same keys
    a human does' is exactly the wrong mental model that produced silent zero-submit failures.
    """
    assert "InvokePattern" in text, "the InvokePattern button mechanism is not documented"


def test_invariant3_rejects_the_flat_always_steer_claim(text):
    """Invariant #3 must no longer assert the flat 'always becomes a steer'. The corrected
    text keeps the old wording only to mark it as wrong (誤り). Assert both the correction
    verdict and that the old wording is attributed to the past, not stated as current truth.
    """
    # The corrected invariant explicitly calls the flat claim an error.
    assert "「必ず steer になる」は誤り" in text, (
        "invariant #3 no longer states that '必ず steer になる' is an error -- the "
        "flat-always-steer claim may have regressed"
    )
    # The old wording survives only under a past-tense attribution ('以前この項は ... と書いていた').
    assert "以前この項は" in text and "と書いていた" in text, (
        "the past-tense attribution that quarantines the old wrong wording is missing"
    )


def test_invariant3_enumerates_route_dependent_outcomes(text):
    """The correction's substance is that the outcome depends on the route and there is more
    than one. Assert the document states the outcome is split (not one) and shows the
    branches that distinguish steer from a new worker."""
    assert "経路で決まる" in text, (
        "invariant #3 no longer says the outcome is decided by the route"
    )
    # Both distinct outcomes must appear: becomes a steer, and becomes a new worker.
    assert "steer になる" in text, "the 'becomes a steer' outcome is missing from invariant #3"
    assert "新しいワーカーになる" in text, (
        "the 'becomes a new worker' outcome is missing from invariant #3"
    )


def test_invariant3_separates_fleet_submit_from_the_four_routes(text):
    """fleet_submit is none of the mid-run routes: it only queues. The document must keep that
    distinction so a reader does not think calling fleet_submit starts anything."""
    assert "fleet_submit" in text, "fleet_submit is not mentioned in invariant #3"
    assert "キューに積むだけ" in text, (
        "the clarification that fleet_submit only queues (does not run) is missing"
    )

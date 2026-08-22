"""One dashboard window, however many times the constitution is re-signed.

Measured: a working day produced 24 re-signings, the notification path opened the
authority dashboard on every one of them, and 24 identical windows were sitting on the
desktop. The operator's reaction was to close them all -- which is worse than never
notifying, because the one that mattered goes with the rest.

The --approval-gate path had held a mutex for exactly this reason since it was written,
and the comment above --authority claimed it was built to the same shape. It was not:
the mutex was the mechanism, and it was the part left out.

Source-level checks, like ui/test_fleet_cockpit_approval_center.py: the C# is not
importable from pytest, so the wiring is asserted on the file.

Run: pytest -q ui/test_fleet_cockpit_authority_window.py
"""
from pathlib import Path

UI = Path(__file__).parent
COCKPIT = (UI / "FleetCockpit.cs").read_text(encoding="utf-8")
DASH = (UI / "SelfImproveDashboard.cs").read_text(encoding="utf-8")


def _authority_branch() -> str:
    body = COCKPIT[COCKPIT.index('args[0].Equals("--authority"'):]
    return body[:body.index("string path = args.Length")]


def test_the_authority_branch_takes_a_single_instance_mutex():
    body = _authority_branch()
    assert "new Mutex(true," in body
    assert "M365CompanionAuthorityDashboard" in body


def test_it_does_not_share_the_approval_gates_mutex_name():
    """Sharing one name would make an open approval prompt suppress the dashboard, and
    the two windows answer different questions."""
    assert COCKPIT.count("M365CompanionApprovalPrompt") == 1
    assert COCKPIT.count("M365CompanionAuthorityDashboard") == 1


def test_a_second_launch_raises_the_open_window_instead_of_returning_silently():
    """--approval-gate may return silently because the running prompt polls the gate
    directory. Nothing polls here, so a silent return is a notification whose click does
    nothing visible -- which is the complaint this whole path exists to answer."""
    body = _authority_branch()
    assert "RaiseExistingDashboard()" in body
    assert "if (!ownsMutex) return;" not in body


def test_the_raise_is_scoped_to_the_dashboard_window():
    """The same exe also runs as the ordinary cockpit and as the approval prompt, so
    matching on process name alone would drag the wrong window to the front."""
    body = COCKPIT[COCKPIT.index("static void RaiseExistingDashboard"):]
    body = body[:body.index("\n}")]
    assert "SelfImproveDashboardWindow.WindowTitle" in body
    assert "SetForegroundWindow" in body


def test_the_matched_title_is_the_one_the_window_actually_sets():
    """A literal copied into the matcher would drift the first time the title changed."""
    assert 'public const string WindowTitle = "Self-Improvement";' in DASH
    assert "Title = WindowTitle;" in DASH


def test_failing_to_find_the_handle_does_not_open_a_second_window():
    """Best effort by design: a window that did not come forward is a smaller harm than
    a second one appearing."""
    body = COCKPIT[COCKPIT.index("static void RaiseExistingDashboard"):]
    body = body[:body.index("\n}")]
    assert "catch { }" in body
    assert "new Application()" not in body

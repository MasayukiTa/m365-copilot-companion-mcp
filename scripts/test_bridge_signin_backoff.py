"""While the bridge Edge is on a sign-in page the watchdog must back off: it must NOT restart the
bridge or open new tabs in a loop. A wedged wall used to drive a restart every one to two
minutes, each opening fresh tabs. The need is stated as one line and recorded for the doctor.

The back-off decision lives in a pure function (Test-WatchdogMayAct) so it can be checked here
without a browser or a clock. The default window is 600s (10 min), tunable via
MCP_BRIDGE_SIGNIN_BACKOFF_SEC.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _src() -> str:
    return (ROOT / "scripts" / "start_bridge.ps1").read_text(encoding="utf-8")


def _func(src: str, name: str) -> str:
    start = src.index("function " + name)
    return src[start:src.index("\nfunction ", start + 10)]


def test_backoff_decider_exists_and_is_pure():
    body = _func(_src(), "Test-WatchdogMayAct")
    # No wall -> never act.
    assert "if (-not $WallUp) { return $false }" in body
    # First surface allowed (null last-surface), then gated on the back-off window.
    assert "$null -eq $LastSurfaceEpoch" in body
    assert "$BackoffSec" in body
    # Pure: it must not reach for the browser or the filesystem.
    for forbidden in ("Test-Cdp", "Needs-SignIn", "Ensure-Edge", "Get-Content", "Set-Content"):
        assert forbidden not in body, f"Test-WatchdogMayAct is not pure: calls {forbidden}"


def test_wall_branch_backs_off_and_does_not_restart_or_open_tabs():
    src = _src()
    wall_at = src.index("if ($signInWall) {")
    cont_at = src.index("continue", wall_at)
    branch = src[wall_at:cont_at]
    assert "Test-WatchdogMayAct" in branch, "the wall branch does not apply the back-off"
    assert "Ensure-Edge" not in branch, \
        "the watchdog restarts the Edge while a sign-in wall is up (the restart loop)"
    assert "Format-SigninNeededLine" in branch, "the wall branch does not state the one clear line"
    assert "bridge_signin_needed" in branch, "the need is not recorded for the doctor"


def test_backoff_default_is_ten_minutes_and_tunable():
    src = _src()
    assert "$signInBackoffSec = 600" in src, "the back-off default is not 10 minutes"
    assert "MCP_BRIDGE_SIGNIN_BACKOFF_SEC" in src, "the back-off window cannot be tuned"


def test_one_clear_line_names_the_profile_and_port():
    body = _func(_src(), "Format-SigninNeededLine")
    assert "$Profile" in body and "$CdpPort" in body, \
        "the one clear line does not name which Edge needs the sign-in"

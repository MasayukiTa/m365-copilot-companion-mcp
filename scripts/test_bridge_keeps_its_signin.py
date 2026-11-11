"""start_bridge.ps1 must not throw away the bridge Edge's sign-in after every sign-in, and
must not restart the bridge in a loop while a sign-in wall is up.

THE DEFECT. After a sign-in the keepalive supervisor quit the bridge Edge and relaunched it
headless (Demote-ToHeadless -> Ensure-Edge -Hard). Quitting the browser dropped the session
cookies the sign-in had just established, so the sign-in page came straight back -- measured on
a fresh PC, the wall reappeared after every single sign-in. On top of that the watchdog loop,
seeing a wall every pass, restarted the bridge every one to two minutes, each restart opening
new tabs.

THE FIX, asserted here as SOURCE shape (the Linux CI runner has no Edge, no venv and no Windows,
so it cannot execute the script; the behaviour itself was verified by hand on Windows):

  (1) The default demote path keeps the signed-in process alive and only MINIMISES its window
      (Minimize-BridgeEdge), so the cookies -- and the sign-in -- survive. The old
      quit-and-relaunch (Demote-ToHeadless / Ensure-Edge -Hard) is reachable only when the
      caller opts in with MCP_BRIDGE_HARD_DEMOTE=1 (Use-HardDemote).

  (2) The Edge profile can be overridden from the environment (MCP_BRIDGE_PROFILE), alongside
      the CDP port which already reads MCP_BRIDGE_CDP_PORT -- so the bridge can run on a profile
      and port that do not collide with another managed Edge, and the defaults are unchanged.

  (3) While a sign-in wall is up the watchdog applies a back-off (default 600s): between surfaces
      it does NOT restart the bridge Edge or open new tabs. The need is stated as ONE line in
      the log and recorded for the doctor to read.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _src() -> str:
    return (ROOT / "scripts" / "start_bridge.ps1").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (1) The sign-in survives: minimise, do not quit, unless hard demote is asked for.
# ---------------------------------------------------------------------------

def test_a_minimise_helper_exists_and_does_not_relaunch():
    """Minimize-BridgeEdge must take the window off the desk WITHOUT quitting or relaunching
    the Edge -- that is the whole point of keeping the session. It must not call Ensure-Edge
    (which is the quit-and-relaunch path) in its body."""
    src = _src()
    assert "function Minimize-BridgeEdge" in src
    body = src[src.index("function Minimize-BridgeEdge"):]
    body = body[:body.index("\nfunction ", 10)]
    assert "ShowWindowAsync" in body, "Minimize-BridgeEdge does not minimise a real window"
    assert "Ensure-Edge" not in body, \
        "Minimize-BridgeEdge relaunches the Edge -- that drops the session it is meant to keep"


def test_hard_demote_is_opt_in_via_the_environment():
    src = _src()
    assert "function Use-HardDemote" in src
    body = src[src.index("function Use-HardDemote"):]
    body = body[:body.index("\nfunction ", 10)]
    assert "MCP_BRIDGE_HARD_DEMOTE" in body, \
        "hard demote is not gated on MCP_BRIDGE_HARD_DEMOTE"


def test_show_signin_minimises_by_default_and_only_hard_demotes_on_opt_in():
    """After bringing the sign-in window forward, the default branch must wait and then minimise
    (Wait-SignInThenMinimize), keeping the session; the quit-and-relaunch Demote-ToHeadless may
    run ONLY under Use-HardDemote."""
    src = _src()
    body = src[src.index("function Show-SignIn"):]
    body = body[:body.index("\nfunction ", 10)]
    assert "if (Use-HardDemote) {" in body, "Show-SignIn does not gate the demote on Use-HardDemote"
    assert "Wait-SignInThenMinimize" in body, \
        "Show-SignIn does not take the session-preserving (minimise) path by default"
    # The destructive demote is on the hard-demote side of the branch, not the default.
    hard_idx = body.index("if (Use-HardDemote) {")
    else_idx = body.index("} else {", hard_idx)
    assert "Demote-ToHeadless" in body[hard_idx:else_idx], \
        "Demote-ToHeadless must be the hard-demote branch only"
    assert "Demote-ToHeadless" not in body[else_idx:], \
        "the default (non-hard) branch must not quit-and-relaunch the Edge"


def test_wait_then_minimise_keeps_the_session():
    """Wait-SignInThenMinimize is the default completion path. It must end by minimising, never
    by Ensure-Edge -Hard (quit+relaunch)."""
    src = _src()
    assert "function Wait-SignInThenMinimize" in src
    body = src[src.index("function Wait-SignInThenMinimize"):]
    body = body[:body.index("\nfunction ", 10)]
    assert "Minimize-BridgeEdge" in body
    assert "Ensure-Edge -Hard" not in body, \
        "the default completion path quits and relaunches the Edge, dropping the session"


def test_the_idle_headed_branch_minimises_not_relaunches_by_default():
    """The top-of-loop net for a headed Edge with no wall must minimise by default and only
    quit-and-relaunch under hard demote."""
    src = _src()
    # The branch that handles 'headed but no wall'.
    anchor = "Edge-IsHeaded) -and -not (Needs-SignIn)"
    assert anchor in src
    seg = src[src.index(anchor):]
    seg = seg[:seg.index("Run-BridgeWatched", 1)]
    assert "Minimize-BridgeEdge" in seg
    assert "if (Use-HardDemote)" in seg


# ---------------------------------------------------------------------------
# (2) Profile and CDP port are environment-overridable; defaults unchanged.
# ---------------------------------------------------------------------------

def _param_block() -> str:
    """The text of the top-level param(...) block. The closing paren is found as the first ')'
    that stands alone at the start of a line, so the ')' inside each parameter default
    ($(if (...) {...} else {...})) does not end the block early."""
    src = _src()
    start = src.index("param(")
    end = src.index("\n)", start)
    return src[start:end]


def test_profile_is_overridable_from_the_environment_default_unchanged():
    param = _param_block()
    assert "MCP_BRIDGE_PROFILE" in param, "the Edge profile cannot be set from the environment"
    assert "copilot-bridge-edge" in param, "the default profile name changed"


def test_cdp_port_is_overridable_from_the_environment_default_unchanged():
    param = _param_block()
    assert "MCP_BRIDGE_CDP_PORT" in param
    assert "9223" in param, "the default CDP port changed"


# ---------------------------------------------------------------------------
# (3) The watchdog backs off while a sign-in wall is up: no restart, no new tabs.
# ---------------------------------------------------------------------------

def test_a_backoff_decider_exists_and_is_pure():
    """Test-WatchdogMayAct decides whether the watchdog may act again. It must depend only on
    its arguments (a wall flag, the last-surface epoch, now, and the back-off window) so it can
    be reasoned about and unit-tested, and it must gate on the back-off seconds."""
    src = _src()
    assert "function Test-WatchdogMayAct" in src
    body = src[src.index("function Test-WatchdogMayAct"):]
    body = body[:body.index("\nfunction ", 10)]
    # No wall -> never act.
    assert "if (-not $WallUp) { return $false }" in body
    # First surface is allowed, then gated on the back-off window.
    assert "$BackoffSec" in body


def test_the_loop_backs_off_and_does_not_restart_or_open_tabs_during_a_wall():
    """While a wall is up, the loop must use Test-WatchdogMayAct and must NOT call Ensure-Edge
    (restart) in that branch. It states one line and records the need for the doctor."""
    src = _src()
    # The wall branch: from the wall test to its 'continue'.
    wall_at = src.index("if ($signInWall) {")
    cont_at = src.index("continue", wall_at)
    branch = src[wall_at:cont_at]
    assert "Test-WatchdogMayAct" in branch, "the wall branch does not apply the back-off"
    assert "Ensure-Edge" not in branch, \
        "the watchdog restarts the Edge while a sign-in wall is up (the restart loop)"
    assert "Format-SigninNeededLine" in branch, "the wall branch does not state the one clear line"
    assert "bridge_signin_needed" in branch, \
        "the sign-in need is not recorded where the doctor can read it"


def test_the_backoff_window_defaults_to_ten_minutes_and_is_tunable():
    src = _src()
    assert "$signInBackoffSec = 600" in src, "the back-off default is not 10 minutes"
    assert "MCP_BRIDGE_SIGNIN_BACKOFF_SEC" in src, "the back-off window cannot be tuned"

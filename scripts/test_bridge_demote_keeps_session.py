"""After a sign-in the bridge Edge must KEEP its session: the default demote path minimises the
signed-in window instead of quitting and relaunching the browser (which dropped the cookies the
sign-in had just set, so the wall came straight back). Quit-and-relaunch is opt-in only via
MCP_BRIDGE_HARD_DEMOTE=1.

Source-shape assertions: the Linux CI runner has no Edge/venv/Windows, so it cannot run the
script; the behaviour was verified by hand on Windows.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _src() -> str:
    return (ROOT / "scripts" / "start_bridge.ps1").read_text(encoding="utf-8")


def _func(src: str, name: str) -> str:
    start = src.index("function " + name)
    return src[start:src.index("\nfunction ", start + 10)]


def test_minimise_helper_does_not_quit_or_relaunch():
    body = _func(_src(), "Minimize-BridgeEdge")
    assert "ShowWindowAsync" in body, "Minimize-BridgeEdge does not minimise a real window"
    assert "Ensure-Edge" not in body, \
        "Minimize-BridgeEdge relaunches the Edge -- that drops the session it is meant to keep"


def test_hard_demote_is_opt_in():
    body = _func(_src(), "Use-HardDemote")
    assert "MCP_BRIDGE_HARD_DEMOTE" in body, "hard demote is not gated on the environment"


def test_show_signin_minimises_by_default_and_hard_demotes_only_on_opt_in():
    body = _func(_src(), "Show-SignIn")
    assert "if (Use-HardDemote) {" in body
    assert "Wait-SignInThenMinimize" in body, "default path does not preserve the session"
    hard = body.index("if (Use-HardDemote) {")
    els = body.index("} else {", hard)
    assert "Demote-ToHeadless" in body[hard:els], "quit/relaunch must be the hard-demote branch"
    assert "Demote-ToHeadless" not in body[els:], "default branch must not quit/relaunch"


def test_wait_then_minimise_ends_by_minimising_not_relaunching():
    body = _func(_src(), "Wait-SignInThenMinimize")
    assert "Minimize-BridgeEdge" in body
    assert "Ensure-Edge -Hard" not in body, \
        "the default completion path quits and relaunches the Edge, dropping the session"


def test_idle_headed_branch_minimises_by_default():
    src = _src()
    anchor = "Edge-IsHeaded) -and -not (Needs-SignIn)"
    seg = src[src.index(anchor):]
    seg = seg[:seg.index("Run-BridgeWatched", 1)]
    assert "Minimize-BridgeEdge" in seg
    assert "if (Use-HardDemote)" in seg, "the idle-headed net is not gated on hard demote"

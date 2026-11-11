import pathlib

# Guard: the long-running bridge launch must be windowless by DEFAULT, and MCP_SHOW_CONSOLE=1
# must be the single documented escape hatch that makes it visible for debugging. This locks
# the contract described in the task so a later edit cannot silently drop either half.

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


def test_bridge_default_launch_is_windowless():
    s = _read("scripts/start_bridge.ps1")
    # The default (no env) branch launches the bridge python with -NoNewWindow.
    assert "-NoNewWindow -PassThru" in s, "default bridge launch must stay -NoNewWindow"


def test_mcp_show_console_is_the_debug_escape_hatch():
    s = _read("scripts/start_bridge.ps1")
    assert "MCP_SHOW_CONSOLE" in s, "MCP_SHOW_CONSOLE debug switch must exist"
    # When the switch is on, the visible launch must NOT pass -NoNewWindow.
    i = s.find("MCP_SHOW_CONSOLE set --")
    assert i != -1, "visible-console branch must be present"
    branch = s[i:i+400]
    assert "-NoNewWindow" not in branch.split("else")[0], "visible branch must drop -NoNewWindow"


def test_supervisor_and_start_all_hide_their_windows():
    for rel in ("scripts/start_all.ps1", "scripts/supervisor.ps1"):
        s = _read(rel)
        assert "WindowStyle Hidden" in s, rel + " must launch hidden"
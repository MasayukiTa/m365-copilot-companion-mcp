"""The bridge Edge's profile and CDP port must be overridable from the environment, so a second
managed Edge on the box does not collide with the bridge -- and the defaults must not change.

  MCP_BRIDGE_PROFILE   -> -Profile   (default 'copilot-bridge-edge')
  MCP_BRIDGE_CDP_PORT  -> -CdpPort   (default 9223)

Source-shape assertions: the Linux CI runner has no Edge/venv/Windows, so it cannot run the
script; the behaviour was verified by hand on Windows.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _param_block() -> str:
    src = (ROOT / "scripts" / "start_bridge.ps1").read_text(encoding="utf-8")
    start = src.index("param(")
    end = src.index("\n)", start)
    return src[start:end]


def test_profile_reads_mcp_bridge_profile_default_unchanged():
    param = _param_block()
    assert "$Profile" in param
    assert "MCP_BRIDGE_PROFILE" in param, "the Edge profile cannot be set from the environment"
    assert "copilot-bridge-edge" in param, "the default profile name changed"


def test_cdp_port_reads_mcp_bridge_cdp_port_default_unchanged():
    param = _param_block()
    assert "$CdpPort" in param
    assert "MCP_BRIDGE_CDP_PORT" in param, "the CDP port cannot be set from the environment"
    assert "9223" in param, "the default CDP port changed"


def test_bridge_port_default_unchanged():
    """The bridge's own HTTP port default must not drift while we touch the param block."""
    param = _param_block()
    assert "MCP_BRIDGE_PORT" in param
    assert "8765" in param, "the default bridge port changed"

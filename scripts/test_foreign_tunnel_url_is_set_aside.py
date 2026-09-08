# -*- coding: utf-8 -*-
"""A tunnel URL from another machine must not survive a FAILED provisioning run.

The fix that stamps MCP_TUNNEL_HOST is not enough on its own, and the reason is the order of
events on the machine where this was reported. setup_devtunnel.ps1 has sixteen `exit 1` paths
before the block that rewrites .env, and the first two are "CLI not installed" and "not signed
in" -- which is exactly the state of a fresh PC. So the run failed early, .env was never
touched, the inherited URL stood, quickstart's readiness gate saw a non-empty value and let
the operator continue, and copilot_studio_values.ps1 printed the old machine's address to
paste into Copilot Studio.

Section 0 of the script therefore runs BEFORE anything that can exit, and sets a foreign URL
aside. The behaviour was measured on this machine against five .env shapes (foreign stamp,
own stamp, no stamp, no URL, case-flipped stamp); what is held here is the ordering and the
shape, which is what a later edit would break.
"""
import io
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PS = ROOT / "scripts" / "setup_devtunnel.ps1"
QS = ROOT / "quickstart.bat"


@pytest.fixture(scope="module")
def src():
    if not PS.is_file():
        pytest.skip("setup_devtunnel.ps1 not in this checkout")
    return io.open(PS, encoding="utf-8", errors="replace").read()


def test_the_guard_runs_before_anything_that_can_exit(src):
    """ORDER IS THE WHOLE POINT. Placed after the CLI or sign-in checks it would never run on
    the machine that needs it."""
    guard = src.index("# --- 0. a URL this machine did not mint")
    first_exit = re.search(r"^\s*exit 1\s*$", src, re.M)
    assert first_exit, "no exit path found; this test no longer measures what it claims"
    assert guard < first_exit.start(), "the guard is reachable only after a failure can exit"
    assert guard < src.index("# --- 1. ensure the CLI is installed")


def test_the_foreign_url_is_commented_out_not_deleted(src):
    """An install predating the stamp is indistinguishable from an inherited one, so a
    machine that legitimately owns its URL can land here. Deleting would cost it a working
    connector if this run then fails; commenting keeps the value readable."""
    sec = src[src.index("# --- 0. a URL"):src.index("# --- 1. ensure the CLI")]
    assert '"# " + $ln' in sec, "the old value is not preserved as a comment"
    assert "Remove-Item" not in sec and "Set-Content" not in sec


def test_the_guard_writes_utf8_without_a_bom(src):
    """Same .env encoding rule as everywhere else: ASCII destroys non-ASCII lines and PS 5.1's
    -Encoding UTF8 writes a BOM that folds into the first key name."""
    sec = src[src.index("# --- 0. a URL"):src.index("# --- 1. ensure the CLI")]
    assert "UTF8Encoding($false)" in sec
    assert "Get-Content $envPath1 -Encoding UTF8" in sec


def test_the_machine_comparison_is_case_insensitive(src):
    """Windows reports the machine name in either case depending on how it is read."""
    sec = src[src.index("# --- 0. a URL"):src.index("# --- 1. ensure the CLI")]
    assert ".ToLower()" in sec


def test_quickstarts_readiness_gate_rejects_the_commented_form():
    """THE TWO HALVES HAVE TO AGREE. Setting the URL aside only helps if quickstart then says
    'not ready' -- otherwise the operator is waved on to STEP 5 with no URL at all, which is a
    different bad outcome. This asserts the gate quickstart actually uses, taken from the file
    rather than retyped, against both shapes."""
    if not QS.is_file():
        pytest.skip("quickstart.bat not in this checkout")
    qs = io.open(QS, encoding="utf-8", errors="replace").read()
    m = re.search(r'findstr /b /r "([^"]+)"', qs)
    assert m, "quickstart no longer gates on findstr; this test is stale"
    # findstr /b /r "MCP_TUNNEL_URL=..*" -> anchored at line start, one or more chars after '='
    pattern = re.compile("^" + m.group(1).replace("..*", "..*").replace(".", "[\\s\\S]") + "")
    live = "MCP_TUNNEL_URL=https://mine.devtunnels.ms"
    aside = "# MCP_TUNNEL_URL=https://old.devtunnels.ms"
    assert pattern.match(live), "a real URL must still count as ready"
    assert not pattern.match(aside), "a commented URL would be read as ready"

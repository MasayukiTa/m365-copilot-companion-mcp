# -*- coding: utf-8 -*-
"""The supervisor must not abandon a working tunnel for a name nobody owns.

WHAT HAPPENED (2026-09-09). `.env`'s MCP_TUNNEL_NAME changed from the configured name to 'devtunnel'
at 07:36. The supervisor's live self-correction saw the change, stopped the WORKING host, and
switched to a tunnel that does not exist. Hosting then failed every ~35 seconds until something
rewrote the name back at 07:48. The switch WAS the outage: leaving the old host alone would have
cost nothing at all.

WHY THE CHECK GOES HERE AND NOT IN THE WRITERS. Every script in this repository that writes
MCP_TUNNEL_NAME was audited -- bootstrap.py, setup_devtunnel.ps1, heal_tunnel.ps1,
start_all.ps1, env_portability.py -- and none of them can produce that value; bootstrap.log has
no entry for that day at all. The writer is still unidentified, and the instrument that would
have named it (MCP_TRACE_TOOLCALLS) was not enabled. tools/file_ops.py refuses .env to the file
tools but states in its own comment that run_python and shell_exec are NOT covered, because they
run same-user subprocess code where `open('.env')` never reaches that check.

So a producer-side guard would have to enumerate every possible writer, including ones outside
this repository. The consumer is one place and covers all of them.

The asymmetry decides the default: refusing a genuine rename costs a log line and a retry on the
next cycle; accepting a bad one costs the tunnel.
"""
import io
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent / "supervisor.ps1"


@pytest.fixture(scope="module")
def src():
    if not SRC.is_file():
        pytest.skip("supervisor.ps1 not in this checkout")
    return io.open(SRC, encoding="utf-8", errors="replace").read()


def _switch_block(src):
    """From the first drift check to the line that actually stops the old host."""
    start = src.index("$freshTn = Get-EnvTunnelName")
    end = src.index("stopping the old host and switching", start)
    return src[start:end]


def test_the_name_is_checked_against_the_tunnels_the_account_owns(src):
    block = _switch_block(src)
    assert "$DevTunnel list" in block, "the new name is accepted without asking the CLI"
    assert "Get-BareTunnelName $matches[1]" in block, \
        "the listing is not compared against the claimed name"


def test_an_unowned_name_is_refused_and_the_working_host_is_kept(src):
    block = _switch_block(src)
    assert 'if ($ownsIt -eq $false)' in block, "an unowned name is not refused"
    assert '$freshTn = ""' in block, "refusing does not prevent the switch below"


def test_a_failed_listing_is_not_treated_as_a_refusal(src):
    """OFFLINE IS NOT EVIDENCE. If `devtunnel list` cannot run -- no network, signed out, CLI
    missing -- that says nothing about whether the name is good. Refusing on it would freeze the
    supervisor on a stale name exactly when a genuine repoint is most likely to be needed, which
    is the same fail-shut mistake in the other direction.

    So the guard is three-valued: true owns it, false does not, $null could not tell -- and only
    an explicit false refuses. `if ($ownsIt -eq $false)` and not `if (-not $ownsIt)`, because
    $null is falsy and would silently take the refusal branch.
    """
    block = _switch_block(src)
    assert "$ownsIt = $null" in block, "the could-not-tell state does not exist"
    assert "-eq $false" in block, "a null (could not tell) would fall into the refusal branch"
    assert not re.search(r"if\s*\(\s*-not\s+\$ownsIt\s*\)", block), \
        "-not treats 'could not tell' as 'does not own it'"


def test_the_check_runs_before_anything_is_stopped(src):
    """Order is the whole point: the outage was caused by stopping a healthy host first."""
    src_i = src.index("$freshTn = Get-EnvTunnelName")
    check_i = src.index("$DevTunnel list", src_i)
    stop_i = src.index("stopping the old host and switching", src_i)
    assert check_i < stop_i


def test_the_binary_is_the_one_the_supervisor_already_resolved(src):
    """$DevTunnel is resolved at startup, preferring the winget copy and falling back to the
    name on PATH. Spelling a path again here would drift from that resolution."""
    assert '$DevTunnel = "devtunnel"' in src, "the resolution this depends on is gone"
    block = _switch_block(src)
    assert "devtunnel.exe" not in block, "a second, hardcoded path to the CLI"

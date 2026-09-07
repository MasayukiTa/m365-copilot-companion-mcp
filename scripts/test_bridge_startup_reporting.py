"""The daily launcher started the chat bridge fire-and-forget: a bridge that exited at
once (most often "No agent page. Set MCP_IMPL_AGENT_URL..." on a fresh PC where that env
var is unset) printed "[3/4] bridge: starting" and moved on. Its exit went only to a
hidden window's redirected error log, never to $script:startupFailures and never to the
screen, so start_all exited 0 and quickstart printed SETUP COMPLETE over a stack whose
bridge was down. Every other launch in the same block -- the supervisor, the :8765-held
case, the UI rebuild -- already counts its failures; this one did not.

These are SOURCE assertions. The Linux CI runner cannot execute start_all.ps1 (no Edge, no
venv, no Windows), so the guard is that the fix's shape cannot silently regress. The
behaviour itself was verified by hand on Windows on 2026-09-07: the new poll of
:8765/conv after the headless launch appends to startupFailures and prints a yellow line
when /conv never answers, and start_all's exit code (the count of startupFailures) then
carries it.

Why /conv rather than the supervisor's HasExited probe: the bridge is launched as the
-Keepalive WRAPPER, which stays alive in its own loop and relaunches a dead child every
few seconds. The wrapper process therefore reports "still running" while nothing serves,
so process liveness is the wrong signal. /conv is the liveness probe the surrounding block
already keys on, and is the one the fix uses.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _start_all() -> str:
    return (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")


def _bridge_new_start_branch(source: str) -> str:
    """The block that launches a brand-new headless keepalive: from the '(headless
    keepalive)' banner to the end of the surrounding else. This is the branch that was
    fire-and-forget; the assertions below are scoped to it so a counted failure elsewhere
    in the file cannot make this test pass vacuously."""
    start = source.index('bridge: starting (headless keepalive)')
    # The UI section (step 4) begins right after this branch closes.
    end = source.index('# 4) WPF apps.', start)
    return source[start:end]


def test_bridge_new_start_launches_the_headless_keepalive():
    branch = _bridge_new_start_branch(_start_all())
    assert '"start_bridge.ps1"' in branch
    assert '"-Keepalive"' in branch


def test_bridge_new_start_rechecks_liveness_and_counts_a_dead_bridge():
    """THE DEFECT. After launching, the branch must confirm the bridge actually serves and
    record a startup failure if it does not -- the same accounting the supervisor, the
    port-held case and the UI rebuild already do."""
    branch = _bridge_new_start_branch(_start_all())

    # It probes the liveness endpoint the rest of the block uses, not process existence.
    assert 'Http-Up "http://127.0.0.1:8765/conv"' in branch, \
        "the bridge new-start branch does not re-check /conv after launching"

    # A bridge that never answers is counted, so start_all's exit code (the count of
    # startupFailures) is non-zero and quickstart cannot print SETUP COMPLETE over it.
    assert '$script:startupFailures +=' in branch, \
        "a bridge that failed to come up is not added to startupFailures"


def test_bridge_new_start_surfaces_the_reason_from_the_error_log():
    """The bridge's own exit message is the most useful line in the whole startup. The
    branch redirects it to "$bridgeLog.err"; on failure it must read that back so the
    reason reaches the operator instead of only the hidden file."""
    branch = _bridge_new_start_branch(_start_all())
    assert '"$bridgeLog.err"' in branch
    assert 'Get-Content "$bridgeLog.err"' in branch
    assert '-ForegroundColor Yellow' in branch

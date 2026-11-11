"""Pure decision core: when the supervisor itself is gone and a watchdog must relaunch it.

scripts/supervisor.ps1 is the single point of failure for every automatic recovery on this
machine: it restarts the MCP server, re-hosts the Dev Tunnel, drains the queue and resumes
interrupted fleet/review/local-loop runs. When the supervisor itself dies, ALL of that stops
and nothing brings it back until a person runs start_all by hand.

MEASURED (2026-11-11/12): the supervisor (pid 25644) ended 23:58:02 with exit code
-1073741571 (0xC00000FD, a native thread stack overflow faulting in ntdll.dll). A full scan
of all functions in supervisor.ps1 found NO function that calls itself, and the dot-sourced
helpers plus supervisor_handoff.ps1 are mutex-guarded, so it was not a PowerShell recursion
bug. A second, unrelated process (git.exe) also overflowed its stack with 0xC00000FD 26s
later, and the last heartbeat recorded free RAM 625 MB / CPU 96 pct: the signature of
stack-growth guard-page commits failing under system-wide memory/commit exhaustion. Whatever
the cause, nothing restarted the supervisor for ~37 minutes and the public URL answered 502.

THE WATCHDOG is an out-of-process backstop that runs OUTSIDE the supervisor (a per-user
Startup-folder launcher, no admin) and relaunches start_all.ps1 -CoreOnly -NoSplash when the
supervisor has not written a heartbeat for stale_threshold_s (default 180s = 3 minutes). It
must NOT disturb a healthy supervisor and must never kill any process (no devtunnel, no
interactive login, no server).

This function is PURE (no I/O, no clock, no process calls) so it can be exercised from pytest
on any OS, like decide_hang_restart / decide_liveness_strike / decide_runner_respawn. The
caller (supervisor_watchdog.ps1) owns the clock, reads the heartbeat file, and checks whether
a supervisor process is alive; it hands the facts in.

The SAFE direction here is the OPPOSITE of the hang gate: the danger to avoid is relaunching
while a healthy supervisor is running, so every ambiguous or unreadable input fails to wait.

Stdlib only. No Windows-specific calls: this runs in the Linux CI test job.
"""

from __future__ import annotations

STALE_THRESHOLD_SECONDS = 180


def decide_watchdog_action(
    heartbeat_age_s,
    supervisor_alive,
    stale_threshold_s=STALE_THRESHOLD_SECONDS,
):
    """Decide whether the watchdog must relaunch the supervisor stack.

    heartbeat_age_s: wall-clock seconds between NOW and the local_time stamped in
    .fleet/supervisor_heartbeat.json by the live supervisor each loop iteration. None means
    the heartbeat file is absent or unparseable; treated as stale ONLY with supervisor_alive
    False. supervisor_alive: is a supervisor process alive right now. None (cannot tell) is
    treated as alive (fail safe). stale_threshold_s: window in seconds (default 180);
    non-numeric / non-positive fails SAFE.

    Returns "relaunch" only when the supervisor is NOT alive AND either its heartbeat is older
    than the window or there is no heartbeat at all; otherwise "wait". A live supervisor always
    wins, and every unreadable input fails to "wait".
    """
    if supervisor_alive is None:
        return "wait"
    if supervisor_alive:
        return "wait"
    try:
        window = float(stale_threshold_s)
    except (TypeError, ValueError):
        return "wait"
    if window <= 0:
        return "wait"
    if heartbeat_age_s is None:
        return "relaunch"
    try:
        age = float(heartbeat_age_s)
    except (TypeError, ValueError):
        return "wait"
    if age < window:
        return "wait"
    return "relaunch"

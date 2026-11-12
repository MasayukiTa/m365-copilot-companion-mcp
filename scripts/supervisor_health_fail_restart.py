"""Pure decision core: restart main.py when /health FAILS or is too SLOW N ticks in a row.

WHY THIS EXISTS
---------------
The supervisor already has two liveness paths, and BOTH miss the failure that was measured
on 2026-11-11:

  * ``stale_server_check.decide_liveness_strike`` returns ``"alive"`` whenever the
    confirmation /health GET answers AT ALL, no matter how slow -- the
    ``LIVENESS_CONFIRM_SECONDS`` (60s) GET is treated as proof of life. So a server that
    keeps answering /health slowly (3s, then slower) on EVERY tick resets the miss streak
    every time and is never restarted. That is exactly the supervisor log line in the
    problem record: ``server slow (3 s) but answering -- not restarting``.
  * ``supervisor_hang_detect.decide_hang_restart`` fires only when /health goes FULLY
    SILENT for ``HANG_THRESHOLD_SECONDS`` (120s). A server that still answers -- just slowly
    and degrading -- never trips it, because ``last_ok_age`` is reset to ~0 on every answered
    tick.

MEASURED (2026-11-11): main.py (pid 24076) stopped being USEFUL on /health from ~17:22 to
~17:26 (~4 minutes) while the PROCESS stayed alive (handles 367 -> 1616, CPU time 641s,
system commit 86-94%). The supervisor saw slow-but-answering /health and declined to restart
(``slow (3 s) but answering -- not restarting``); only an owner-side manual restart at
17:26:31 recovered it. Nothing in the supervisor counted "answered, but far too slowly"
as a fault, so the degradation accumulated for four minutes untouched.

THE GOAL of this module: count consecutive checks where /health is UNHEALTHY -- it either
failed outright OR answered but took at least a response-time BOUND -- and, after N such
checks in a row, tell the caller to restart main.py BY PID even though the process is alive.
A single healthy (fast, answered) check resets the streak, so a lone slow tick on a busy box
never provokes a restart.

This function is PURE (no I/O, no clock, no process calls) so it is exercised from pytest on
any OS, exactly like ``decide_hang_restart`` / ``decide_liveness_strike``. The caller
(supervisor.ps1) owns the /health probe and the streak counter: it measures how long the
probe took, decides healthy-vs-unhealthy against the bound, keeps the running count across
ticks, and hands the facts in.

Stdlib only. No Windows-specific calls: this runs in the Linux CI test job.
"""

from __future__ import annotations

#: The response-time BOUND, in seconds. A /health answer that takes at least this long is
#: counted as an UNHEALTHY check (a degraded server), the same as a /health that did not
#: answer at all. MEASURED BASIS: on 2026-11-02 a busy box (whole fleet at ~100% CPU, under
#: 1 GB free) answered /health in 7.4s / 10.7s / 15s / 16s -- all HTTP 200, all a server that
#: was ALIVE and mid-work. 30s clears that measured 16s slow-but-alive maximum with ~2x
#: margin, so an ordinary busy tick never counts against the server; a /health that
#: repeatedly needs 30s+ is no longer "busy" but a worker pool / event loop that cannot
#: service the dedicated health path, which is the 17:22-17:26 shape. It also sits ABOVE
#: ``LIVENESS_CONFIRM_SECONDS`` is NOT required -- the caller treats a confirmation that took
#: >= this bound as unhealthy regardless of whether it eventually returned 200.
SLOW_HEALTH_BOUND_SECONDS = 30

#: How many consecutive UNHEALTHY checks (failed OR >= bound) force a restart. MEASURED
#: BASIS: the 2026-11-11 outage lasted ~4 minutes (17:22-17:26) with the process alive and
#: the supervisor declining to act every tick. The supervisor's own tick interval is 15s and
#: its transient-glitch debounce is ``FailuresBeforeAction`` = 4, so N = 4 matches the
#: established "four consecutive faults is not a glitch" bar AND, at a ~15s tick plus the
#: per-tick confirmation cost, four consecutive degraded checks span roughly the same
#: ~1-4 minute window the outage occupied -- i.e. this rule would have fired DURING
#: 17:22-17:26 instead of sitting it out. Fewer than 4 would risk restarting a server that is
#: merely having a brief slow patch on a saturated machine.
SLOW_HEALTH_FAIL_STREAK = 4


def classify_health_check(answered, seconds, bound_s=SLOW_HEALTH_BOUND_SECONDS):
    """Is ONE /health check healthy or unhealthy? Returns ``"healthy"`` or ``"unhealthy"``.

    A check is HEALTHY only when it answered AND did so in less than the bound. It is
    UNHEALTHY when it did not answer, OR it answered but took at least the bound (a degraded
    server whose /health is being starved by the busy worker pool / event loop).

    Inputs:

      * ``answered`` -- did the /health probe return this checkout's 200 answer this check.
      * ``seconds``  -- how long the probe took. Only read when ``answered`` is True. A
                        non-numeric value fails SAFE to HEALTHY (a broken timer must not be
                        able to manufacture a restart), so only a measured, numeric,
                        at-or-past-bound time counts against the server.
      * ``bound_s``  -- the slow bound (default 30s). A non-positive / unreadable bound
                        disables the slowness arm (any answer is healthy), so a broken caller
                        can never turn every answered tick into a fault.
    """
    if not answered:
        return "unhealthy"
    try:
        b = float(bound_s)
    except (TypeError, ValueError):
        return "healthy"
    if b <= 0:
        return "healthy"
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "healthy"  # answered, timer unreadable -> treat as a normal answer
    if s >= b:
        return "unhealthy"
    return "healthy"


def next_streak(prev_streak, answered, seconds, bound_s=SLOW_HEALTH_BOUND_SECONDS):
    """The consecutive-unhealthy streak AFTER this check. Pure; the caller persists it.

    A healthy check resets the streak to 0. An unhealthy check increments it. A damaged
    ``prev_streak`` (non-integer, negative) is treated as 0 so a corrupt counter can only
    ever make the gate MORE reluctant to restart, never less.
    """
    try:
        n = int(prev_streak)
    except (TypeError, ValueError):
        n = 0
    if n < 0:
        n = 0
    if classify_health_check(answered, seconds, bound_s) == "unhealthy":
        return n + 1
    return 0


def decide_health_fail_restart(
    streak,
    listening,
    own_main_py_alive,
    booting,
    fail_streak_n=SLOW_HEALTH_FAIL_STREAK,
    coordinator_running=False,
):
    """Decide whether N consecutive UNHEALTHY /health checks warrant a restart-by-PID.

    This is a THIRD, independent trigger beside the ordinary strike debounce and the
    full-silence hang gate. It fires for the one shape BOTH of those miss: a server that is
    still alive and still holds the port, but whose /health has FAILED or answered only past
    the slow bound for ``fail_streak_n`` checks in a row -- the 2026-11-11 "slow but
    answering -- not restarting" degradation.

    Every other shape is deferred to the paths that already own it:

      * a VANISHED server (nothing listening, no main.py of ours) is relaunched on the first
        miss by Get-VanishedServerVerdict -- faster than this;
      * a BOOTING server (within StartupGraceSeconds of launch) is never killed here;
      * a FOREIGN listener is not ours to PID-kill (Start-Server's identity rules own it).

    Inputs:

      * ``streak``              -- consecutive unhealthy checks so far (see ``next_streak``).
      * ``listening``           -- is anything LISTENing on the port now. A degraded server
                                   still holds the port; if nothing listens the server has
                                   vanished and the vanished path acts, not this one.
      * ``own_main_py_alive``   -- is a main.py of THIS checkout alive. A restart-by-PID only
                                   makes sense when there is a process of ours to kill.
      * ``booting``             -- within the startup grace window. True -> never restart here.
      * ``fail_streak_n``       -- N, the consecutive-unhealthy count that forces a restart
                                   (default 4). Non-numeric / non-positive fails SAFE (no
                                   restart) so a broken caller cannot manufacture a kill.
      * ``coordinator_running`` -- is a fleet coordinator of THIS checkout alive. A restart
                                   drops the gateway ~100s and would kill a live run, so while
                                   a coordinator is up this gate holds off and returns
                                   ``"wait"`` -- the coordinator-aware full-silence override in
                                   ``decide_hang_restart`` (10-minute bound) remains the
                                   escape hatch for a genuinely wedged run. A degraded (not
                                   silent) server mid-run is left for that longer bound rather
                                   than cutting a run short on slowness alone.

    Returns ``"restart"`` (the streak has reached N, the port is still held by a live main.py
    of ours, it is not booting, and no coordinator is running) or ``"wait"`` (anything else).
    The gate fails to ``"wait"`` on every ambiguous or unreadable input, so it can only ever
    ADD the one restart the other gates miss and never take one they would have kept.
    """
    try:
        n = int(streak)
    except (TypeError, ValueError):
        return "wait"
    try:
        need = int(fail_streak_n)
    except (TypeError, ValueError):
        return "wait"
    if need <= 0:
        return "wait"  # a non-positive N would restart on tick zero; refuse
    if booting:
        return "wait"
    if not listening:
        return "wait"  # vanished path owns a port with no listener
    if not own_main_py_alive:
        return "wait"  # foreign listener is not ours to PID-kill
    if coordinator_running:
        return "wait"  # do not cut a live fleet run on slowness; hang gate's 10-min bound owns it
    if n >= need:
        return "restart"
    return "wait"

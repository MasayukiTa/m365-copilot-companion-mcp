"""Pure decision core: when a /health that stopped answering means a HANG restart.

WHY THIS EXISTS
---------------
The supervisor's ordinary liveness path (``Test-ServerUp`` + ``decide_liveness_strike``
+ the ``FailuresBeforeAction`` strike debounce in supervisor.ps1) is deliberately slow to
act: a fast 5s /health probe that times out is confirmed with a longer
``LIVENESS_CONFIRM_SECONDS`` (60s) GET before even ONE strike is counted, and
``FailuresBeforeAction`` (4) strikes are required before a restart. That is correct for
SLOWNESS -- a busy box has answered /health in 7-16s while perfectly alive (measured
2026-11-02) -- but it means a server that is genuinely WEDGED (event loop dead, port still
listening, /health never answers again) is not restarted for a long time.

MEASURED (main_launch_log.jsonl, 2026-11-10 08:45:45): a wedged server was finally
restarted with reason ``health_failed_4_times`` and last_health
``"Timeout (confirmation also timed out after 60s)"``. Walking the ticks that produced it:
each strike costs up to ``fast(5s) + confirm(60s) + IntervalSeconds(15s)`` ~= 80s, and 4
strikes are needed, so detection of a hard hang takes ~5.3 min in the worst case, and
longer once the startup grace and tick alignment are included -- which is why
``health_failed`` did not fire for ~7 minutes. It was NOT a missed detection; it was the
designed latency of the slowness-tolerant path applied to a hang.

THE GOAL of this module: detect a server whose /health has not returned 200 for
``hang_threshold_s`` (default 120s = 2 minutes) and restart it by PID at once, WITHOUT
waiting out the slowness debounce -- while never acting on a server that is merely
starting up or legitimately slow but still answering.

This function is PURE (no I/O, no clock, no process calls) so it can be exercised from
pytest on any OS, exactly like ``decide_liveness_strike`` / ``decide_runner_respawn``. The
caller (supervisor.ps1) owns the clock and the /health probe: it tracks the wall-clock time
of the last 200 answer, measures the age, and hands the facts in.

Stdlib only. No Windows-specific calls: this runs in the Linux CI test job.
"""

from __future__ import annotations

#: Default hang window. A /health that has not answered 200 for this many seconds, while the
#: port is still held (a wedged loop, not a vanished process) and the process is past its
#: startup grace, is a HANG. 120s = the 2-minute ceiling the operator asked for; it clears
#: the measured 16s slow-but-alive maximum (LIVENESS_CONFIRM_SECONDS=60) with wide margin, so
#: a merely slow server can never reach it -- a 16s or even 55s answer resets last_ok_age to
#: ~0 on the tick it lands.
HANG_THRESHOLD_SECONDS = 120


def decide_hang_restart(
    last_ok_age_s,
    listening,
    own_main_py_alive,
    booting,
    hang_threshold_s=HANG_THRESHOLD_SECONDS,
):
    """Decide whether a non-answering /health is a HANG that warrants an immediate restart.

    This is a SECOND, independent trigger that sits beside the ordinary strike debounce in
    supervisor.ps1. It fires ONLY for the wedged case the slow path is too patient about: the
    process is still alive and still holds the port, but /health has been silent past the
    hang window. Every other shape is left to the existing paths:

      * a VANISHED server (nothing listening, no main.py of ours) is already relaunched on
        the first miss by Get-VanishedServerVerdict -- faster than this, so defer to it;
      * a BOOTING server (within StartupGraceSeconds of launch) must never be killed -- it
        has not had a chance to answer yet;
      * a server that just answered (last_ok_age_s below the window) is alive, slow or not.

    Inputs:

      * ``last_ok_age_s``     -- wall-clock seconds since /health last returned 200 with this
                                 checkout's body. The caller resets its clock to 0 on every
                                 successful probe, so this is the length of the current
                                 silence. ``None`` means /health has NEVER answered since the
                                 supervisor (re)started tracking -- treated as "not yet proven
                                 hung" so a fresh, still-booting or never-up server is not
                                 killed by this gate (the vanished/boot paths own that case).
      * ``listening``         -- is anything LISTENing on the port right now. A hang is a
                                 process that still holds the port; if nothing listens the
                                 server has vanished and the vanished path, not this one,
                                 acts.
      * ``own_main_py_alive`` -- is a main.py of THIS checkout alive. A restart-by-PID only
                                 makes sense when there is a process of ours to kill; if the
                                 listener is foreign, this gate must not act (Start-Server's
                                 own identity rules handle foreign owners).
      * ``booting``           -- is the process within its StartupGraceSeconds window. True
                                 means it may legitimately be alive-but-not-listening /
                                 not-answering yet; never restart it here.
      * ``hang_threshold_s``  -- the hang window in seconds (default 120). Tolerated as
                                 non-numeric / non-positive by failing SAFE (no restart) so a
                                 broken caller cannot manufacture a kill.

    Returns one of two words:

      * ``"restart"`` -- /health has been silent for at least the window, the port is still
                         held by a main.py of ours, and it is not booting: a wedged server.
                         The caller kills it by PID and relaunches at once.
      * ``"wait"``    -- anything else: still answering, never answered, vanished, foreign,
                         booting, or the window not yet elapsed. The caller does nothing here
                         and lets the ordinary paths run.

    Precedence is deliberately conservative: this gate can only ever ADD a restart for the
    one shape (wedged-and-silent) the slow path is too patient about, and it fails to
    ``"wait"`` on every ambiguous or unreadable input, so it can never kill a server the
    other gates would have kept.
    """
    # Never answered since tracking began -> not proven hung; boot/vanished paths own it.
    if last_ok_age_s is None:
        return "wait"
    try:
        age = float(last_ok_age_s)
    except (TypeError, ValueError):
        return "wait"  # unreadable clock: fail safe, do not kill
    try:
        window = float(hang_threshold_s)
    except (TypeError, ValueError):
        return "wait"
    if window <= 0:
        return "wait"  # a non-positive window would kill on every tick; refuse
    # A booting server may legitimately not answer yet.
    if booting:
        return "wait"
    # A hang holds the port with a process of ours. No listener -> vanished path acts.
    # Foreign listener -> not ours to PID-kill.
    if not listening:
        return "wait"
    if not own_main_py_alive:
        return "wait"
    # Silent long enough, still held by our live process, not booting: a wedge.
    if age >= window:
        return "restart"
    return "wait"

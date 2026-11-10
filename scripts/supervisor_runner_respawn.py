# -*- coding: utf-8 -*-
r"""Respawn the fleet runner when it is gone but its work is not finished.

WHY THIS EXISTS (design gap measured 2026-11-10)
------------------------------------------------
relay/fleet_runner.py exited at ~04:52 with an uncaught FleetRecoveryTimeout (a
hard_reset that did not return within 60s). PR 528 fixes THAT particular crash, but
any other uncaught exception in the runner has the same consequence, so the hole is
not the one crash -- it is that NOTHING brings the runner back.

After the exit the supervisor (scripts/supervisor.ps1) kept the MCP server alive
(it restarts main.py) but never restarted the runner itself. .fleet/status.json went
on reporting running=true, recovering=true, pending=33 for 18 minutes, until a NEW
goal submitted at 05:10 happened to start fleet_runner and resume the old workers.

THE EXISTING auto-resume IS NOT ENOUGH ON ITS OWN. Invoke-FleetAutoResume resumes
from a run marker (.fleet/fleet_run_active.json, or a pending interrupted snapshot)
whose recorded pid is dead. That covers a reboot that left a marker behind. It does
NOT cover the case this module is for: the runner process is simply ABSENT while the
run state still says there is unfinished work -- the marker may already have been
reaped, or the status/task state is the only thing that still says "not done". So
this is a SECOND, independent trigger: the runner process is gone AND the run state
shows unfinished work.

THE SHAPE IS THE SAME AS THE OTHER SUPERVISOR GATES. Like
scripts/supervisor_host_launch.py and scripts/supervisor_zero_conn_recovery.py this
is the PURE decision core: no I/O, no clock, no process calls. The PowerShell side
(scripts/supervisor.ps1) measures "is a runner of this checkout alive", "does the run
state show unfinished work", and the respawn history, and acts on the one word this
returns. Keeping it pure is what lets the unit test drive every branch with fabricated
inputs and no live processes.

THE BACKOFF AND THE CAP ARE A CRASH-LOOP BRAKE, NOT A GUESS
----------------------------------------------------------
A runner that crashes the instant it starts must not be respawned every tick. Two
bounds together stop a thrash:

  * MIN_RESPAWN_INTERVAL_SECONDS (>= 30s): at least this long must pass since the last
    respawn this supervisor did, so even a runner that dies immediately is retried at
    most about twice a minute, not every 15s tick.
  * MAX_RESPAWNS_PER_WINDOW (5) over RESPAWN_WINDOW_SECONDS (600s): no more than five
    respawns in any rolling ten-minute window, so a runner that keeps crashing stops
    being relaunched and the failure becomes visible (a human looks) instead of the
    supervisor burning the machine relaunching it forever.

The caller keeps the timestamps of its own recent respawns and passes them in; this
function counts how many fall inside the window and measures the gap to the newest.
That makes "exactly one respawn per backoff window" a property the test can assert
directly.
"""
from __future__ import annotations

#: The shortest time (seconds) that must elapse between two respawns this supervisor
#: performs. The GOAL requires "a backoff of at least 30 seconds"; 30 is that floor.
#: A runner that dies immediately is therefore retried at most ~twice a minute.
MIN_RESPAWN_INTERVAL_SECONDS = 30

#: The rolling window (seconds) over which respawns are counted for the cap. Ten
#: minutes, matching the GOAL's "cap of 5 restarts per 10 minutes".
RESPAWN_WINDOW_SECONDS = 600

#: The most respawns allowed inside RESPAWN_WINDOW_SECONDS. The GOAL's "cap of 5
#: restarts per 10 minutes"; once reached, a crash loop can no longer thrash and the
#: failure is left visible for a human.
MAX_RESPAWNS_PER_WINDOW = 5


def count_respawns_in_window(respawn_history, now, window=RESPAWN_WINDOW_SECONDS):
    """How many respawn timestamps fall within ``window`` seconds before ``now``.

    ``respawn_history`` is an iterable of unix timestamps (floats) of respawns this
    supervisor already did. Non-numeric or future-dated entries are ignored (a clock
    that went backwards must not be read as a future respawn that blocks forever).
    Pure: no clock, no I/O.
    """
    try:
        n = float(now)
    except (TypeError, ValueError):
        return 0
    try:
        w = float(window)
    except (TypeError, ValueError):
        w = float(RESPAWN_WINDOW_SECONDS)
    if w <= 0:
        return 0
    count = 0
    for ts in (respawn_history or []):
        try:
            t = float(ts)
        except (TypeError, ValueError):
            continue
        # Only count respawns at or before now and inside the window.
        if t <= n and (n - t) < w:
            count += 1
    return count


def seconds_since_last_respawn(respawn_history, now):
    """Seconds since the most recent respawn at or before ``now``.

    Returns ``None`` when there is no usable prior respawn (empty history, or every
    entry is non-numeric or in the future). Pure: no clock, no I/O.
    """
    try:
        n = float(now)
    except (TypeError, ValueError):
        return None
    newest = None
    for ts in (respawn_history or []):
        try:
            t = float(ts)
        except (TypeError, ValueError):
            continue
        if t > n:
            # A respawn stamped in the future (clock skew) is not evidence of how long
            # ago we last acted; ignore it rather than let it suppress a real respawn.
            continue
        if newest is None or t > newest:
            newest = t
    if newest is None:
        return None
    return n - newest


def decide_runner_respawn(runner_alive,
                          has_unfinished_work,
                          respawn_history=None,
                          now=0.0,
                          min_interval=MIN_RESPAWN_INTERVAL_SECONDS,
                          window=RESPAWN_WINDOW_SECONDS,
                          max_in_window=MAX_RESPAWNS_PER_WINDOW):
    """Whether the supervisor should respawn the fleet runner now.

    Inputs (all measured by the caller, see Invoke-FleetRunnerRespawnCheck):

      * ``runner_alive`` -- is a ``relay.fleet_runner`` process of THIS checkout alive
        right now? When true, there is nothing to respawn.
      * ``has_unfinished_work`` -- does the run state show work that is not done
        (status.json running=true with workers not all done, pending>0, recovering
        true, or queued>0)? When false, an absent runner is the normal finished/idle
        state and must be left alone.
      * ``respawn_history`` -- unix timestamps of respawns this supervisor already did,
        used for the backoff and the cap.
      * ``now`` -- current unix time (the caller's clock). <=0 is treated as "unknown",
        which fails safe to WAIT (never respawn on an unreadable clock).

    Returns one of:

      * ``"respawn"``   -- the runner is absent, there IS unfinished work, the backoff
        has elapsed, and the cap is not reached: start exactly one fresh runner.
      * ``"runner_alive"`` -- a runner is already alive; nothing to do.
      * ``"no_work"``   -- no unfinished work; an absent runner is expected.
      * ``"backoff"``   -- a respawn happened less than ``min_interval`` seconds ago.
      * ``"cap"``       -- ``max_in_window`` respawns already happened in ``window``.
      * ``"unknown_clock"`` -- ``now`` is not a usable time; refuse rather than guess.

    First refusal wins, in the order above. Pure: no I/O, no clock, no process calls.
    """
    if runner_alive:
        return "runner_alive"
    if not has_unfinished_work:
        return "no_work"
    try:
        n = float(now)
    except (TypeError, ValueError):
        return "unknown_clock"
    if n <= 0:
        return "unknown_clock"
    try:
        mi = float(min_interval)
    except (TypeError, ValueError):
        mi = float(MIN_RESPAWN_INTERVAL_SECONDS)
    # The GOAL mandates a floor of 30s; never let a smaller configured value weaken it.
    if mi < MIN_RESPAWN_INTERVAL_SECONDS:
        mi = float(MIN_RESPAWN_INTERVAL_SECONDS)
    hist = list(respawn_history or [])
    since = seconds_since_last_respawn(hist, n)
    if since is not None and since < mi:
        return "backoff"
    try:
        cap = int(max_in_window)
    except (TypeError, ValueError):
        cap = MAX_RESPAWNS_PER_WINDOW
    if cap < 1:
        cap = MAX_RESPAWNS_PER_WINDOW
    if count_respawns_in_window(hist, n, window) >= cap:
        return "cap"
    return "respawn"


# --------------------------------------------------------------------------------
# Reading "is there unfinished work" from a REAL-SHAPED .fleet/status.json
# --------------------------------------------------------------------------------
#
# THE BUG THIS CLOSES. scripts/supervisor.ps1's Test-FleetHasUnfinishedWork originally
# decided a worker was done from the properties ``done`` or ``state``. The real
# .fleet/status.json workers carry NEITHER: a worker is a string field ``status``
# (values such as done, stuck, waiting, ready, refuting, awaiting_children, cancelled)
# PLUS a boolean field ``closed``. So every worker read as "not done", and after a
# legitimate idle state (all workers done or closed) with running=true the supervisor
# would respawn an idle runner up to the cap, forever.
#
# A worker counts as FINISHED when ``closed`` is true OR ``status`` is one of
# ``done``, ``cancelled``, ``stuck`` -- a closed stuck worker is finished work for
# this purpose (nothing more will happen to it without a human). It is UNFINISHED only
# when ``closed`` is false AND ``status`` is anything else (waiting, ready, refuting,
# awaiting_children, ...). This is the pure core; supervisor.ps1 reads status.json and
# calls it, so the key names live in exactly one tested place.

#: Worker ``status`` values that mean the worker's work is over even if a tick still
#: sees it in the list. ``stuck`` is included deliberately: a stuck worker is closed
#: work for respawn purposes -- the runner cannot make it progress, so its presence is
#: not a reason to bring an absent runner back.
FINISHED_WORKER_STATUSES = frozenset({"done", "cancelled", "stuck"})


def worker_is_finished(worker):
    """True when one status.json worker counts as finished work.

    Finished == ``closed`` is true OR ``status`` is one of FINISHED_WORKER_STATUSES
    (done, cancelled, stuck). Unfinished == ``closed`` is false AND ``status`` is
    anything else. A worker that is not a mapping, or carries neither field, is treated
    as UNFINISHED (fail towards keeping work alive, never towards declaring idle on a
    shape we do not understand). Pure: no I/O.
    """
    if not isinstance(worker, dict):
        return False
    if worker.get("closed") is True:
        return True
    status = worker.get("status")
    if isinstance(status, str) and status.strip().lower() in FINISHED_WORKER_STATUSES:
        return True
    return False


def status_json_has_unfinished_work(status):
    """Whether a parsed .fleet/status.json shows work that is not done.

    ``status`` is the already-parsed dict (or None when the file is missing/unreadable).
    Mirrors scripts/supervisor.ps1's Test-FleetHasUnfinishedWork, so the two cannot
    drift, and uses the REAL worker shape (``closed`` bool + ``status`` string).

    Rules, first match wins:

      * None / not a dict  -> False (no run to bring back; an absent runner with no
        state is left alone).
      * ``recovering`` is true -> True (the 18-minute stuck state in the GOAL).
      * ``running`` is not true -> False (nothing is running).
      * running AND ``pending`` > 0 -> True.
      * running AND ``queued`` > 0 -> True.
      * running AND a worker list exists -> True iff ANY worker is unfinished
        (closed false AND status not in {done, cancelled, stuck}); all finished -> False.
      * running, no worker list -> True iff ``total`` > ``done_count``.
      * otherwise -> False.

    Pure: no I/O, no clock.
    """
    if not isinstance(status, dict):
        return False
    if status.get("recovering") is True:
        return True
    if status.get("running") is not True:
        return False
    for key in ("pending", "queued"):
        val = status.get(key)
        try:
            if val is not None and int(val) > 0:
                return True
        except (TypeError, ValueError):
            pass
    workers = status.get("workers")
    if isinstance(workers, list) and workers:
        for w in workers:
            if not worker_is_finished(w):
                return True
        # running with workers, all of them finished -> nothing unfinished.
        return False
    # Running with no worker list: fall back to the done/total counters.
    total = status.get("total")
    done_count = status.get("done_count")
    try:
        if total is not None and done_count is not None and int(total) > int(done_count):
            return True
    except (TypeError, ValueError):
        pass
    return False

# -*- coding: utf-8 -*-
"""Make the long-lived fleet coordinator pick up merged source changes safely.

WHY THIS EXISTS
---------------
``python -m relay.fleet_runner`` (the coordinator) imports ``relay/relay_fleet.py`` and the
modules under ``relay/`` ONCE, at process start, and keeps running that in-memory code for its
whole life. On 2026-11-01 a coordinator started at 11:52 kept composing every job from the
pre-#388 ``RelayWorker.__init__`` even after #388 landed on disk at 14:37, so a merged fix
never took effect and three live jobs got no judge instruction (see the root-cause probe,
``inject_probe_out/report.md`` sections A/C/D). #387 already gates a *server* restart on live
in-flight HTTP requests, but nothing covered this coordinator.

THE FIX, AT THE CAUSE
---------------------
This module is the PURE decision core. The coordinator, at start, records a fingerprint of the
``relay/`` modules it loaded (``relay.code_staleness.CodeWatch`` -- the SAME shared mechanism the
bridge and the supervisor already use; we do NOT invent a second staleness check). Each tick it
asks this gate whether it should restart. When a watched source file is newer than the running
process AND nothing is in flight (no admitted worker, no queued admission) AND the operator did
not ask to stop, the coordinator exits cleanly with a DISTINCT exit code and leaves its active-run
marker in place. The supervisor's EXISTING fleet auto-resume (``scripts/supervisor.ps1``: marker
present + pid dead -> relaunch with ``--resume``) then starts a fresh coordinator on the new code.
We reuse that mechanism and its in-flight idea; we never kill a running worker.

WHILE WORK IS IN FLIGHT IT WAITS. The gate returns ``"wait"`` and the coordinator keeps sweeping;
the next idle tick after the source changed is when it restarts. The check is cheap -- the throttle
below runs the CodeWatch stat pass at most once every ``RELOAD_CHECK_INTERVAL_S`` seconds -- and it
FAILS SAFE: any error inside the check is swallowed and read as ``"none"`` (do nothing), because a
false restart would cycle a healthy coordinator.

The decision function is pure (no clock, no I/O) so it is trivially unit-testable; the throttle and
the CodeWatch live behind ``CoordinatorReloadGate`` which the coordinator constructs once.
"""
from __future__ import annotations

from relay.code_staleness import CodeWatch

#: The DISTINCT exit code the coordinator uses when it exits to pick up merged source. Chosen as
#: 75 (BSD sysexits.h EX_TEMPFAIL, "temporary failure; the user is invited to retry") because that
#: is exactly the contract here -- not a crash, not a clean finish, but "replace me on fresh code".
#: It must not collide with the codes main() already returns: 0 (done), 2 (gate/config refusal),
#: 3 (run lock held), 5 (durable-state write failure), 130 (SIGINT). 75 is none of those.
COORDINATOR_RELOAD_EXIT_CODE = 75

#: HOW OFTEN the stale check actually runs. on_tick fires every poll_s (~1 s); re-stat'ing the
#: watched files every tick would be needless churn when a source change is a rare event, so the
#: gate throttles to at most one stat pass per this many seconds. 10 s is chosen as the smallest
#: interval that makes the per-tick cost negligible (one stat pass in ten) while keeping the
#: pick-up latency for a merged fix well under the supervisor's own resume backoff (>=300 s) and
#: far under the human timescale on which a merge-then-wait happens. A source change is seen within
#: one interval of the first idle tick after it lands.
RELOAD_CHECK_INTERVAL_S = 10.0

#: The ``relay/`` modules the coordinator loads whose change makes a running coordinator stale.
#: These are the files the root-cause report names (relay_fleet.py -- the composer that dropped the
#: judge instruction; judge_gate.py / task_kind.py -- the classification #385 uses; refuter.py and
#: tools/search_ops.py -- review and data-query code paths the composed job runs) plus the small
#: modules relay_fleet imports for job composition. Paths are repo-root-relative with forward
#: slashes, matching relay.code_staleness.*_CODE_FILES. Kept explicit (not a directory walk) so a
#: file that does NOT change the composed job cannot cycle an idle coordinator for an unrelated edit.
COORDINATOR_SOURCE_FILES = (
    "relay/relay_fleet.py",
    "relay/judge_gate.py",
    "relay/task_kind.py",
    "relay/refuter.py",
    "relay/fleet_runner.py",
    "relay/fanout.py",
    "relay/temp_home.py",
    "tools/search_ops.py",
)


def decide_coordinator_reload(source_stale, in_flight, stop_requested=False):
    """PURE decision: what should an idle-aware coordinator do this tick?

    * ``source_stale``   -- a watched source file is newer than this process start.
    * ``in_flight``      -- True iff work is in flight: an admitted worker is running OR a goal
                            is queued for admission. (The coordinator supplies the boolean; the
                            SAME notion of "running" status.json already uses.)
    * ``stop_requested`` -- the operator asked the run to stop (cockpit graceful stop). An
                            explicit stop owns the ending; we never turn it into a reload.

    Returns one of three words:

    * ``"none"``    -- nothing changed on disk: leave the coordinator running (the ordinary case,
                       and also the case this returns on any ambiguity, so it is the safe default).
    * ``"wait"``    -- source changed but work is in flight (or a stop is pending): do NOT restart,
                       keep sweeping, and never kill a running worker. The next idle tick decides.
    * ``"restart"`` -- source changed and nothing is in flight: exit cleanly so the supervisor
                       relaunches a fresh coordinator on the new code.

    Pure: no clock, no filesystem. The ORDER matters -- an unchanged source is ``"none"`` whatever
    is running, so an idle coordinator on current code is never disturbed.
    """
    if not source_stale:
        return "none"
    if stop_requested or in_flight:
        return "wait"
    return "restart"


def newest_source_mtime(watch):
    """The newest mtime (epoch seconds) among the watched files, or 0.0 when none can be stat'd.

    Reads the stat cache the CodeWatch already maintains (``_stat`` is (mtime_ns, size) per file,
    refreshed by ``changed()``), so this adds no filesystem calls of its own. Exposed so the
    coordinator can publish it in status.json next to its start time -- a stale coordinator is then
    visible in the cockpit (newest source mtime > coordinator start time). Never raises: an
    unreadable stat contributes nothing.
    """
    newest_ns = 0
    try:
        for st in watch._stat.values():
            if st and st[0] and st[0] > newest_ns:
                newest_ns = st[0]
    except Exception:
        return 0.0
    return newest_ns / 1e9


class CoordinatorReloadGate(object):
    """Throttled, fail-safe wrapper the coordinator asks once per tick.

    Constructed once at process start: it builds a ``CodeWatch`` over ``COORDINATOR_SOURCE_FILES``
    (recording each file's fingerprint and mtime/size now). ``should_restart`` is called every tick
    with the current clock and the in-flight / stop booleans; it runs the actual stat pass at most
    once per ``interval`` seconds and returns the pure decision word. Any exception anywhere in the
    check is swallowed and reported as ``"none"`` -- a check that cannot answer must never cycle a
    healthy coordinator.
    """

    def __init__(self, root, rels=COORDINATOR_SOURCE_FILES, interval=RELOAD_CHECK_INTERVAL_S,
                 started=None):
        self.root = root
        self.interval = float(interval)
        self.started = started
        self._watch = None
        self._last_check = None
        self._stale = False
        try:
            self._watch = CodeWatch(root, rels)
        except Exception:
            self._watch = None

    def newest_source_mtime(self):
        """Newest watched-source mtime (epoch seconds), 0.0 when unavailable. Never raises."""
        if self._watch is None:
            return 0.0
        return newest_source_mtime(self._watch)

    def _refresh_stale(self, now):
        """Run the CodeWatch stat pass at most once per interval; cache the staleness. Fail safe."""
        if self._watch is None:
            return False
        try:
            if self._last_check is not None and (now - self._last_check) < self.interval:
                return self._stale
            self._last_check = now
            self._stale = bool(self._watch.changed())
            return self._stale
        except Exception:
            return False

    def should_restart(self, now, in_flight, stop_requested=False):
        """The decision word for this tick: ``"none"`` / ``"wait"`` / ``"restart"``.

        ``now`` is a monotonic-enough clock (time.time() in production, a fake in tests). The stat
        pass is throttled to once per ``interval``; between passes the last known staleness is
        reused, so a tick that does not re-stat still returns ``"wait"`` while work is in flight.
        Never raises.
        """
        try:
            stale = self._refresh_stale(now)
            return decide_coordinator_reload(stale, bool(in_flight), bool(stop_requested))
        except Exception:
            return "none"

    def restart_log_line(self, newest_mtime=None):
        """One clear line explaining WHY the coordinator is exiting to reload. No side effects."""
        nm = self.newest_source_mtime() if newest_mtime is None else newest_mtime
        started = self.started
        detail = ""
        if started and nm:
            detail = " (newest source mtime %.0f > coordinator start %.0f)" % (nm, started)
        return ("[reload] a watched relay source changed since this coordinator started and "
                "nothing is in flight -- exiting cleanly with code %d so the supervisor relaunches "
                "a fresh coordinator on the merged code%s" % (COORDINATOR_RELOAD_EXIT_CODE, detail))

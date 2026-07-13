"""Phantom-fleet-run reaper (self-healing for the sidecar files a dead coordinator
leaves behind).

Incident this closes: when a fleet coordinator process (`python -m relay.fleet_runner`)
dies abnormally -- hard-killed, crashed, machine rebooted -- mid-run, three sidecar
files under `.fleet/` go stale and keep lying about the run's state:

  - fleet_run_active.json -- written once at run start (see `_write_active_marker` in
    relay/fleet_runner.py), holds the coordinator's pid. Only cleared on clean
    completion or an explicit stop.
  - status.json -- the live snapshot (see `_snapshot` in relay/fleet_runner.py),
    including `running`, `paused`, and a `workers` list with per-worker status/pill/
    color/outcome/reason/closed/phase_events.
  - history.json -- an array of per-worker archive entries the WPF cockpit itself
    creates (some archived early, while still non-terminal, by a manual user action).

If nothing relaunches the coordinator, these three files sit there forever claiming
the run is still live: the cockpit shows workers stuck "running", and Stop/Pause from
the UI write to commands.json, which nothing is left alive to read, so they're no-ops.

This is a SEPARATE, more conservative fix from `should_auto_resume()` / scripts/
supervisor.ps1's `Invoke-FleetAutoResume`, which auto-RELAUNCHES the coordinator (with
--resume) to continue an interrupted run -- but only runs once, at supervisor startup,
before the main polling loop begins. A coordinator that dies mid-session (supervisor
itself keeps running) is never noticed by that path. `reap_stale_run()` never
relaunches anything; it only FINALIZES the dead sidecars to a clean terminal
"cancelled" state so the phantom clears. It is meant to be called on every supervisor
poll cycle -- idempotent, cheap, and safe to call from a tight loop.

Design:
  - stdlib-only at module top. psutil (the default liveness check) is imported lazily
    inside `_pid_alive_psutil()`, not at module top, so a missing/broken psutil install
    never breaks importing this module, and callers that always pass an explicit
    `alive=` predicate (e.g. tests) never touch psutil at all.
  - `reap_stale_run()` never raises: the entire body is wrapped in a single outer
    try/except that returns None on any failure. It must be safe to call from a tight
    polling loop without ever taking down the caller.
  - Atomic writes: tmp file + os.replace, UTF-8, ensure_ascii=False -- same pattern as
    tools/tool_probe.py / relay/fleet_runner.py's `_write_atomic`.
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, Optional

ACTIVE_MARKER = "fleet_run_active.json"
STATUS_FILE = "status.json"
HISTORY_FILE = "history.json"

TERMINAL_STATUSES = frozenset({"done", "stuck", "maxturns", "error", "cancelled"})

CANCELLED_PILL = "停止"
CANCELLED_COLOR = "muted"
CANCELLED_REASON = "coordinator process gone; reaped"


def _pid_alive_psutil(pid: int) -> bool:
    """Default liveness predicate. Imports psutil lazily so a missing/broken psutil
    never breaks importing this module."""
    try:
        import psutil
        return bool(psutil.pid_exists(pid))
    except Exception:
        # Can't tell -- be conservative and assume alive so we never reap a run we
        # couldn't actually confirm is dead.
        return True


def _read_json(path: str):
    """Tolerant JSON read: missing/corrupt -> None. utf-8-sig tolerates a BOM,
    matching relay/fleet_runner.py's `_read_active_marker`."""
    try:
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def _write_atomic(path: str, payload) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _finalize_status(status: dict) -> int:
    """Mutate `status` in place to a finalized/cancelled state. Returns the count of
    workers actually flipped to closed by this call."""
    status["running"] = False
    status["paused"] = False

    workers = status.get("workers")
    closed_count = 0
    if isinstance(workers, list):
        for w in workers:
            if not isinstance(w, dict):
                continue
            if w.get("closed"):
                continue
            w["closed"] = True
            w["status"] = "cancelled"
            if not w.get("outcome"):
                w["outcome"] = "CANCELLED"
            w["pill"] = CANCELLED_PILL
            w["color"] = CANCELLED_COLOR
            if not w.get("reason"):
                w["reason"] = CANCELLED_REASON
            events = w.get("phase_events")
            if not isinstance(events, list):
                events = []
                w["phase_events"] = events
            already_cancelled = bool(events) and isinstance(events[-1], dict) and \
                events[-1].get("event") == "cancelled"
            if not already_cancelled:
                last_ts = None
                if events and isinstance(events[-1], dict):
                    last_ts = events[-1].get("ts")
                ts = (last_ts + 1) if isinstance(last_ts, (int, float)) else time.time()
                events.append({"ts": ts, "event": "cancelled", "label": "Stopped (reaped)"})
            closed_count += 1

    total = status.get("total")
    if not isinstance(total, int) or (isinstance(workers, list) and total != len(workers)):
        total = len(workers) if isinstance(workers, list) else total
        status["total"] = total
    status["done_count"] = total if isinstance(total, int) else status.get("done_count")

    return closed_count


def _finalize_history(entries) -> int:
    """Mutate the list in place. Returns count of entries actually terminated."""
    terminated = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get("status") in TERMINAL_STATUSES:
            continue
        e["status"] = "cancelled"
        e["closed"] = True
        if "outcome" in e:
            e["outcome"] = "CANCELLED"
        terminated += 1
    return terminated


def reap_stale_run(fleet_dir: str = ".fleet", *, alive: Optional[Callable[[int], bool]] = None,
                    stale_after_s: float = 600.0) -> Optional[dict]:
    """Best-effort, idempotent, NEVER-raising finalizer for a fleet run whose
    coordinator process has died without a supervisor restart happening.

    Never relaunches anything -- purely finalizes dead sidecar state (see module
    docstring). Returns a summary dict on an actual reap, or None if there was nothing
    to do (including on any internal error -- this function must be safe to call from a
    tight polling loop).
    """
    try:
        alive_fn = alive if alive is not None else _pid_alive_psutil

        active_path = os.path.join(fleet_dir, ACTIVE_MARKER)
        status_path = os.path.join(fleet_dir, STATUS_FILE)
        history_path = os.path.join(fleet_dir, HISTORY_FILE)

        marker = _read_json(active_path)
        pid = None
        should_finalize = False

        if isinstance(marker, dict):
            raw_pid = marker.get("pid")
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                pid = None
            if pid is not None:
                if alive_fn(pid):
                    return None  # genuinely live -- never touch a live run
                should_finalize = True
            # marker present but no usable pid -- fall through conservatively, do
            # nothing (can't confirm death).
        else:
            # No marker (or corrupt marker) -- conservative staleness fallback.
            status = _read_json(status_path)
            if not isinstance(status, dict) or status.get("running") is not True:
                return None
            updated = status.get("updated")
            if not isinstance(updated, (int, float)):
                return None
            if (time.time() - updated) < stale_after_s:
                return None
            should_finalize = True

        if not should_finalize:
            return None

        status = _read_json(status_path)
        workers_closed = 0
        if isinstance(status, dict):
            was_running = status.get("running") is True
            workers_closed = _finalize_status(status)
            if was_running or workers_closed:
                _write_atomic(status_path, status)

        history_terminated = 0
        history = _read_json(history_path)
        if isinstance(history, list) and history:
            history_terminated = _finalize_history(history)
            if history_terminated:
                _write_atomic(history_path, history)

        marker_removed = False
        if os.path.isfile(active_path):
            try:
                os.remove(active_path)
                marker_removed = True
            except Exception:
                pass

        return {
            "reaped": True,
            "pid": pid,
            "workers_closed": workers_closed,
            "history_terminated": history_terminated,
        }
    except Exception:
        return None

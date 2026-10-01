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
state so the phantom clears: `interrupted` (non-terminal,
resumable) for workers the death cut short, `cancelled` only for a pending user stop;
finished workers are untouched. It is meant to be called on every supervisor
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

#: Mirror of relay.relay_fleet.TERMINAL (pinned equal by a test). content_refused was missing
#: here, which would have made a finished refused worker look "unfinished" to the reaper.
TERMINAL_STATUSES = frozenset({"done", "stuck", "maxturns", "error", "cancelled",
                               "content_refused"})

#: NON-terminal and resumable: written only here, into the sidecars of a dead coordinator.
#: Deliberately NOT in TERMINAL_STATUSES. `cancelled` remains only for a user stop.
INTERRUPTED_STATUS = "interrupted"
INTERRUPTED_OUTCOME = "INTERRUPTED"
INTERRUPTED_PILL = "中断"
INTERRUPTED_COLOR = "warn"
INTERRUPTED_REASON = "coordinator died"
INTERRUPTED_DIR = "interrupted"     # .fleet/interrupted/<run_id>.json; never swept
COMMANDS_DIR = "commands.d"

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


def _has_unconsumed_stop(fleet_dir: str) -> bool:
    """True when a user `stop` command is still sitting unread in the command queue.

    A clean stop reaches the coordinator's normal completion path and never leaves a dead
    marker behind, so this is only the narrow case of a stop written just before the
    coordinator died. The user's intent wins: that run is `cancelled`, not `interrupted`.
    Layout (relay/fleet_runner.py): `commands.json` (legacy) and `commands.d/*.json`, either
    of which may already be renamed to `*.claim-<pid>` by a coordinator that then died."""
    try:
        candidates = [os.path.join(fleet_dir, "commands.json")]
        d = os.path.join(fleet_dir, COMMANDS_DIR)
        if os.path.isdir(d):
            candidates.extend(os.path.join(d, n) for n in os.listdir(d))
        try:
            candidates.extend(os.path.join(fleet_dir, n) for n in os.listdir(fleet_dir)
                              if n.startswith("commands.json.claim-"))
        except OSError:
            pass
        for path in candidates:
            name = os.path.basename(path)
            if not (name.endswith(".json") or ".json.claim-" in name):
                continue
            cmd = _read_json(path)
            if isinstance(cmd, dict) and cmd.get("stop") is True:
                return True
    except Exception:
        return False
    return False


def _coordinator_log_evidence(fleet_dir: str, pid) -> tuple:
    """(name, mtime) of the newest coordinator log of this pid, or (None, None). Cheap: one
    directory listing, one stat per matching file."""
    best = (None, None)
    try:
        suffix = "_p%d.log" % int(pid) if pid is not None else None
        for name in os.listdir(fleet_dir):
            if not name.startswith("coordinator_"):
                continue
            if suffix is not None and not name.endswith(suffix):
                continue
            try:
                m = os.path.getmtime(os.path.join(fleet_dir, name))
            except OSError:
                continue
            if best[1] is None or m > best[1]:
                best = (name, m)
    except Exception:
        pass
    return best


def _free_bytes(path: str):
    try:
        import shutil
        return int(shutil.disk_usage(path).free)
    except Exception:
        return None


def _is_unfinished(w) -> bool:
    """A worker the death actually cut short: not closed, not terminal, not already
    interrupted. Everything else (done-but-not-yet-closed included) is left as it is."""
    if not isinstance(w, dict) or w.get("closed"):
        return False
    st = w.get("status")
    return st not in TERMINAL_STATUSES and st != INTERRUPTED_STATUS


def _append_event(w: dict, event: str, label: str) -> None:
    events = w.get("phase_events")
    if not isinstance(events, list):
        events = []
        w["phase_events"] = events
    if events and isinstance(events[-1], dict) and events[-1].get("event") == event:
        return
    last_ts = events[-1].get("ts") if events and isinstance(events[-1], dict) else None
    ts = (last_ts + 1) if isinstance(last_ts, (int, float)) else time.time()
    events.append({"ts": ts, "event": event, "label": label})


def _finalize_status(status: dict, evidence: Optional[dict] = None, *,
                     stopped: bool = False) -> int:
    """Mutate `status` in place. Returns how many workers were actually changed.

    Only workers the death cut short (`_is_unfinished`) change. With `stopped` (a user stop
    was pending) they become `cancelled`; otherwise `interrupted`, a NON-terminal, resumable
    status. Workers that are done/terminal are never rewritten, closed or not."""
    status["running"] = False
    status["paused"] = False

    workers = status.get("workers")
    changed = 0
    reason = INTERRUPTED_REASON
    if evidence and evidence.get("pid") is not None:
        reason = "%s: pid %s is gone" % (INTERRUPTED_REASON, evidence["pid"])
        if evidence.get("last_coordinator_log_ts"):
            reason += "; last coordinator log write %s" % time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(evidence["last_coordinator_log_ts"]))
    if isinstance(workers, list):
        for w in workers:
            if not _is_unfinished(w):
                continue
            if stopped:
                w["closed"] = True
                w["status"] = "cancelled"
                if not w.get("outcome"):
                    w["outcome"] = "CANCELLED"
                w["pill"] = CANCELLED_PILL
                w["color"] = CANCELLED_COLOR
                if not w.get("reason"):
                    w["reason"] = CANCELLED_REASON
                _append_event(w, "cancelled", "Stopped (reaped)")
            else:
                w["prior_status"] = w.get("status")
                if w.get("outcome"):
                    w["prior_outcome"] = w.get("outcome")
                if w.get("reason"):
                    w["prior_reason"] = w.get("reason")
                w["status"] = INTERRUPTED_STATUS
                w["outcome"] = "INTERRUPTED"  # == INTERRUPTED_OUTCOME; literal so the outcome-closure walker sees it
                w["pill"] = INTERRUPTED_PILL
                w["color"] = INTERRUPTED_COLOR
                w["reason"] = reason
                w["resumable"] = True
                _append_event(w, "interrupted", "Interrupted (coordinator died)")
            changed += 1

    total = status.get("total")
    if not isinstance(total, int) or (isinstance(workers, list) and total != len(workers)):
        total = len(workers) if isinstance(workers, list) else total
        status["total"] = total
    if isinstance(workers, list):
        # done_count is what FINISHED, so an interrupted worker is not counted as one.
        status["done_count"] = sum(
            1 for w in workers if isinstance(w, dict)
            and w.get("status") in TERMINAL_STATUSES)
    if evidence is not None and not stopped and changed:
        status["interrupted"] = dict(evidence)
    return changed


def _finalize_history(entries, *, stopped: bool = False) -> int:
    """Mutate the list in place. Returns count of entries actually changed. Terminal and
    already-interrupted entries are left alone."""
    changed = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        st = e.get("status")
        if st in TERMINAL_STATUSES or st == INTERRUPTED_STATUS:
            continue
        if stopped:
            e["status"] = "cancelled"
            e["closed"] = True
            if "outcome" in e:
                e["outcome"] = "CANCELLED"
        else:
            e["status"] = INTERRUPTED_STATUS
            if "outcome" in e:
                e["outcome"] = "INTERRUPTED"
        changed += 1
    return changed


def _campaign_plans(fleet_dir: str, status=None, run_id: str = "", marker=None) -> list:
    """The fan-out plan(s) of THIS run, from campaigns.jsonl (header + child keys only).

    The ledger holds every campaign ever split (63 campaigns / 2.6 MB measured), so embedding
    all of them made each snapshot huge and made "every unfinished campaign" look like work of
    the interrupted run. Only families that belong to the run are kept
    (fleet_resume.campaigns_of_run: stamp, worker campaign id, parent goal, or the lineage it
    resumed), at most SNAPSHOT_MAX_CAMPAIGNS of them with SNAPSHOT_MAX_CHILDREN child keys each.
    No evidence -> no plans.
    """
    try:
        path = os.path.join(fleet_dir, "campaigns.jsonl")
        if not os.path.isfile(path):
            return []
        from relay import fleet_resume as fr
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            lines = fh.read().splitlines()
        from relay.fanout import campaigns_from_ledger
        fams = campaigns_from_ledger(lines)
        run_ids, cids, texts = fr.run_identity(run_id, (status or {}).get("workers"))
        try:
            with open(os.path.join(fleet_dir, "last_run_goals.json"), encoding="utf-8-sig") as fh:
                texts += [str(e.get("text")) for e in (json.load(fh).get("goals") or [])
                          if isinstance(e, dict) and e.get("text")]
        except Exception:
            pass
        prior = fr.lineage_campaign_ids(
            fleet_dir, (marker or {}).get("resume_lineage") if isinstance(marker, dict) else None)
        mine = fr.campaigns_of_run(fams, run_ids, cids, texts, prior)
        plans = []
        for cid in sorted(mine)[:fr.SNAPSHOT_MAX_CAMPAIGNS]:
            fam = fams[cid]
            kids = [c for c in fam.get("children", []) if isinstance(c, dict)]
            plans.append({
                "campaign_id": cid,
                "goal": str(fam.get("goal") or "")[:400],
                "n": fam.get("n"),
                "cwd": fam.get("cwd"),
                "merged": bool(fam.get("merged")),
                "run_ids": list(fam.get("run_ids") or [])[:4],
                "children": [{"task_id": c.get("task_id"),
                              "subtask_index": c.get("subtask_index")}
                             for c in kids[:fr.SNAPSHOT_MAX_CHILDREN]],
            })
        return plans
    except Exception:
        return []


def _run_id(status, marker, pid) -> str:
    """`r<hex>_a<n>` of the first worker that has one; else `p<pid>_<start_ts>`."""
    try:
        for w in (status or {}).get("workers") or []:
            rid = w.get("run_id") if isinstance(w, dict) else None
            if isinstance(rid, str) and rid and all(c.isalnum() or c in "_-" for c in rid):
                return rid
    except Exception:
        pass
    start = (marker or {}).get("start_ts") if isinstance(marker, dict) else None
    return "p%s_%d" % (pid if pid is not None else "x",
                       int(start) if isinstance(start, (int, float)) else int(time.time()))


def _snapshot_payload(status: dict, marker, evidence: dict, run_id: str,
                      fleet_dir: str) -> dict:
    workers = []
    for w in status.get("workers") or []:
        if not isinstance(w, dict):
            continue
        workers.append({k: w.get(k) for k in (
            "name", "status", "outcome", "run_id", "jid", "campaign", "role",
            "subtask_index", "turns", "closed", "goal", "campaign_id") if k in w})
    # A RUN THAT DESCENDS FROM AN EARLIER RESUME keeps that resume's count, so the loop guard
    # (fleet_resume.resume_gate) counts a crash loop across the new run ids a resume creates.
    resume = {"count": 0, "history": []}
    try:
        from relay.fleet_resume import inherit_resume
        inherited = inherit_resume(fleet_dir, (marker or {}).get("resume_lineage")
                                   if isinstance(marker, dict) else None)
        if inherited:
            resume = inherited
    except Exception:
        pass
    return {
        "schema": 1,
        "run_id": run_id,
        "resume": resume,
        "state": "pending",
        "written_ts": time.time(),
        "marker": marker if isinstance(marker, dict) else None,
        "interrupted": evidence,
        "workers": workers,
        "campaigns": _campaign_plans(fleet_dir, status, run_id, marker),
        # the list above is THIS run's families only (see _campaign_plans); resume trusts it
        # only when this flag is present, because older snapshots embedded the whole ledger.
        "campaigns_scoped": True,
    }


def read_interrupted_snapshot(fleet_dir: str = ".fleet"):
    """(data, path) of the newest `.fleet/interrupted/*.json` whose state is `pending`, or
    None. This is what the resumer reads once the reaper has consumed the live marker."""
    try:
        d = os.path.join(fleet_dir, INTERRUPTED_DIR)
        best = None
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            data = _read_json(os.path.join(d, name))
            if not isinstance(data, dict) or data.get("state") != "pending":
                continue
            ts = data.get("written_ts")
            ts = ts if isinstance(ts, (int, float)) else 0
            if best is None or ts > best[0]:
                best = (ts, data, os.path.join(d, name))
        return (best[1], best[2]) if best else None
    except Exception:
        return None


def mark_snapshot_state(path: str, state: str) -> bool:
    """Flip a snapshot's `state` (e.g. to `resumed`) atomically. Never raises."""
    try:
        data = _read_json(path)
        if not isinstance(data, dict):
            return False
        data["state"] = state
        data["state_ts"] = time.time()
        _write_atomic(path, data)
        return True
    except Exception:
        return False


def reap_stale_run(fleet_dir: str = ".fleet", *, alive: Optional[Callable[[int], bool]] = None,
                    stale_after_s: float = 600.0, exit_code: Optional[int] = None,
                    exit_evidence: Optional[list] = None) -> Optional[dict]:
    """Best-effort, idempotent, NEVER-raising finalizer for a fleet run whose
    coordinator process has died without a supervisor restart happening.

    Never relaunches anything. Workers the death cut short become `interrupted` (a
    non-terminal, resumable status), NOT `cancelled`; `cancelled` is only for a user stop
    that was still pending. Returns a summary dict on an actual reap, or None if there was
    nothing to do (including on any internal error -- safe to call from a tight loop).
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
        stopped = _has_unconsumed_stop(fleet_dir)
        unfinished = 0
        if isinstance(status, dict):
            unfinished = sum(1 for w in (status.get("workers") or []) if _is_unfinished(w))

        evidence = None
        snapshot_path = None
        if unfinished and not stopped:
            log_name, log_ts = _coordinator_log_evidence(fleet_dir, pid)
            marker_d = marker if isinstance(marker, dict) else {}
            evidence = {
                "pid": pid,
                "pid_birth": marker_d.get("pid_birth"),
                "start_ts": marker_d.get("start_ts"),
                "detected_ts": time.time(),
                "last_coordinator_log_ts": log_ts,
                "coordinator_log": log_name,
                "exit_code": exit_code,
                "exit_evidence": list(exit_evidence or []),
                "free_bytes_at_detection": _free_bytes(fleet_dir),
                "resumable": True,
            }
            # Snapshot FIRST and fail closed: if it cannot be written, nothing else is
            # touched and the next cycle tries again. The marker (the only resume input)
            # is deleted below only after this copy is safely on disk.
            run_id = _run_id(status, marker, pid)
            snapshot_path = os.path.join(fleet_dir, INTERRUPTED_DIR, run_id + ".json")
            os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
            _write_atomic(snapshot_path, _snapshot_payload(status, marker, evidence,
                                                           run_id, fleet_dir))

        workers_closed = 0
        if isinstance(status, dict):
            was_running = status.get("running") is True
            workers_closed = _finalize_status(status, evidence, stopped=stopped)
            if was_running or workers_closed:
                _write_atomic(status_path, status)

        history_terminated = 0
        history = _read_json(history_path)
        if isinstance(history, list) and history:
            history_terminated = _finalize_history(history, stopped=stopped)
            if history_terminated:
                _write_atomic(history_path, history)

        if os.path.isfile(active_path):
            try:
                os.remove(active_path)
            except Exception:
                pass

        return {
            "reaped": True,
            "pid": pid,
            "workers_closed": workers_closed,
            "history_terminated": history_terminated,
            "interrupted": bool(evidence),
            "stopped": stopped,
            "snapshot": snapshot_path,
        }
    except Exception:
        return None


def main(argv=None) -> int:                                      # pragma: no cover
    """Command line for the reap above, because there was no way to run it.

    The module was complete and covered by tests, and nothing in the repository referenced
    it: no entry point, no scheduler, no caller. A phantom run could be finalised only by a
    person who happened to know the function existed and opened a Python prompt to call it.
    That is the same shape as the browser nobody collected and the approval nobody requested
    -- the capability was built and the trigger was never attached.

    Safe to run at any time and from anywhere: reap_stale_run refuses to touch a run whose
    pid is alive, never relaunches anything, and never raises.

        python -m relay.fleet_reaper                 # report what it would find
        python -m relay.fleet_reaper --reap          # finalize a dead run's sidecars
    """
    import argparse

    ap = argparse.ArgumentParser(description="Finalize the sidecar files a dead fleet "
                                             "coordinator left behind.")
    ap.add_argument("--fleet-dir", default=".fleet", help="state directory to examine")
    ap.add_argument("--reap", action="store_true",
                    help="actually finalize (default: report only)")
    ap.add_argument("--stale-after-s", type=float, default=600.0,
                    help="with no usable marker pid, treat a status file untouched for this "
                         "long as dead")
    args = ap.parse_args(argv)

    marker = _read_json(os.path.join(args.fleet_dir, ACTIVE_MARKER))
    if isinstance(marker, dict) and marker.get("pid"):
        pid = marker["pid"]
        alive = _pid_alive_psutil(int(pid)) if str(pid).isdigit() else True
        print("%s: marker pid=%s %s" % (args.fleet_dir, pid,
                                        "ALIVE -- nothing to reap" if alive else "DEAD"))
        if alive:
            return 0
    else:
        print("%s: no active-run marker" % args.fleet_dir)

    if not args.reap:
        print("run again with --reap to finalize the stale sidecars.")
        return 0

    result = reap_stale_run(args.fleet_dir, stale_after_s=args.stale_after_s)
    if not result:
        print("nothing to reap.")
        return 0
    print("reaped pid=%s: %d worker(s) %s, %d history entr(ies) updated%s"
          % (result.get("pid"), result.get("workers_closed") or 0,
             "cancelled" if result.get("stopped") else "marked interrupted",
             result.get("history_terminated") or 0,
             ("; snapshot " + str(result.get("snapshot"))) if result.get("snapshot") else ""))
    return 0


if __name__ == "__main__":                                       # pragma: no cover
    import sys as _sys

    _sys.exit(main())

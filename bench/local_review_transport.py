"""Run review fleet goals through response-independent LOCAL_LOOP jobs.

This module is a transport adapter. It preserves review_run's existing producer,
refuter, decomposer, adjudicator and report parsers, but replaces browser-response
scraping with a final SQLite commit. The resulting status.json deliberately matches
the small subset of relay.fleet_runner's snapshot consumed by those parsers.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from relay.local_job_store import JobStoreError, LocalJobStore, TERMINAL_JOB_STATUSES


CAMPAIGN_MANIFEST = "local_review_campaign.json"
CONTROLLER_PAUSED_STATUSES = {
    "WAITING_USER", "WAITING_EXTERNAL", "NEEDS_ROUTING", "WAITING_AUTH",
    "WAITING_CONSENT", "WAITING_RUNTIME",
}


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".%s.tmp" % os.getpid())
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _read_goals(path: str | os.PathLike) -> list[dict]:
    goals = []
    with open(path, encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                goals.append(value)
    return goals


def _goals_digest(goals: list[dict]) -> str:
    payload = json.dumps(goals, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _campaign_entries(state: Path, jobs_dir: Path, store: LocalJobStore,
                      goals: list[dict]) -> tuple[list[dict], float, bool]:
    """Create a durable campaign once, or reopen the exact same SQLite jobs.

    Reusing job ids is the critical coordinator-crash/reboot property: restarting the
    parent process must never create a second set of browser conversations for work that
    is already committed in SQLite.
    """
    manifest_path = state / CAMPAIGN_MANIFEST
    digest = _goals_digest(goals)
    resumed = manifest_path.is_file()
    if resumed:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise RuntimeError(f"LOCAL_LOOP campaign manifest is unreadable: {exc}") from exc
        if manifest.get("goals_digest") != digest or int(manifest.get("total", -1)) != len(goals):
            raise RuntimeError(
                "LOCAL_LOOP state directory already belongs to a different goal set"
            )
        rows = manifest.get("entries") if isinstance(manifest.get("entries"), list) else []
        if len(rows) != len(goals):
            raise RuntimeError("LOCAL_LOOP campaign manifest entry count is invalid")
        started = float(manifest.get("started") or time.time())
    else:
        # Derive ids from state+goals and persist the manifest BEFORE creating jobs. If
        # power is lost anywhere in bootstrap, the next process recreates only missing
        # rows under the exact same ids instead of starting a duplicate campaign.
        identity = (str(state.resolve()) + "\0" + digest).encode("utf-8")
        campaign = hashlib.sha256(identity).hexdigest()[:12]
        started = time.time()
        rows = [{
            "job_id": "deep_%s_%04d" % (campaign, index),
            "worker": "w%d" % index,
            "restart_count": 0,
        } for index in range(len(goals))]
        _atomic_json(manifest_path, {
            "version": 1, "started": started, "goals_digest": digest,
            "total": len(goals), "entries": rows,
        })

    entries = []
    for index, (row, goal) in enumerate(zip(rows, goals)):
        job_id = str(row.get("job_id") or "")
        if not job_id:
            raise RuntimeError("LOCAL_LOOP campaign manifest has an empty job id")
        job = build_local_review_job(goal, job_id)
        try:
            store.get_job_status(job_id)
        except JobStoreError as exc:
            if exc.code != "JOB_NOT_FOUND":
                raise
            try:
                store.create_job(job)
            except JobStoreError as create_exc:
                # A second recovering coordinator may have won the same deterministic
                # insert. JOB_EXISTS is therefore success; every other error is real.
                if create_exc.code != "JOB_EXISTS":
                    raise
        job_file = jobs_dir / (job_id + ".json")
        _atomic_json(job_file, job)
        entries.append({
            "job_id": job_id,
            "worker": str(row.get("worker") or "w%d" % index),
            "goal": goal,
            "job_file": job_file,
            "worker_dir": jobs_dir / job_id,
            "restart_count": int(row.get("restart_count", 0)),
            "next_launch_at": 0.0,
        })
    return entries, started, resumed


def _persist_restart_counts(state: Path, entries: list[dict]) -> None:
    path = state / CAMPAIGN_MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
        counts = {entry["job_id"]: int(entry.get("restart_count", 0)) for entry in entries}
        for row in manifest.get("entries", []):
            row["restart_count"] = counts.get(str(row.get("job_id")), 0)
        _atomic_json(path, manifest)
    except Exception:
        # SQLite remains authoritative. Losing only the restart counter must not stop work.
        pass


def build_local_review_job(goal: dict, job_id: str) -> dict:
    """Translate one existing fleet goal into an operator-fixed three-turn plan."""
    original = str(goal.get("text") or "").strip()
    workspace = str(goal.get("cwd") or "").strip()
    if not original:
        raise ValueError("review goal text is required")
    if not workspace:
        raise ValueError("review goal cwd is required")

    metadata = goal.get("metadata") if isinstance(goal.get("metadata"), dict) else {}
    try:
        p2c_level = int(metadata.get("p2c_level", 1))
    except (TypeError, ValueError):
        p2c_level = 1
    if p2c_level not in (1, 2):
        p2c_level = 1

    safe_partition = (
        "\n\nP2C SAFE-PROGRESS POLICY (higher priority than embedded review wording):\n"
        "This is an authorized defensive review, but authorization does not require unsafe "
        "live side effects. Partition every requested check instead of refusing the whole job:\n"
        "1. SAFE-EXECUTE: run compile checks, pure functions, tests, and probes that are "
        "read-only or confined to temporary directories.\n"
        "2. ISOLATE: for code that writes files, launches processes, sends notifications, "
        "changes registration, or touches the network, use mocks, dry-run modes, temporary "
        "copies, or static call-path evidence. Never use live secrets or expose a live tunnel.\n"
        "3. STATIC-FALLBACK: when isolation is unavailable, inspect the exact code and report "
        "the path as statically verified but dynamically unverified. Continue the remaining "
        "safe slices.\n"
        "Do not abort the entire review merely because one slice is unsafe or a requested "
        "execution tool is unavailable. Commit the safe and static evidence with an honest "
        "scope caveat. Do not claim that a side effect was observed unless it actually ran "
        "inside the allowed isolation boundary.\n"
    )
    full_validation_partition = (
        "\n\nP2C LEVEL 2 ACTIVE-VALIDATION POLICY (higher priority than embedded wording):\n"
        "This operator explicitly selected full validation for an authorized assessment of "
        "the scoped local workspace. Prefer controlled dynamic reproduction over static "
        "reasoning: use repository tests, localhost-only services, temporary copies, test "
        "accounts, and synthetic secrets/data. Exercise realistic adversarial inputs and "
        "record the command/harness, input, and observed result. Keep effects inside the "
        "workspace or an ephemeral local test boundary. Never target third parties, use live "
        "credentials, establish persistence, or exfiltrate real data. If a required dynamic "
        "probe cannot be executed or safely contained, continue collecting other evidence but "
        "mark that slice INCONCLUSIVE. Static inspection must never be presented as successful "
        "dynamic validation or as a clean result.\n"
    )
    progress_policy = full_validation_partition if p2c_level == 2 else safe_partition
    common = (
        progress_policy +
        "\n\nThis is a read-only review. Do not edit source files. Use the local tools "
        "needed to inspect or safely exercise the scoped repository. The operator-fixed "
        "turn plan is authoritative. Before ending this turn, commit through LOCAL_LOOP; "
        "do not place the substantive result only in the browser response.\n\n"
        "ORIGINAL REVIEW TASK:\n" + original
    )
    plan = [
        {"instruction": (
            "PRODUCER PASS (1/3). Investigate the original review task thoroughly. Verify "
            "file and line evidence. Commit CONTINUE with a compact progress summary that "
            "preserves the strongest candidate findings for the next pass." + common
        )},
        {"instruction": (
            "ADVERSARIAL PASS (2/3). Challenge the producer's candidate findings, search for "
            "missed counterexamples and false positives, and re-open the exact source lines. "
            "Commit CONTINUE with the corrected compact finding set for final adjudication." + common
        )},
        {"instruction": (
            "ADJUDICATION PASS (3/3). Produce the exact final output contract requested by the "
            "original task. Commit CANDIDATE_DONE and put the COMPLETE final machine-readable "
            "answer in commit_turn.summary (including FINDINGS/verdict delimiters when the "
            "task requires them). Keep the browser receipt short. Do not omit the final output "
            "from the SQLite commit." + common
        )},
    ]
    return {
        "job_id": job_id,
        "execution_profile": "LOCAL_LOOP",
        "data_location": "LOCAL",
        "requires_local_tool": True,
        "task": {"type": "deep_review", "instruction": plan[0]["instruction"]},
        "turn_plan": plan,
        "workspace": workspace,
        "constraints": {
            "allowed_base": workspace,
            "allow_shell": True,
            "allow_network": False,
            "p2c_level": p2c_level,
            "require_active_validation": p2c_level == 2,
            "max_turns": 3,
            # Logical review passes and delivery retries are different budgets. A missed
            # commit or safely-rescoped refusal must not consume the final adjudication pass.
            "max_attempts": 5,
            "continue_on_unsafe_abort": True,
            "max_safe_rescopes": 2,
            "unsafe_abort_fallback_instruction": progress_policy.strip(),
            "max_claim_bytes": 32768,
            "max_commit_summary_bytes": 65536,
            "max_context_file_bytes": 262144,
        },
        "acceptance_checks": [],
        "review_metadata": {
            "task_id": goal.get("task_id"),
            "campaign_id": goal.get("campaign_id"),
            "role": goal.get("role"),
            "p2c_level": p2c_level,
        },
    }


def _campaign_snapshot(store: LocalJobStore, entries: list[dict], started: float,
                       active_workers: set[str] | None = None) -> dict:
    workers = []
    done = 0
    running = False
    for entry in entries:
        status = store.get_job_status(entry["job_id"])
        commit = status.get("commit") or {}
        terminal = status["status"] in TERMINAL_JOB_STATUSES
        if status["status"] == "DONE":
            done += 1
        if not terminal:
            running = True
        goal = entry["goal"]
        workers.append({
            "name": entry["worker"],
            "task_id": goal.get("task_id"),
            "goal": goal.get("text", ""),
            "status": status["status"].lower(),
            "outcome": status["status"] if terminal else None,
            "turn": status["current_seq"],
            "reason": status.get("verification_detail", ""),
            "last": commit.get("summary", ""),
            "display_result": commit.get("summary", ""),
            "transcript": "",
            "closed": terminal,
            "execution_profile": "LOCAL_LOOP",
            "artifacts": commit.get("artifacts", []),
            "phase_events": status.get("events", []),
        })
    return {
        "started": started,
        "updated": time.time(),
        "total": len(entries),
        "done_count": done,
        "running": running,
        # READY includes all pre-created jobs, not open conversations. During a live run the
        # process table is authoritative; after completion there are no active workers.
        "open_tabs": (len(active_workers) if active_workers is not None else
                      sum(1 for worker in workers if worker["status"] == "running")),
        "execution_mode": "LOCAL_LOOP",
        "response_content_reads": 0,
        "workers": workers,
    }


def _consume_console_commands(path: Path, store: LocalJobStore, entries: list[dict]) -> set[str]:
    if not path.is_file():
        return set()
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        value = {}
    try:
        path.unlink()
    except OSError:
        pass
    requested = {entry["worker"] for entry in entries} if value.get("stop") else set()
    requested.update(str(item) for item in value.get("close", []) if item)
    job_ids = {entry["worker"]: entry["job_id"] for entry in entries}
    for worker in requested:
        job_id = job_ids.get(worker)
        if not job_id:
            continue
        try:
            store.cancel_job(job_id, "operator stop from FleetCockpit")
        except JobStoreError:
            pass
    return requested


def run_local_review_fleet(
    goals_path: str,
    max_concurrent: int,
    state_dir: str,
    *,
    repo_root: str,
    python_exe: str | None = None,
) -> int:
    """Run all goals and write a fleet-compatible status snapshot.

    Browser processes are never opened in a new visible window. Each controller attaches
    to the already-headless companion Edge over CDP. SQLite is shared with the MCP server,
    while per-worker controller snapshots/logs stay below the campaign state directory.
    """
    if os.environ.get("MCP_EXECUTION_PROFILES", "0").strip() != "1":
        print("ERROR: LOCAL_LOOP deep review requires MCP_EXECUTION_PROFILES=1 and a restart")
        return 2
    goals = _read_goals(goals_path)
    if not goals:
        return 0

    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    jobs_dir = state / "local_jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    db_path = Path(os.environ.get("MCP_LOCAL_JOB_DB") or Path(repo_root) / ".jobs" / "jobs.sqlite3")
    if not db_path.is_absolute():
        db_path = Path(repo_root) / db_path
    store = LocalJobStore(db_path)
    entries, started, resumed = _campaign_entries(state, jobs_dir, store, goals)
    if resumed:
        print("LOCAL_LOOP: resuming durable campaign from %s" % (state / CAMPAIGN_MANIFEST))

    effective = max(1, min(int(max_concurrent), int(os.environ.get(
        "MCP_LOCAL_REVIEW_MAX_CONCURRENT", "2"))))
    python_exe = python_exe or sys.executable
    pending = [entry for entry in entries
               if store.get_job_status(entry["job_id"])["status"] not in
               TERMINAL_JOB_STATUSES | CONTROLLER_PAUSED_STATUSES]
    active: dict[str, tuple[subprocess.Popen, object, dict]] = {}
    status_path = state / "status.json"
    commands_path = state / "commands.json"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    restart_cap = max(0, int(os.environ.get("MCP_LOCAL_CONTROLLER_MAX_RESTARTS", "0")))
    restart_backoff_cap = max(1.0, float(os.environ.get(
        "MCP_LOCAL_CONTROLLER_RESTART_BACKOFF_MAX", "60")))

    def launch(entry: dict) -> None:
        entry["worker_dir"].mkdir(parents=True, exist_ok=True)
        log = open(entry["worker_dir"] / "run.log", "a", encoding="utf-8")
        cmd = [
            python_exe, "-m", "relay.local_loop_controller",
            "--job-file", str(entry["job_file"]),
            "--db", str(db_path),
            "--state-dir", str(entry["worker_dir"]),
            "--rotate-after-turns", os.environ.get("MCP_LOCAL_ROTATE_AFTER_TURNS", "3"),
            "--turn-timeout", os.environ.get("MCP_LOCAL_TURN_TIMEOUT", "1800"),
            "--ui-idle-timeout", os.environ.get("MCP_LOCAL_UI_IDLE_TIMEOUT", "300"),
            "--edge-mb-limit", os.environ.get("MCP_LOCAL_EDGE_MB_LIMIT", "1400"),
        ]
        proc = subprocess.Popen(
            cmd, cwd=repo_root, env=dict(os.environ), stdout=log,
            stderr=subprocess.STDOUT, creationflags=creationflags,
        )
        active[entry["worker"]] = (proc, log, entry)
        store.record_event(entry["job_id"], "CONTROLLER_LAUNCHED", {
            "pid": proc.pid, "restart_count": int(entry.get("restart_count", 0)),
        })

    while pending or active:
        now = time.time()
        while pending and len(active) < effective:
            ready_index = next((index for index, entry in enumerate(pending)
                                if float(entry.get("next_launch_at", 0)) <= now), None)
            if ready_index is None:
                break
            launch(pending.pop(ready_index))
        stopped = _consume_console_commands(commands_path, store, entries)
        for worker in stopped:
            pair = active.get(worker)
            if pair and pair[0].poll() is None:
                pair[0].terminate()
        for worker, (proc, log, entry) in list(active.items()):
            if proc.poll() is None:
                continue
            exit_code = proc.returncode
            log.close()
            active.pop(worker, None)
            status = store.get_job_status(entry["job_id"])
            store.record_event(entry["job_id"], "CONTROLLER_EXITED", {
                "exit_code": exit_code, "job_status": status["status"],
            })
            if (worker not in stopped and status["status"] not in
                    TERMINAL_JOB_STATUSES | CONTROLLER_PAUSED_STATUSES):
                entry["restart_count"] = int(entry.get("restart_count", 0)) + 1
                if restart_cap and entry["restart_count"] > restart_cap:
                    store.mark_waiting_runtime(
                        entry["job_id"],
                        "controller restart limit reached (%d)" % restart_cap,
                    )
                else:
                    try:
                        store.retry_uncommitted_turn(
                            entry["job_id"], int(status["current_seq"]),
                            "controller exited unexpectedly with code %s" % exit_code,
                        )
                    except JobStoreError:
                        pass
                    backoff = min(restart_backoff_cap, 2 ** min(entry["restart_count"], 8))
                    entry["next_launch_at"] = time.time() + backoff
                    pending.append(entry)
                    store.record_event(entry["job_id"], "CONTROLLER_RESTART_SCHEDULED", {
                        "restart_count": entry["restart_count"], "backoff_seconds": backoff,
                        "exit_code": exit_code,
                    })
                    _persist_restart_counts(state, entries)
        _atomic_json(status_path, _campaign_snapshot(
            store, entries, started, active_workers=set(active),
        ))
        if pending or active:
            time.sleep(1.0)

    snapshot = _campaign_snapshot(store, entries, started, active_workers=set())
    _atomic_json(status_path, snapshot)
    failures = [worker for worker in snapshot["workers"] if worker["outcome"] != "DONE"]
    if failures:
        print("ERROR: LOCAL_LOOP review workers incomplete: " + ", ".join(
            "%s=%s" % (worker["name"], worker["status"]) for worker in failures))
        return 2
    return 0

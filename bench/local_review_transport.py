"""Run review fleet goals through response-independent LOCAL_LOOP jobs.

This module is a transport adapter. It preserves review_run's existing producer,
refuter, decomposer, adjudicator and report parsers, but replaces browser-response
scraping with a final SQLite commit. The resulting status.json deliberately matches
the small subset of relay.fleet_runner's snapshot consumed by those parsers.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from relay.local_job_store import JobStoreError, LocalJobStore, TERMINAL_JOB_STATUSES


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


def build_local_review_job(goal: dict, job_id: str) -> dict:
    """Translate one existing fleet goal into an operator-fixed three-turn plan."""
    original = str(goal.get("text") or "").strip()
    workspace = str(goal.get("cwd") or "").strip()
    if not original:
        raise ValueError("review goal text is required")
    if not workspace:
        raise ValueError("review goal cwd is required")

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
    common = (
        safe_partition +
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
            "max_turns": 3,
            # Logical review passes and delivery retries are different budgets. A missed
            # commit or safely-rescoped refusal must not consume the final adjudication pass.
            "max_attempts": 5,
            "continue_on_unsafe_abort": True,
            "max_safe_rescopes": 2,
            "unsafe_abort_fallback_instruction": safe_partition.strip(),
            "max_claim_bytes": 32768,
            "max_commit_summary_bytes": 65536,
            "max_context_file_bytes": 262144,
        },
        "acceptance_checks": [],
        "review_metadata": {
            "task_id": goal.get("task_id"),
            "campaign_id": goal.get("campaign_id"),
            "role": goal.get("role"),
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
    campaign = uuid.uuid4().hex[:12]
    entries = []
    for index, goal in enumerate(goals):
        job_id = "deep_%s_%04d" % (campaign, index)
        job = build_local_review_job(goal, job_id)
        store.create_job(job)
        job_file = jobs_dir / (job_id + ".json")
        _atomic_json(job_file, job)
        entries.append({
            "job_id": job_id,
            "worker": "w%d" % index,
            "goal": goal,
            "job_file": job_file,
            "worker_dir": jobs_dir / job_id,
        })

    effective = max(1, min(int(max_concurrent), int(os.environ.get(
        "MCP_LOCAL_REVIEW_MAX_CONCURRENT", "2"))))
    python_exe = python_exe or sys.executable
    pending = list(entries)
    active: dict[str, tuple[subprocess.Popen, object]] = {}
    started = time.time()
    status_path = state / "status.json"
    commands_path = state / "commands.json"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

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
        active[entry["worker"]] = (proc, log)

    while pending or active:
        while pending and len(active) < effective:
            launch(pending.pop(0))
        stopped = _consume_console_commands(commands_path, store, entries)
        for worker in stopped:
            pair = active.get(worker)
            if pair and pair[0].poll() is None:
                pair[0].terminate()
        for worker, (proc, log) in list(active.items()):
            if proc.poll() is None:
                continue
            log.close()
            active.pop(worker, None)
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

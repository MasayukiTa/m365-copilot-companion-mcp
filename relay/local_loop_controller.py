"""LOCAL_LOOP coordinator that never reads Copilot assistant response content.

The browser remains an input/control surface: send a short RUN trigger, wait for
the Stop control to disappear, handle auth/consent, and rotate heavy sessions.
The only semantic result channel is :mod:`relay.local_job_store`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from relay.acceptance import Check, normalize_checks
from relay.execution_profiles import ExecutionProfile
from relay.local_job_store import (
    INTERACTION_WAIT_STATUSES,
    JobStoreError,
    LocalJobStore,
    TERMINAL_JOB_STATUSES,
)
from tools import childproc


PAUSED_STATUSES = frozenset({
    "WAITING_USER", "WAITING_EXTERNAL", "NEEDS_ROUTING", "WAITING_AUTH", "WAITING_CONSENT",
})

def _new_companion_job_id(goal: str, now: float | None = None, nonce: str | None = None) -> str:
    """Mint a safe, human-recognisable id for an ad-hoc durable companion task."""
    text = str(goal or "").strip()
    if not text:
        raise ValueError("goal must not be empty")
    now = time.time() if now is None else float(now)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime(now))
    scope = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    suffix = str(nonce or secrets.token_hex(2)).lower()
    suffix = "".join(ch for ch in suffix if ch.isalnum())[:12] or secrets.token_hex(2)
    return f"companion_{stamp}_{scope}_{suffix}"


def _job_from_goal(goal: str, *, job_id: str | None = None, cwd: str | None = None,
                   max_turns: int = 1000, read_only: bool = False) -> dict:
    """Build the smallest LOCAL_LOOP job contract from an ordinary natural-language goal.

    The task remains intentionally open-ended: each committed turn supplies the next instruction.
    A fixed ``turn_plan`` is only appropriate when a human or planner has actually authored one.
    """
    text = str(goal or "").strip()
    if not text:
        raise ValueError("goal must not be empty")
    turns = int(max_turns)
    if turns < 1:
        raise ValueError("max_turns must be >= 1")
    constraints = {
        "max_turns": turns,
        "read_only": bool(read_only),
    }
    if cwd:
        constraints["allowed_base"] = str(Path(cwd).resolve())
    return {
        "job_id": str(job_id or _new_companion_job_id(text)),
        "execution_profile": "LOCAL_LOOP",
        # LOCAL_LOOP itself depends on the local MCP commit/heartbeat tools even when the
        # business data being worked on lives in M365 rather than on disk.
        "requires_local_tool": True,
        "task": {"type": "companion_task", "instruction": text},
        "constraints": constraints,
        "acceptance_checks": [],
    }


def _execution_profiles_enabled() -> bool:
    return os.environ.get("MCP_EXECUTION_PROFILES", "0").strip().lower() in {"1", "true", "yes", "on"}


def _read_goal_file(path: str | os.PathLike) -> str:
    text = Path(path).read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("goal file is empty")
    return text


def _read_enqueue_goals_file(path: str | os.PathLike) -> list[str]:
    """Read the Cockpit's enqueue payload without teaching the UI the durable job schema."""
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, list):
        raise ValueError("enqueue goals file must contain a JSON array")
    goals = [str(v or "").strip() for v in raw]
    goals = [v for v in goals if v]
    if not goals:
        raise ValueError("enqueue goals file is empty")
    return goals


def _campaign_manifest_path(state_dir: str | os.PathLike) -> Path:
    return Path(state_dir) / LOCAL_LOOP_CAMPAIGN_MANIFEST


def _campaign_lock_path(state_dir: str | os.PathLike) -> Path:
    return Path(state_dir) / LOCAL_LOOP_LOCK_DIR / LOCAL_LOOP_CAMPAIGN_LOCK


def _active_marker_job_ids(state_dir: str | os.PathLike) -> set[str]:
    ids = set()
    marker_dir = Path(state_dir) / LOCAL_LOOP_MARKER_DIR
    try:
        for marker in marker_dir.glob("*.json"):
            try:
                ids.add(LocalJobStore._validate_job_id(marker.stem))
            except JobStoreError:
                continue
    except OSError:
        pass
    return ids


def _read_campaign_manifest(state_dir: str | os.PathLike) -> dict:
    path = _campaign_manifest_path(state_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    rows = value.get("entries")
    if not isinstance(rows, list):
        rows = []
    return {
        "version": 1,
        "started": float(value.get("started") or 0.0),
        "updated": float(value.get("updated") or 0.0),
        "entries": [dict(row) for row in rows if isinstance(row, dict)],
    }


def _archive_campaign_manifest(state_dir: str | os.PathLike, manifest: dict,
                               *, now: float | None = None) -> Path:
    """Move a completed active manifest out of the supervisor's hot path but keep it for audit."""
    now = time.time() if now is None else float(now)
    payload = dict(manifest or {})
    payload["version"] = 1
    payload["updated"] = now
    payload["closed"] = now
    active = _campaign_manifest_path(state_dir)
    _write_atomic(active, payload)
    history = Path(state_dir) / LOCAL_LOOP_CAMPAIGN_HISTORY_DIR
    history.mkdir(parents=True, exist_ok=True)
    started = int(float(payload.get("started") or now))
    closed = int(now)
    target = history / f"campaign_{started}_{closed}_{secrets.token_hex(4)}.json"
    os.replace(active, target)
    return target


def _acquire_campaign_lock(state_dir: str | os.PathLike, timeout_seconds: float = 2.0):
    """Serialize manifest read-modify-write; bounded so UI enqueue can never hang indefinitely."""
    path = _campaign_lock_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        fh = None
        try:
            fh = open(path, "a+b", buffering=0)
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"\0")
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except (OSError, IOError):
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)


def _runtime_wait_scope(status: dict) -> str:
    """Return the scope attached to the newest WAITING_RUNTIME event.

    Old databases/events predate runtime scoping. Treat those as job-local so one historical
    browser failure cannot freeze every sibling in a campaign indefinitely.
    """
    for event in reversed(status.get("events") or []):
        if str(event.get("event") or "") != "WAITING_RUNTIME":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        scope = str(payload.get("scope") or "job").strip().lower()
        return "campaign" if scope == "campaign" else "job"
    return "job"


def _campaign_has_live_work(store: LocalJobStore, manifest: dict,
                            marker_ids: set[str]) -> bool:
    for row in manifest.get("entries", []):
        try:
            job_id = LocalJobStore._validate_job_id(row.get("job_id"))
        except JobStoreError:
            continue
        try:
            status = str(store.get_job_status(job_id).get("status") or "")
        except JobStoreError as exc:
            if exc.code == "JOB_NOT_FOUND":
                # A root may live in another explicit DB; its marker is then the only ownership
                # proof. A queued manifest copy can also still materialize locally.
                if job_id in marker_ids or isinstance(row.get("job"), dict):
                    return True
            continue
        if status not in TERMINAL_JOB_STATUSES:
            return True
        # Terminal SQLite state is authoritative: a leftover marker is stale, not live work.
    return False


def _materialize_campaign_entry(store: LocalJobStore, row: dict) -> bool:
    try:
        job_id = LocalJobStore._validate_job_id(row.get("job_id"))
    except JobStoreError:
        return False
    try:
        store.get_job_status(job_id)
        return True
    except JobStoreError as exc:
        if exc.code != "JOB_NOT_FOUND":
            raise
    job = row.get("job")
    if not isinstance(job, dict):
        return False
    try:
        store.create_job(job)
    except JobStoreError as exc:
        if exc.code != "JOB_EXISTS":
            raise
    return True


def _enqueue_campaign_goals(store: LocalJobStore, state_dir: str | os.PathLike,
                            goals: list[str], *, cwd: str | None = None,
                            max_turns: int = 1000, read_only: bool = False,
                            now: float | None = None) -> dict:
    """Durably enqueue LOCAL_LOOP jobs before any controller process is required to exist."""
    now = time.time() if now is None else float(now)
    clean = [str(goal or "").strip() for goal in goals]
    clean = [goal for goal in clean if goal]
    if not clean:
        raise ValueError("campaign enqueue requires at least one goal")
    jobs = [
        _job_from_goal(goal, cwd=cwd, max_turns=max_turns, read_only=read_only)
        for goal in clean
    ]
    marker_ids = _active_marker_job_ids(state_dir)
    lock = _acquire_campaign_lock(state_dir)
    if lock is None:
        raise RuntimeError("LOCAL_LOOP campaign queue is busy")
    try:
        manifest = _read_campaign_manifest(state_dir)
        if not _campaign_has_live_work(store, manifest, marker_ids):
            if manifest.get("entries") and _campaign_manifest_path(state_dir).is_file():
                _archive_campaign_manifest(state_dir, manifest, now=now)
            manifest = {"version": 1, "started": now, "updated": now, "entries": []}
        if not manifest.get("started"):
            manifest["started"] = now
        entries = list(manifest.get("entries", []))
        known = {str(row.get("job_id")) for row in entries if row.get("job_id")}
        # A standalone controller becomes the campaign root the moment live work is added.
        # Root entries need no job copy: SQLite already owns them and their marker proves scope.
        for job_id in sorted(marker_ids):
            if job_id not in known:
                entries.append({"job_id": job_id, "joined_at": now})
                known.add(job_id)
        for job in jobs:
            job_id = job["job_id"]
            entries.append({
                "job_id": job_id,
                "job": job,
                "enqueued_at": now,
                "launch_attempts": 0,
                "retry_after": 0.0,
            })
            known.add(job_id)
        manifest.update({"version": 1, "updated": now, "entries": entries})
        _write_atomic(_campaign_manifest_path(state_dir), manifest)
    finally:
        _release_job_lock(lock)

    # The manifest is the intake source of truth. Materialise SQLite afterwards so a crash in
    # this gap is repairable by campaign drain rather than turning an accepted task into an orphan.
    for row in manifest["entries"]:
        if row.get("job_id") in {job["job_id"] for job in jobs}:
            _materialize_campaign_entry(store, row)
    if jobs:
        _project_job_snapshot(store, jobs[0]["job_id"], Path(state_dir) / "status.json", now=now)
    return {"ok": True, "job_ids": [job["job_id"] for job in jobs]}


def _campaign_commands_path(state_dir: str | os.PathLike, job_id: str) -> Path:
    safe = LocalJobStore._validate_job_id(job_id)
    return Path(state_dir) / "local_loop_commands" / (safe + ".json")


def _campaign_child_argv(args, job_id: str) -> list[str]:
    values = dict(vars(args))
    values["commands_file"] = str(_campaign_commands_path(args.state_dir, job_id))
    return _controller_resume_argv(argparse.Namespace(**values), job_id)


def _default_campaign_launcher(argv: list[str]) -> int:
    repo = Path(__file__).resolve().parent.parent
    proc = subprocess.Popen(
        [sys.executable, "-m", "relay.local_loop_controller"] + list(argv),
        cwd=str(repo), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, **childproc.tree_popen_kwargs(headless=True),
    )
    return int(proc.pid)


def _drain_campaign(store: LocalJobStore, state_dir: str | os.PathLike, args, *,
                    now: float | None = None, launcher=None) -> dict:
    """Reconcile campaign state, then launch eligible jobs without crossing a runtime pause."""
    now = time.time() if now is None else float(now)
    launcher = launcher or _default_campaign_launcher
    lock = _acquire_campaign_lock(state_dir)
    if lock is None:
        return {"ok": False, "launched": [], "closed": False, "reason": "campaign queue busy"}
    selected = []
    manifest = None
    closed = False
    runtime_blocked = False
    candidates = []
    try:
        manifest = _read_campaign_manifest(state_dir)
        entries = manifest.get("entries", [])
        changed = False
        active = False

        # PASS 1: materialise/read every durable status and clean markers that cannot represent
        # crash-recoverable work. This pass must finish before selecting ANY sibling to launch:
        # WAITING_RUNTIME usually means a shared runtime/configuration defect (for example missing
        # Copilot Studio Agent Instructions), so starting later READY siblings just repeats it.
        for row in entries:
            try:
                job_id = LocalJobStore._validate_job_id(row.get("job_id"))
            except JobStoreError:
                continue
            marker_path = _controller_marker_path(state_dir, job_id)
            has_marker = marker_path.is_file()
            materialized = _materialize_campaign_entry(store, row)
            if not materialized:
                # A campaign root may be owned by another explicit DB. Preserve a live/unknown
                # marker as scope evidence; without either source there is nothing to launch here.
                if has_marker:
                    active = True
                continue
            try:
                status_row = store.get_job_status(job_id, event_limit=50)
                status = str(status_row.get("status") or "")
            except JobStoreError:
                if has_marker:
                    active = True
                continue

            if status in TERMINAL_JOB_STATUSES:
                if has_marker:
                    try:
                        marker_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        # Failure to reap must not turn terminal work back into executable work.
                        pass
                continue

            active = True
            if status == "WAITING_RUNTIME":
                if _runtime_wait_scope(status_row) == "campaign":
                    runtime_blocked = True
                if has_marker:
                    try:
                        marker_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
                continue
            if status in INTERACTION_WAIT_STATUSES or status in PAUSED_STATUSES:
                if has_marker:
                    try:
                        marker_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
                continue
            if has_marker:
                continue
            candidates.append((row, job_id))

        # PASS 2: only a campaign with no runtime-wide pause may launch READY/RUNNING candidates.
        if not runtime_blocked:
            for row, job_id in candidates:
                try:
                    retry_after = float(row.get("retry_after") or 0.0)
                except Exception:
                    retry_after = 0.0
                if retry_after > now:
                    continue
                attempts = max(0, int(row.get("launch_attempts") or 0)) + 1
                delay = min(900.0, 30.0 * float(2 ** min(attempts - 1, 5)))
                row["launch_attempts"] = attempts
                row["retry_after"] = now + delay
                row["last_launch_at"] = now
                selected.append(job_id)
                changed = True

        if entries and not active:
            _archive_campaign_manifest(state_dir, manifest, now=now)
            closed = True
        elif changed:
            manifest["updated"] = now
            _write_atomic(_campaign_manifest_path(state_dir), manifest)
    finally:
        _release_job_lock(lock)

    launched = []
    pids = {}
    if not closed:
        for job_id in selected:
            try:
                pid = int(launcher(_campaign_child_argv(args, job_id)))
            except Exception:
                continue
            launched.append(job_id)
            pids[job_id] = pid
    return {
        "ok": True, "launched": launched, "pids": pids, "closed": closed,
        "runtime_blocked": runtime_blocked,
    }


def _campaign_job_ids(state_dir: str | os.PathLike) -> set[str]:
    rows = _read_campaign_manifest(state_dir).get("entries", [])
    ids = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            ids.add(LocalJobStore._validate_job_id(row.get("job_id")))
        except JobStoreError:
            continue
    return ids


def _project_job_snapshot(store: LocalJobStore, job_id: str, status_path: str | os.PathLike,
                          *, now: float | None = None, terminal_grace_seconds: float = 120.0) -> dict:
    """Project every LOCAL_LOOP job visible in this state dir without leaking shared-DB jobs.

    Controllers are locked per *job*, so several durable jobs may legitimately run at once and
    all of them write the same FleetCockpit status file.  Projection membership therefore comes
    from this state dir's active marker files plus LOCAL_LOOP rows already visible in the previous
    status, never from every row in a potentially shared SQLite database.  Recently terminal rows
    get a short display grace so another controller cannot erase completion before Cockpit/history
    observes it.
    """
    now = time.time() if now is None else float(now)
    terminal_grace_seconds = max(0.0, float(terminal_grace_seconds))
    status_path = Path(status_path)
    state_dir = status_path.parent
    snapshot = store.console_snapshot()

    marker_ids = _active_marker_job_ids(state_dir)

    campaign_ids = _campaign_job_ids(state_dir)
    # A completed campaign manifest may remain on disk for audit/resume. A standalone job that is
    # unrelated to it must not resurrect those old rows. Treat the manifest as live membership
    # only when this controller belongs to it or another active marker does.
    if campaign_ids and job_id not in campaign_ids and not (marker_ids & campaign_ids):
        campaign_ids = set()

    previous_ids = set()
    try:
        previous = json.loads(status_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        previous = {}
    for worker in previous.get("workers", []):
        if worker.get("execution_profile") != ExecutionProfile.LOCAL_LOOP.value:
            continue
        try:
            previous_ids.add(LocalJobStore._validate_job_id(worker.get("name")))
        except JobStoreError:
            continue

    candidate_ids = marker_ids | campaign_ids | previous_ids | {LocalJobStore._validate_job_id(job_id)}
    workers = []
    for worker in snapshot.get("workers", []):
        name = worker.get("name")
        if name not in candidate_ids:
            continue
        if (worker.get("closed") and name != job_id and name not in marker_ids
                and name not in campaign_ids):
            updated = float(worker.get("updated_at") or 0.0)
            if name not in previous_ids or now - updated > terminal_grace_seconds:
                continue
        workers.append(worker)

    snapshot["workers"] = workers
    snapshot["total"] = len(workers)
    snapshot["done_count"] = sum(1 for worker in workers if worker.get("outcome") == "DONE")
    snapshot["running"] = any(not bool(worker.get("closed")) for worker in workers)
    snapshot["open_tabs"] = sum(
        1 for worker in workers
        if worker.get("name") in marker_ids and not bool(worker.get("closed"))
    )
    snapshot["started"] = min(
        (float(worker.get("created_at") or worker.get("updated_at") or now) for worker in workers),
        default=now,
    )
    snapshot["updated"] = now
    _write_atomic(status_path, snapshot)
    return snapshot


_AUTH_URL_MARKERS = (
    "login.microsoftonline.com", "/adfs/", "/oauth2/", "/signin", "/auth/",
)
_AUTH_ACTION_SELECTOR = ", ".join((
    'button:has-text("Sign in")', 'button:has-text("サインイン")',
    'button:has-text("Continue")', 'button:has-text("続行")',
    'button:has-text("Next")', 'button:has-text("次へ")',
    'input[type="submit"][value="Sign in"]',
    'input[type="submit"][value="Continue"]',
    '[data-test-id="accountTile"]', '[role="button"][data-test-id*="account"]',
))
_CREDENTIAL_INPUT_SELECTOR = (
    'input[type="password"], input[name="passwd"], input[autocomplete="current-password"]'
)


def _visible_count(locator) -> int:
    """Count visible matches without reading page text or response content."""
    try:
        return sum(1 for index in range(locator.count()) if locator.nth(index).is_visible())
    except Exception:
        return 0


def _single_visible(locator):
    """Return the sole visible locator, otherwise None.

    A single compound selector is important here: the same DOM element can match several
    auth selectors, and counting each selector independently falsely turns one safe choice
    into several choices.
    """
    try:
        visible = [locator.nth(index) for index in range(locator.count())
                   if locator.nth(index).is_visible()]
        return visible[0] if len(visible) == 1 else None
    except Exception:
        return None


def probe_browser_interaction(driver) -> str:
    """Handle safe single-choice auth/consent UI without inspecting assistant content."""
    page = driver.page
    from relay.edge_reconnect import click_through_consent

    if click_through_consent(page):
        return "CLEAR"

    url = str(getattr(page, "url", "") or "").lower()
    if any(marker in url for marker in _AUTH_URL_MARKERS):
        # Microsoft/ADFS often presents account tile -> Continue -> Sign in as separate
        # pages. Follow a short chain only while every page has exactly one safe action.
        # Credential entry is never automated: the persistent browser profile should own
        # that state, and a visible password control is an explicit operator boundary.
        for _ in range(4):
            if _visible_count(page.locator(_CREDENTIAL_INPUT_SELECTOR)):
                return "WAITING_AUTH"
            action = _single_visible(page.locator(_AUTH_ACTION_SELECTOR))
            if action is None:
                return "WAITING_AUTH"
            try:
                action.click()
                page.wait_for_timeout(2000)
            except Exception:
                return "WAITING_AUTH"
            url = str(getattr(page, "url", "") or "").lower()
            if not any(marker in url for marker in _AUTH_URL_MARKERS):
                return "CLEAR"
        return "WAITING_AUTH"

    try:
        pending = page.locator(
            'button:has-text("Allow"), button:has-text("許可"), '
            'a:has-text("connection manager"), a:has-text("接続マネージャーを開く")'
        )
        if pending.count() and any(pending.nth(i).is_visible() for i in range(pending.count())):
            return "WAITING_CONSENT"
    except Exception:
        pass
    return "CLEAR"


def _write_atomic(path: str | os.PathLike, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Multiple LOCAL_LOOP controllers legitimately project the same status.json concurrently.
    # A shared ``status.json.tmp`` lets one controller replace/delete another controller's temp
    # file. Give each write its own same-directory temp so only the final os.replace is shared.
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        # Windows can transiently reject two concurrent replaces of the same destination with
        # WinError 5/32 even though each source temp is independent. Retry only that sharing
        # contention; the payload itself stays private to this writer until replace succeeds.
        deadline = time.monotonic() + 1.0
        while True:
            try:
                os.replace(tmp, target)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


LOCAL_LOOP_MARKER_DIR = "local_loop_active"
LOCAL_LOOP_LOCK_DIR = "local_loop_locks"
LOCAL_LOOP_CAMPAIGN_MANIFEST = "local_loop_campaign.json"
LOCAL_LOOP_CAMPAIGN_HISTORY_DIR = "local_loop_campaign_history"
LOCAL_LOOP_CAMPAIGN_LOCK = "campaign.lock"


def _controller_marker_path(state_dir: str | os.PathLike, job_id: str) -> Path:
    # Marker/lock filenames are security boundaries too. Resume-by-id reaches these helpers
    # before any browser work, so validate here rather than relying on a later SQLite lookup.
    safe_id = LocalJobStore._validate_job_id(job_id)
    return Path(state_dir) / LOCAL_LOOP_MARKER_DIR / (safe_id + ".json")


def _controller_lock_path(state_dir: str | os.PathLike, job_id: str) -> Path:
    safe_id = LocalJobStore._validate_job_id(job_id)
    return Path(state_dir) / LOCAL_LOOP_LOCK_DIR / (safe_id + ".lock")


def _read_controller_marker(state_dir: str | os.PathLike, job_id: str) -> dict | None:
    path = _controller_marker_path(state_dir, job_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_controller_marker(state_dir: str | os.PathLike, job_id: str, resume_argv: list[str],
                             *, pid: int | None = None, started: float | None = None,
                             restart_count: int | None = None, retry_after: float | None = None) -> dict:
    existing = _read_controller_marker(state_dir, job_id) or {}
    if restart_count is None:
        try:
            restart_count = int(existing.get("restart_count", 0))
        except Exception:
            restart_count = 0
    if retry_after is None:
        try:
            retry_after = float(existing.get("retry_after", 0.0))
        except Exception:
            retry_after = 0.0
    payload = {
        "version": 1,
        "job_id": str(job_id),
        "pid": int(os.getpid() if pid is None else pid),
        "started": float(time.time() if started is None else started),
        "resume_argv": [str(v) for v in resume_argv],
        "restart_count": max(0, int(restart_count)),
        "retry_after": max(0.0, float(retry_after)),
    }
    _write_atomic(_controller_marker_path(state_dir, job_id), payload)
    return payload


def _clear_controller_marker(state_dir: str | os.PathLike, job_id: str,
                             *, owner_pid: int | None = None) -> bool:
    owner = int(os.getpid() if owner_pid is None else owner_pid)
    marker = _read_controller_marker(state_dir, job_id)
    try:
        marker_pid = int((marker or {}).get("pid") or 0)
    except Exception:
        return False
    if marker_pid != owner:
        return False
    try:
        _controller_marker_path(state_dir, job_id).unlink()
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _acquire_job_lock(state_dir: str | os.PathLike, job_id: str):
    """Take a non-blocking kernel lock for one controller per durable LOCAL_LOOP job."""
    path = _controller_lock_path(state_dir, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = None
    try:
        fh = open(path, "a+b", buffering=0)
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\0")
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except (OSError, IOError):
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        return None


def _release_job_lock(fh) -> None:
    if fh is None:
        return
    try:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def _controller_resume_argv(args, job_id: str) -> list[str]:
    """Canonical crash-resume command line containing no task text or agent URL secrets."""
    out = ["--job-id", str(job_id), "--state-dir", str(Path(args.state_dir).resolve())]
    if getattr(args, "db", None):
        out += ["--db", str(Path(args.db).resolve())]
    if getattr(args, "cdp_url", None):
        out += ["--cdp-url", str(args.cdp_url)]
    if getattr(args, "commands_file", None):
        out += ["--commands-file", str(Path(args.commands_file).resolve())]
    for flag, attr in (
        ("--poll-seconds", "poll_seconds"),
        ("--turn-timeout", "turn_timeout"),
        ("--ui-idle-timeout", "ui_idle_timeout"),
        ("--rotate-after-turns", "rotate_after_turns"),
        ("--js-heap-limit-mb", "js_heap_limit_mb"),
        ("--dom-node-limit", "dom_node_limit"),
        ("--edge-mb-limit", "edge_mb_limit"),
    ):
        out += [flag, str(getattr(args, attr))]
    return out


def run_acceptance_checks(job: dict) -> tuple[bool, str]:
    checks = normalize_checks(job.get("acceptance_checks"))
    if not checks:
        return True, "No machine acceptance checks configured."
    constraints = job.get("constraints") if isinstance(job.get("constraints"), dict) else {}
    cwd = job.get("workspace") or constraints.get("allowed_base") or None
    details = []
    for spec in checks:
        check = Check(spec, cwd=cwd).start()
        result = check.poll()
        while result is None:
            time.sleep(0.2)
            result = check.poll()
        passed, detail = result
        details.append(f"{check.describe()}: {'PASS' if passed else 'FAIL'}\n{detail}")
        if not passed:
            return False, "\n\n".join(details)
    return True, "\n\n".join(details)


def collect_browser_metrics(page, edge_mb_fn=None) -> dict:
    """Collect load signals without reading assistant text or conversation DOM content."""
    metrics = {}
    session = None
    try:
        session = page.context.new_cdp_session(page)
        heap = session.send("Runtime.getHeapUsage")
        dom = session.send("Memory.getDOMCounters")
        metrics.update({
            "js_heap_mb": round(float(heap.get("usedSize", 0)) / (1024 * 1024), 2),
            "js_heap_total_mb": round(float(heap.get("totalSize", 0)) / (1024 * 1024), 2),
            "dom_nodes": int(dom.get("nodes", 0)),
            "dom_documents": int(dom.get("documents", 0)),
            "dom_listeners": int(dom.get("jsEventListeners", 0)),
        })
    except Exception as exc:
        metrics["cdp_metric_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if session is not None:
                session.detach()
        except Exception:
            pass
    if edge_mb_fn is not None:
        try:
            metrics["edge_mb"] = round(float(edge_mb_fn()), 2)
        except Exception as exc:
            metrics["edge_metric_error"] = f"{type(exc).__name__}: {exc}"
    return metrics


NO_COMMIT_REASON = "browser response finished without LOCAL_LOOP commit"


NO_COMMIT_REASON = "browser response finished without LOCAL_LOOP commit"


class LocalLoopController:
    def __init__(self, store: LocalJobStore, job_id: str, driver,
                 status_path: str | os.PathLike | None = None,
                 commands_path: str | os.PathLike | None = None,
                 poll_seconds: float = 1.0, turn_timeout_seconds: float = 1800,
                 ui_idle_timeout_seconds: float = 300,
                 rotate_after_turns: int = 5,
                 js_heap_limit_mb: float = 0,
                 dom_node_limit: int = 0,
                 edge_mb_limit: float = 0,
                 no_commit_idle_seconds: float = 8,
                 rotate_driver=None, consent_probe=None, metrics_probe=None,
                 acceptance_runner=run_acceptance_checks,
                 sleep_fn=time.sleep, monotonic_fn=time.monotonic):
        self.store = store
        self.job_id = job_id
        self.driver = driver
        self.status_path = Path(status_path) if status_path else None
        self.commands_path = Path(commands_path) if commands_path else None
        self.poll_seconds = max(0.01, float(poll_seconds))
        self.turn_timeout_seconds = max(1.0, float(turn_timeout_seconds))
        self.ui_idle_timeout_seconds = max(1.0, float(ui_idle_timeout_seconds))
        self.rotate_after_turns = max(0, int(rotate_after_turns))
        self.js_heap_limit_mb = max(0.0, float(js_heap_limit_mb))
        self.dom_node_limit = max(0, int(dom_node_limit))
        self.edge_mb_limit = max(0.0, float(edge_mb_limit))
        self.no_commit_idle_seconds = max(1.0, float(no_commit_idle_seconds))
        self.rotate_driver = rotate_driver
        self.consent_probe = consent_probe
        self.metrics_probe = metrics_probe or (lambda drv: {})
        self.acceptance_runner = acceptance_runner
        self.sleep = sleep_fn
        self.monotonic = monotonic_fn
        self.turns_in_conversation = 0
        self.worker_id = self._new_worker_id()
        self.rotation_count = 0
        self._answer_reads_at_attach = int(getattr(driver, "answer_content_reads", 0))

    @staticmethod
    def _new_worker_id() -> str:
        return f"local_{os.getpid()}_{secrets.token_hex(6)}"

    def _assert_no_answer_content_read(self):
        reads = int(getattr(self.driver, "answer_content_reads", 0))
        if reads != self._answer_reads_at_attach:
            raise RuntimeError("LOCAL_LOOP invariant violated: assistant response content was read")

    def _project(self):
        if not self.status_path:
            return
        snapshot = _project_job_snapshot(self.store, self.job_id, self.status_path)
        snapshot["local_loop_answer_content_reads"] = (
            int(getattr(self.driver, "answer_content_reads", 0)) - self._answer_reads_at_attach
        )
        _write_atomic(self.status_path, snapshot)

    #: What this drain can actually act on. Everything else belongs to somebody else.
    _HANDLED_COMMAND_KEYS = frozenset({"stop", "close"})

    def _drain_commands(self) -> bool:
        """Consume a console command. DELETE ONLY WHAT WAS FULLY CONSUMED.

        This used to read the file, unlink it in a `finally`, and then act on `stop`/`close`
        -- so an `add_goal`, a `steer`, a `set_maxtabs` sitting in the same file was destroyed
        with nothing written down, and a lost goal is indistinguishable from one never sent.
        It normally reads its own state dir, but nothing enforces that: `--state-dir` is a
        parameter, and pointed at `.fleet` this was a shredder racing the fleet's own reader.

        A command carrying keys this cannot handle is not this controller's, so it is left
        alone AND NOT ACTED ON -- taking the `stop` out of a file that clearly belongs to a
        different channel would be answering someone else's instruction. Said out loud once,
        because a refusal nobody can see is the failure this repository keeps paying for.

        A file that will not parse is also left, for the same reason and a stronger one: what
        was in it is exactly what nobody knows. The old code deleted it and then let the
        exception out of the `finally`, so the evidence went first and the report second.
        """
        if not self.commands_path or not self.commands_path.is_file():
            return False
        try:
            command = json.loads(self.commands_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            if not getattr(self, "_command_refusal_said", False):
                self._command_refusal_said = True
                print("[local-loop] leaving %s alone: it will not parse (%s). Deleting it "
                      "would destroy the only copy of whatever it holds."
                      % (self.commands_path, exc), flush=True)
            return False
        if not isinstance(command, dict):
            command = {}
        foreign = set(command) - self._HANDLED_COMMAND_KEYS
        if foreign:
            if not getattr(self, "_command_refusal_said", False):
                self._command_refusal_said = True
                print("[local-loop] leaving %s alone: it carries %s, which this drain does "
                      "not handle -- so this is not its channel, and consuming the rest of "
                      "the file would destroy commands meant for whoever does."
                      % (self.commands_path, ", ".join(sorted(foreign))), flush=True)
            return False
        try:
            self.commands_path.unlink()
        except OSError:
            pass
        stop = bool(command.get("stop")) or self.job_id in command.get("close", [])
        if stop:
            self.store.cancel_job(self.job_id, "operator stop from console")
            self._project()
        return stop

    def _probe_consent(self):
        if self.consent_probe is None:
            return None
        try:
            result = self.consent_probe(self.driver)
            if result in INTERACTION_WAIT_STATUSES:
                self.store.mark_waiting_interaction(
                    self.job_id, result, "browser interaction requires operator attention",
                )
            elif result not in (None, False, "CLEAR"):
                self.store.record_event(self.job_id, "CONSENT_HANDLED")
            return result
        except Exception as exc:
            self.store.record_event(
                self.job_id, "CONSENT_PROBE_ERROR", {"error": f"{type(exc).__name__}: {exc}"},
            )
            return None

    def _response_block_count(self) -> int | None:
        probe = getattr(self.driver, "response_block_count", None)
        if probe is None:
            return None
        try:
            return int(probe())
        except Exception:
            return None

    def _wait_for_commit(self, seq: int, retry_count_before: int = 0,
                         response_count_before: int | None = None) -> dict | None:
        deadline = self.monotonic() + self.turn_timeout_seconds
        next_consent_probe = self.monotonic()
        idle_without_commit_since = None
        while self.monotonic() < deadline:
            if self._drain_commands():
                return None
            commit = self.store.get_turn_commit(self.job_id, seq)
            if commit is not None:
                return commit
            turn_status = self.store.get_job_status(self.job_id)
            if turn_status["status"] in TERMINAL_JOB_STATUSES:
                return {"status": "ABORTED"}
            if (
                turn_status["current_seq"] == seq
                and turn_status["turn_status"] == "READY"
                and int(turn_status["retry_count"]) > int(retry_count_before)
            ):
                return {"status": "RETRYABLE_ABORT"}
            if self.monotonic() >= next_consent_probe:
                self._probe_consent()
                if self.store.get_job_status(self.job_id)["status"] in INTERACTION_WAIT_STATUSES:
                    return None
                next_consent_probe = self.monotonic() + 5.0
            response_count = self._response_block_count()
            response_arrived = (
                response_count_before is not None and response_count is not None
                and response_count > response_count_before
            )
            if response_arrived:
                try:
                    generating = bool(self.driver._is_generating())
                except Exception:
                    generating = True
                if not generating:
                    if idle_without_commit_since is None:
                        idle_without_commit_since = self.monotonic()
                    elif self.monotonic() - idle_without_commit_since >= self.no_commit_idle_seconds:
                        self.store.record_event(self.job_id, "TURN_FINISHED_WITHOUT_COMMIT", {
                            "response_blocks_before": response_count_before,
                            "response_blocks_after": response_count,
                            "idle_seconds": self.no_commit_idle_seconds,
                        }, seq)
                        return {"status": "NO_COMMIT_AFTER_RESPONSE"}
                else:
                    idle_without_commit_since = None
            self._project()
            self.sleep(self.poll_seconds)
        self.store.record_event(self.job_id, "TURN_COMMIT_TIMEOUT", {"timeout_s": self.turn_timeout_seconds}, seq)
        return None

    def _wait_ui_idle(self) -> tuple[bool, float]:
        started = self.monotonic()
        try:
            generating = bool(self.driver._is_generating())
            idle = (not generating) or bool(
                self.driver._wait_generation_idle(timeout_s=self.ui_idle_timeout_seconds)
            )
            if idle and hasattr(self.driver, "_page_alive"):
                idle = bool(self.driver._page_alive())
        except Exception:
            idle = False
        return idle, max(0.0, self.monotonic() - started)

    def _must_rotate(self, metrics: dict, ui_idle_latency_s: float) -> tuple[bool, str]:
        if self.rotate_after_turns and self.turns_in_conversation >= self.rotate_after_turns:
            return True, "turn threshold"
        if self.js_heap_limit_mb and float(metrics.get("js_heap_mb", 0)) >= self.js_heap_limit_mb:
            return True, "JS heap threshold"
        if self.dom_node_limit and int(metrics.get("dom_nodes", 0)) >= self.dom_node_limit:
            return True, "DOM node threshold"
        if self.edge_mb_limit and float(metrics.get("edge_mb", 0)) >= self.edge_mb_limit:
            return True, "Edge memory threshold"
        if ui_idle_latency_s >= self.ui_idle_timeout_seconds:
            return True, "UI idle timeout"
        return False, ""

    def _rotate(self, reason: str) -> bool:
        if self.rotate_driver is None:
            return False
        old_reads = int(getattr(self.driver, "answer_content_reads", 0))
        self._assert_no_answer_content_read()
        replacement = self.rotate_driver(self.driver, reason)
        if replacement is None:
            return False
        self.driver = replacement
        self._answer_reads_at_attach = int(getattr(replacement, "answer_content_reads", 0))
        self.turns_in_conversation = 0
        self.worker_id = self._new_worker_id()
        self.rotation_count += 1
        self.store.record_event(self.job_id, "CONVERSATION_ROTATED", {
            "reason": reason, "rotation_count": self.rotation_count,
            "prior_answer_content_reads": old_reads,
        })
        return True

    def _verify(self) -> dict:
        job = self.store.get_job(self.job_id)
        passed, detail = self.acceptance_runner(job)
        return self.store.verify_candidate(self.job_id, passed, detail)

    def run(self) -> str:
        # Attempt budget is a durable job constraint, not a process-lifetime counter. A crash
        # may resume from the marker; WAITING_RUNTIME is an explicit pause and never auto-resumes.
        sent_attempts = self.store.ui_trigger_attempt_count(self.job_id)
        while True:
            self._assert_no_answer_content_read()
            status = self.store.get_job_status(self.job_id)
            if status["status"] == "WAITING_RUNTIME":
                self._project()
                return "WAITING_RUNTIME"
            if status["status"] in INTERACTION_WAIT_STATUSES:
                interaction = self._probe_consent()
                if interaction == "CLEAR":
                    self.store.resume_interaction(self.job_id)
                    continue
                self._project()
                return status["status"]
            if status["status"] in TERMINAL_JOB_STATUSES:
                self._project()
                self.store.checkpoint()
                return status["status"]
            if status["status"] in PAUSED_STATUSES:
                self._project()
                return status["status"]
            if status["status"] == "VERIFYING":
                self._verify()
                self._project()
                continue
            if self._drain_commands():
                return "CANCELLED"

            job = self.store.get_job(self.job_id)
            constraints = job.get("constraints") if isinstance(job.get("constraints"), dict) else {}
            max_turns = int(constraints.get("max_turns", 1000))
            max_attempts = int(constraints.get("max_attempts", max_turns + 2))
            if int(status["current_seq"]) > max_turns:
                self.store.cancel_job(self.job_id, f"max_turns={max_turns} reached")
                self._project()
                return "CANCELLED"
            if sent_attempts >= max_attempts:
                self.store.cancel_job(self.job_id, f"max_attempts={max_attempts} reached")
                self._project()
                return "CANCELLED"

            seq = int(status["current_seq"])
            retry_count_before = int(status.get("retry_count", 0))
            trigger = f"RUN {self.job_id} seq={seq} worker={self.worker_id}"
            turn_plan = job.get("turn_plan") if isinstance(job.get("turn_plan"), list) else []
            if turn_plan:
                initial_seq = int(job.get("initial_seq", 1))
                turn_number = max(1, seq - initial_seq + 1)
                trigger += f" plan={turn_number}/{len(turn_plan)}"
            if bool(constraints.get("read_only")):
                trigger += " mode=read-only"

            response_count_before = self._response_block_count()
            # Commit the attempt BEFORE touching the browser. If the process dies during send,
            # the next controller still sees that this attempt consumed budget.
            self.store.record_event(self.job_id, "UI_TRIGGER_ATTEMPT", {
                "seq": seq, "worker_id": self.worker_id,
            }, seq)
            sent_attempts += 1
            try:
                self.driver.send(trigger, track_answer=False)
            except Exception as exc:
                reason = f"send failed: {type(exc).__name__}: {exc}"
                self.store.record_event(self.job_id, "UI_TRIGGER_FAILED", {
                    "seq": seq, "worker_id": self.worker_id, "reason": reason,
                }, seq)
                self.store.retry_uncommitted_turn(self.job_id, seq, reason)
                if not self._rotate("send failed"):
                    self.store.mark_waiting_runtime(
                        self.job_id, "send failed; no replacement conversation",
                    )
                    self._project()
                    return "WAITING_RUNTIME"
                continue
            self.store.record_event(self.job_id, "UI_TRIGGER_SENT", {
                "seq": seq, "worker_id": self.worker_id,
            }, seq)
            self.turns_in_conversation += 1

            commit = self._wait_for_commit(seq, retry_count_before, response_count_before)
            if commit is not None and commit.get("status") == "NO_COMMIT_AFTER_RESPONSE":
                # A correctly configured Copilot Studio agent claims/commits the SQLite turn.
                # If it instead produces a normal chat answer, its persistent LOCAL_LOOP Agent
                # Instructions are missing/stale. Do NOT try to teach the protocol in-band: real
                # E2E showed a misconfigured agent can route that control text into ordinary
                # Fleet tools, creating recursive/duplicate work. Fence the uncommitted lease and
                # fail closed after exactly one browser response.
                self.store.retry_uncommitted_turn(self.job_id, seq, NO_COMMIT_REASON)
                reason = (
                    "LOCAL_LOOP Agent Instructions are missing or stale: the browser answered "
                    "RUN without a SQLite commit. Update/publish "
                    "docs/examples/local_loop_agent_instructions.txt in the Copilot Studio "
                    "agent instructions, reconnect MCP, then resume. No retry was sent."
                )
                self.store.mark_waiting_runtime(self.job_id, reason, scope="campaign")
                self._project()
                return "WAITING_RUNTIME"
            if commit is None:
                current_status = self.store.get_job_status(self.job_id)["status"]
                if current_status == "CANCELLED":
                    return "CANCELLED"
                if current_status in PAUSED_STATUSES:
                    self._project()
                    return current_status
                self.store.retry_uncommitted_turn(
                    self.job_id, seq, "LOCAL_LOOP commit timeout",
                )
                if not self._rotate("commit timeout"):
                    self.store.mark_waiting_runtime(self.job_id, "commit timeout; no replacement conversation")
                    self._project()
                    return "WAITING_RUNTIME"
                continue

            idle, idle_latency = self._wait_ui_idle()
            metrics = dict(self.metrics_probe(self.driver) or {})
            metrics["ui_idle_latency_s"] = round(idle_latency, 3)
            metrics["answer_content_reads"] = (
                int(getattr(self.driver, "answer_content_reads", 0)) - self._answer_reads_at_attach
            )
            self.store.record_event(self.job_id, "BROWSER_METRICS", metrics, seq)
            self._assert_no_answer_content_read()

            rotated_for_idle = False
            if not idle:
                if not self._rotate("commit received but UI did not become idle"):
                    self.store.mark_waiting_runtime(
                        self.job_id, "commit received but UI did not become idle",
                    )
                    self._project()
                    return "WAITING_RUNTIME"
                rotated_for_idle = True

            if commit["status"] == "CANDIDATE_DONE":
                self._verify()

            status = self.store.get_job_status(self.job_id)
            if status["status"] in TERMINAL_JOB_STATUSES | PAUSED_STATUSES:
                self._project()
                if status["status"] in TERMINAL_JOB_STATUSES:
                    self.store.checkpoint()
                return status["status"]

            rotate, reason = self._must_rotate(metrics, idle_latency)
            if rotate and not rotated_for_idle:
                self._rotate(reason)
            self._project()


def _open_driver(context, agent_url):
    from relay.copilot_autopilot_relay import CopilotWebDriver
    from relay.edge_reconnect import click_through_consent
    from relay.relay_fleet import _open_fresh

    page = _open_fresh(context, agent_url)
    try:
        click_through_consent(page)
    except Exception:
        pass
    return CopilotWebDriver(page)


def _close_driver_page(driver) -> None:
    """Close only the page owned by this controller; never close the shared CDP browser."""
    page = getattr(driver, "page", None)
    try:
        if page is not None and not page.is_closed():
            page.close()
    except Exception:
        pass


def main(argv=None):
    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser(description="Response-content-independent M365 LOCAL_LOOP")
    ap.add_argument("--job-id", help="resume this existing LOCAL_LOOP job, or override the id for --goal")
    ap.add_argument("--job-file", help="create/resume a LOCAL_LOOP job from this JSON file")
    ap.add_argument("--goal", help="create a durable LOCAL_LOOP job from one natural-language task")
    ap.add_argument("--goal-file", help="read the natural-language task from a UTF-8 text file")
    ap.add_argument("--enqueue-goals-file", help="durably enqueue a JSON array of live LOCAL_LOOP goals")
    ap.add_argument("--drain-campaign", action="store_true", help="materialize and launch queued campaign jobs, then exit")
    ap.add_argument("--resume-runtime", action="store_true",
                    help="explicitly resume an existing WAITING_RUNTIME job after its runtime condition is repaired")
    ap.add_argument("--cwd", help="optional local workspace boundary for an ad-hoc goal job")
    ap.add_argument("--max-turns", type=int, default=1000, help="maximum durable turns for --goal")
    ap.add_argument("--read-only", action="store_true", help="mark an ad-hoc --goal job read-only")
    ap.add_argument("--db", default=os.environ.get("MCP_LOCAL_JOB_DB"))
    ap.add_argument("--cdp-url", default=os.environ.get("MCP_CDP_URL", "http://localhost:9222"))
    ap.add_argument("--agent-url", default=os.environ.get("MCP_FLEET_AGENT_URL") or
                    os.environ.get("MCP_IMPL_AGENT_URL"))
    ap.add_argument("--state-dir", default=".fleet")
    ap.add_argument(
        "--commands-file",
        help="optional controller-specific command file; defaults to <state-dir>/commands.json",
    )
    ap.add_argument("--poll-seconds", type=float, default=1.0)
    ap.add_argument("--turn-timeout", type=float, default=1800)
    ap.add_argument("--ui-idle-timeout", type=float, default=300)
    ap.add_argument("--rotate-after-turns", type=int, default=5)
    ap.add_argument("--js-heap-limit-mb", type=float, default=0)
    ap.add_argument("--dom-node-limit", type=int, default=0)
    ap.add_argument("--edge-mb-limit", type=float, default=0)
    args = ap.parse_args(argv)
    if not _execution_profiles_enabled():
        ap.error("durable LOCAL_LOOP requires MCP_EXECUTION_PROFILES=1; enable it and restart the MCP server")
    goal_sources = sum(bool(value) for value in (args.job_file, args.goal, args.goal_file))
    if goal_sources > 1:
        ap.error("use exactly one of --job-file, --goal or --goal-file")
    special_modes = int(bool(args.enqueue_goals_file)) + int(bool(args.drain_campaign))
    if special_modes > 1 or (special_modes and (goal_sources or args.job_id or args.resume_runtime)):
        ap.error("campaign enqueue/drain modes cannot be combined with a controller job")
    if args.resume_runtime and (goal_sources or not args.job_id):
        ap.error("--resume-runtime requires an existing --job-id and cannot create a new job")

    store = LocalJobStore(args.db)
    if args.enqueue_goals_file:
        goals = _read_enqueue_goals_file(args.enqueue_goals_file)
        queued = _enqueue_campaign_goals(
            store, args.state_dir, goals, cwd=args.cwd, max_turns=args.max_turns,
            read_only=args.read_only,
        )
        launched = _drain_campaign(store, args.state_dir, args) if args.agent_url else {"launched": []}
        queued["launched"] = launched.get("launched", [])
        print(json.dumps(queued, ensure_ascii=False), flush=True)
        return 0
    if args.drain_campaign:
        if not args.agent_url:
            print(json.dumps({"ok": False, "launched": [], "reason": "agent URL unavailable"}), flush=True)
            return 2
        result = _drain_campaign(store, args.state_dir, args)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result.get("ok") else 2
    if not args.agent_url:
        ap.error("--agent-url or MCP_FLEET_AGENT_URL/MCP_IMPL_AGENT_URL is required")
    job_id = args.job_id
    job = None
    goal_text = _read_goal_file(args.goal_file) if args.goal_file else args.goal
    if goal_text:
        job = _job_from_goal(
            goal_text, job_id=job_id, cwd=args.cwd, max_turns=args.max_turns,
            read_only=args.read_only,
        )
        job_id = job["job_id"]
        try:
            store.create_job(job)
        except JobStoreError as exc:
            if exc.code != "JOB_EXISTS":
                raise
    elif args.job_file:
        job = json.loads(Path(args.job_file).read_text(encoding="utf-8"))
        job_id = str(job.get("job_id") or job_id or "")
        try:
            store.create_job(job)
        except JobStoreError as exc:
            if exc.code != "JOB_EXISTS":
                raise
    if not job_id:
        ap.error("--job-id, --job-file, --goal or --goal-file is required")
    if args.resume_runtime:
        resumed = store.resume_runtime(job_id)
        if str(resumed.get("status") or "") == "WAITING_RUNTIME":
            print(f"LOCAL_LOOP {job_id}: runtime resume did not leave WAITING_RUNTIME", flush=True)
            return 2

    job_lock = _acquire_job_lock(args.state_dir, job_id)
    if job_lock is None:
        print(f"LOCAL_LOOP {job_id}: another controller already owns this job", flush=True)
        return 3
    marker_written = False
    completed_normally = False
    try:
        resume_argv = _controller_resume_argv(args, job_id)
        _write_controller_marker(args.state_dir, job_id, resume_argv)
        marker_written = True
        # Surface the durable task immediately, before CDP/browser startup. If browser setup fails,
        # the operator still sees which task exists in SQLite instead of a blank cockpit.
        _project_job_snapshot(store, job_id, Path(args.state_dir) / "status.json")

        from playwright.sync_api import sync_playwright
        from relay.edge_recover import companion_edge_mb

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(args.cdp_url, timeout=20000)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            driver = _open_driver(context, args.agent_url)

            def rotate(old, reason):
                old_page = getattr(old, "page", None)
                replacement = _open_driver(context, args.agent_url)
                try:
                    if old_page is not None and not old_page.is_closed():
                        old_page.close()
                except Exception:
                    pass
                return replacement

            controller = LocalLoopController(
                store, job_id, driver,
                status_path=Path(args.state_dir) / "status.json",
                commands_path=(Path(args.commands_file) if args.commands_file else
                               Path(args.state_dir) / "commands.json"),
                poll_seconds=args.poll_seconds,
                turn_timeout_seconds=args.turn_timeout,
                ui_idle_timeout_seconds=args.ui_idle_timeout,
                rotate_after_turns=args.rotate_after_turns,
                js_heap_limit_mb=args.js_heap_limit_mb,
                dom_node_limit=args.dom_node_limit,
                edge_mb_limit=args.edge_mb_limit,
                rotate_driver=rotate,
                consent_probe=probe_browser_interaction,
                metrics_probe=lambda drv: collect_browser_metrics(drv.page, companion_edge_mb),
            )
            try:
                result = controller.run()
                completed_normally = True
                print(f"LOCAL_LOOP {job_id}: {result}")
                # Record the run per theme. Everything needed is already in the job spec, so
                # this costs one write and gives the next job on the same theme its history.
                # Frame-side and best-effort -- and deliberately AFTER the result is printed,
                # so a memory failure can never change what the CLI reports or returns.
                try:
                    from relay.project_memory import record_task, theme_from_goal
                    task = (job or {}).get("task") or {}
                    instruction = task.get("instruction") or task.get("type") or job_id
                    record_task(theme_from_goal(instruction), instruction, result,
                                note="job=%s base=%s" % (
                                    job_id,
                                    ((job or {}).get("constraints") or {}).get("allowed_base", "")))
                except Exception:
                    pass
                return 0 if result == "DONE" else 2
            finally:
                # connect_over_cdp disconnect does not close pages created in the persistent
                # companion Edge. Leaving one page per completed job steadily recreates the
                # memory problem LOCAL_LOOP is meant to solve.
                _close_driver_page(controller.driver)
    finally:
        # Every ordinary return (terminal, human/consent/routing wait, or WAITING_RUNTIME) clears
        # the crash-recovery marker. An unexpected exception leaves the marker behind while
        # releasing the kernel lock, so the supervisor can auto-resume only true crashes.
        # WAITING_RUNTIME resumes only through explicit --resume-runtime.
        if marker_written and completed_normally:
            _clear_controller_marker(args.state_dir, job_id, owner_pid=os.getpid())
        _release_job_lock(job_lock)


if __name__ == "__main__":
    raise SystemExit(main())

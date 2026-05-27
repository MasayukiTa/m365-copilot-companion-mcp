import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from .file_ops import _validate_path
from .jobs import _JOBS, run_python_in_background

WATCHER_TMP = Path(tempfile.gettempdir()) / "m365-copilot-companion-mcp-watchers"
WATCHER_TMP.mkdir(parents=True, exist_ok=True)


def _watcher_script(folder: str, log_path: str, events: list[str], recursive: bool) -> str:
    # Emits one JSON line per event; safe single-quoted f-string interpolation only on literals.
    events_repr = repr(events)
    return f"""
import json, time, sys, os
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

FOLDER = r{folder!r}
LOG = Path(r{log_path!r})
RECURSIVE = {bool(recursive)}
EVENTS = set({events_repr})

class H(FileSystemEventHandler):
    def _emit(self, kind, ev):
        if kind not in EVENTS:
            return
        rec = {{
            'time': time.time(),
            'kind': kind,
            'is_dir': ev.is_directory,
            'src': ev.src_path,
        }}
        if hasattr(ev, 'dest_path') and ev.dest_path:
            rec['dest'] = ev.dest_path
        with LOG.open('a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\\n')
            f.flush()
    def on_created(self, e):  self._emit('created', e)
    def on_modified(self, e): self._emit('modified', e)
    def on_deleted(self, e):  self._emit('deleted', e)
    def on_moved(self, e):    self._emit('moved', e)

print(f'[watcher] watching {{FOLDER}} -> {{LOG}}')
obs = Observer()
obs.schedule(H(), FOLDER, recursive=RECURSIVE)
obs.start()
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    obs.stop()
obs.join()
"""


def watcher_start(
    folder: str,
    events: Optional[list[str]] = None,
    recursive: bool = True,
    label: str = "",
) -> str:
    """Start watching a folder for file system events. Returns a job_id.

    Events are appended as JSON lines to a per-watcher log file. Use
    watcher_events(job_id) to read them, watcher_stop(job_id) to stop.

    Args:
        folder: Directory to watch (must be under the allowed base).
        events: Event kinds to record. Subset of:
            ["created", "modified", "deleted", "moved"]. Defaults to all.
        recursive: Watch subdirectories as well.
        label: Human label.
    """
    try:
        p = _validate_path(folder)
        if not p.is_dir():
            return f"[watcher_start error: not a directory: {p}]"
        kinds = events or ["created", "modified", "deleted", "moved"]
        log_path = WATCHER_TMP / f"watch-{uuid.uuid4().hex[:10]}.jsonl"
        script = _watcher_script(str(p), str(log_path), kinds, recursive)
        out = run_python_in_background(script, label=label or f"watch {p.name}")
        # The id appears on the first line of run_python_in_background output.
        first = out.splitlines()[0]
        if first.startswith("job_id: "):
            job_id = first[len("job_id: "):].strip()
            # Stash log path on the job for later retrieval.
            job = _JOBS.get(job_id)
            if job is not None:
                job.label = f"watcher: {p.name} -> {log_path.name}"
                # Reuse stderr_path slot is messy; instead we stash on the job dict.
                setattr(job, "watch_log", str(log_path))
            return f"{out}\nlog: {log_path}"
        return out
    except Exception as e:
        return f"[watcher_start error: {type(e).__name__}: {e}]"


def watcher_events(job_id: str, tail: int = 50) -> str:
    """Return the last N file system events captured by a watcher job.

    Args:
        job_id: The id returned by watcher_start (also a normal job_id).
        tail: How many recent events to show.
    """
    try:
        job = _JOBS.get(job_id)
        if job is None:
            return f"[watcher_events error: unknown job_id {job_id!r}]"
        log_path = getattr(job, "watch_log", None)
        if not log_path or not Path(log_path).exists():
            return "(no events yet — log file not created)"
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if not lines:
            return "(no events yet)"
        lines = lines[-tail:]
        out: list[str] = []
        import time

        for raw in lines:
            try:
                ev = json.loads(raw)
            except Exception:
                out.append(raw.rstrip())
                continue
            ts = ev.get("time", 0)
            age = time.time() - ts
            human_age = f"{int(age):>4}s ago" if age < 3600 else f"{age / 3600:.1f}h ago"
            kind = ev.get("kind", "?")
            src = ev.get("src", "?")
            dest = ev.get("dest")
            if dest:
                out.append(f"{human_age}  {kind:<8}  {src} -> {dest}")
            else:
                out.append(f"{human_age}  {kind:<8}  {src}")
        return "\n".join(out)
    except Exception as e:
        return f"[watcher_events error: {type(e).__name__}: {e}]"


def watcher_stop(job_id: str) -> str:
    """Stop a running watcher (equivalent to job_kill but with explicit naming)."""
    from .jobs import job_kill

    return job_kill(job_id)

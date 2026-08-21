"""Operator D — audit & replay.

An append-only, structured run-log for autonomous / relay loops. Every turn of
an orchestration writes one JSONL record; a run can later be summarised or
replayed for debugging and for producing "it actually ran" evidence.

Records live under <MCP_ALLOWED_BASE>/.companion_runs/<run_id>.jsonl so they are
inside the allowed base and survive restarts. Nothing here drives Copilot; it is
pure local bookkeeping, which is why it is the safe first thing to add.
"""
import json
import time
from pathlib import Path
from typing import Any, Optional

from .file_ops import ALLOWED_BASE
from .security import require_unlocked

RUNS_DIR = ALLOWED_BASE / ".companion_runs"


def _run_path(run_id: str) -> Path:
    safe = "".join(c for c in run_id if c.isalnum() or c in ("-", "_"))
    if not safe:
        raise ValueError("run_id must be alphanumeric / - / _")
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return RUNS_DIR / f"{safe}.jsonl"


def runlog_append(run_id: str, record: dict, ts: Optional[float] = None) -> str:
    """Append one structured record to a run's append-only JSONL log.

    Typical record keys for a fixed-point / relay loop: turn, sent, copilot_raw,
    decision, tools_run, state_before, state_after, delta_norm, refutation, agent.
    Any JSON-serialisable dict is accepted; a timestamp is added automatically.

    Args:
        run_id: Identifier for this run (alphanumeric / - / _).
        record: The record to append (any JSON object).
        ts: Optional explicit unix timestamp; defaults to now.
    """
    locked = require_unlocked()
    if locked:
        return locked
    return runlog_append_local(run_id, record, ts)


def runlog_append_local(run_id: str, record: dict, ts=None) -> str:
    """Append without the unlock gate. FOR IN-PROCESS CALLERS ONLY; not exposed as a tool.

    Same reasoning as tools/memory_ops.memory_save_local, and the same measured symptom, at a
    higher rate. The relay calls runlog_append from SIXTEEN sites and discards every return
    value; it runs as a standalone process, so require_unlocked() denied every one of them.
    Two consequences, both silent: the audit runlog has never been written -- .companion_runs
    holds nothing newer than 2026-07-17 despite heavy use since -- and each refusal freshened
    the identity-less refusal slot several times per turn, which is what made one process's
    refusals look like every fleet worker's lock.

    The gate answers "has the REMOTE caller proved possession of the password for its IP". A
    caller inside this process has no remote identity for that question to be about, and no way
    to satisfy it either: unlock() needs a request context too.
    """
    try:
        if not isinstance(record, dict):
            return "[runlog_append error: record must be an object]"
        path = _run_path(run_id)
        entry = dict(record)
        entry.setdefault("ts", ts if ts is not None else time.time())
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        n = sum(1 for _ in path.open(encoding="utf-8"))
        return f"appended to run {run_id} (now {n} record(s))"
    except Exception as e:
        return f"[runlog_append error: {type(e).__name__}: {e}]"


def runlog_read(run_id: str, start: int = 0, limit: int = 50) -> str:
    """Read records from a run log (1 JSON object per line).

    Args:
        run_id: The run to read.
        start: 0-based index of the first record to return.
        limit: Maximum number of records.
    """
    try:
        path = _run_path(run_id)
        if not path.is_file():
            return f"[runlog_read: no such run: {run_id}]"
        lines = path.read_text(encoding="utf-8").splitlines()
        total = len(lines)
        chunk = lines[start:start + limit]
        header = f"run {run_id}: {total} record(s), showing {start}..{start + len(chunk)}"
        return header + "\n" + "\n".join(chunk)
    except Exception as e:
        return f"[runlog_read error: {type(e).__name__}: {e}]"


def runlog_list() -> str:
    """List all recorded runs with their record counts and last-updated time."""
    try:
        if not RUNS_DIR.is_dir():
            return "(no runs recorded)"
        rows = []
        for p in RUNS_DIR.glob("*.jsonl"):
            try:
                n = sum(1 for _ in p.open(encoding="utf-8"))
                mtime = p.stat().st_mtime
                rows.append((p.stem, n, mtime))
            except OSError:
                continue
        if not rows:
            return "(no runs recorded)"
        rows.sort(key=lambda r: r[2], reverse=True)
        now = time.time()
        lines = [f"{'run_id':<28}  {'records':>7}  age"]
        for rid, n, mtime in rows:
            age_s = now - mtime
            age = f"{int(age_s)}s" if age_s < 3600 else f"{age_s/3600:.1f}h"
            lines.append(f"{rid:<28}  {n:>7}  {age}")
        return "\n".join(lines)
    except Exception as e:
        return f"[runlog_list error: {type(e).__name__}: {e}]"


def runlog_summarize(run_id: str, delta_key: str = "delta_norm") -> str:
    """Summarise a run: record count, turns, and the trajectory of a numeric key.

    Useful for inspecting convergence of a fixed-point loop (e.g. how delta_norm
    shrinks over turns) without reading every raw record.

    Args:
        run_id: The run to summarise.
        delta_key: Numeric field whose per-turn values to trace (default delta_norm).
    """
    try:
        path = _run_path(run_id)
        if not path.is_file():
            return f"[runlog_summarize: no such run: {run_id}]"
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not records:
            return f"run {run_id}: (empty)"
        deltas = [(r.get("turn", i), r.get(delta_key)) for i, r in enumerate(records)]
        deltas = [(t, v) for t, v in deltas if isinstance(v, (int, float))]
        tools = {}
        for r in records:
            for tool in (r.get("tools_run") or []):
                name = tool if isinstance(tool, str) else tool.get("name", "?")
                tools[name] = tools.get(name, 0) + 1
        lines = [
            f"run {run_id}",
            f"  records: {len(records)}",
            f"  first ts: {records[0].get('ts')}",
            f"  last ts:  {records[-1].get('ts')}",
        ]
        if deltas:
            trace = ", ".join(f"t{t}:{v:.4g}" for t, v in deltas[:20])
            lines.append(f"  {delta_key} trajectory: {trace}" + (" ..." if len(deltas) > 20 else ""))
            lines.append(f"  {delta_key} last: {deltas[-1][1]:.6g}")
        if tools:
            top = sorted(tools.items(), key=lambda kv: -kv[1])
            lines.append("  tools used: " + ", ".join(f"{n}×{c}" for n, c in top))
        return "\n".join(lines)
    except Exception as e:
        return f"[runlog_summarize error: {type(e).__name__}: {e}]"

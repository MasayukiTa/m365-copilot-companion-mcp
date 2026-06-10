"""Tool-driven task orchestration -- the correct loop mechanism.

The loop is NOT browser automation and NOT Claude driving a UI. It is the Copilot
agent (the only AI) calling these MCP tools inside its own tool-use loop:

    task_plan(goal, steps)        -> stores the plan, returns step 1
    <agent does step 1 with other tools (run_python, write_file, ...)>
    task_advance(task_id, result) -> records it, returns step 2
    <agent does step 2 ...>
    ...
    task_advance(...) on the last step -> auto-marks DONE + desktop notification

For a heavy goal the agent decomposes it once (passing `steps`), then walks the
steps. State is persisted under <MCP_ALLOWED_BASE>/.companion_tasks/<id>.json so
a task survives across Copilot turns: a later turn calls task_status / task_advance
and resumes exactly where it left off. The end user has no Claude installed and no
browser is driven -- only Copilot + this MCP server.

Notifications: the user is told on completion (task_advance past the last step, or
task_finish) AND on a dead end (task_stuck). So you can throw a heavy task, walk
away, and only get pulled back when it finishes or gets stuck.
"""
import json
import time
import uuid
from pathlib import Path
from typing import Optional

from .file_ops import ALLOWED_BASE
from .runlog_ops import runlog_append
from .security import require_unlocked

TASK_DIR = ALLOWED_BASE / ".companion_tasks"


def _path(task_id: str) -> Path:
    safe = "".join(c for c in task_id if c.isalnum() or c in ("-", "_"))
    if not safe:
        raise ValueError("invalid task_id")
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    return TASK_DIR / f"{safe}.json"


def _load(task_id: str) -> Optional[dict]:
    p = _path(task_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(task: dict) -> None:
    task["updated_at"] = time.time()
    _path(task["id"]).write_text(
        json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _notify(title: str, body: str) -> None:
    try:
        from .notify_ops import notify_desktop
        notify_desktop(title, body[:240])
    except Exception:
        pass


def _render(task: dict) -> str:
    lines = [f"task_id: {task['id']}", f"goal: {task['goal']}", f"status: {task['status']}"]
    for i, s in enumerate(task["steps"]):
        mark = {"done": "[x]", "current": "[>]", "pending": "[ ]"}.get(
            "current" if i == task["current"] and task["status"] == "running"
            else s["status"], "[ ]")
        lines.append(f"  {mark} {i + 1}. {s['text']}")
    return "\n".join(lines)


def task_plan(goal: str, steps: list[str]) -> str:
    """Start a multi-step task. YOU (the agent) decompose the goal into ordered
    steps and pass them here; this stores the plan and returns the first step.

    Use this for any goal too big to finish in one shot. After doing each step
    with your other tools, call task_advance(task_id, result) to get the next one.

    Args:
        goal: The overall goal in one sentence.
        steps: Ordered list of concrete step descriptions (you decide the split).
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        if not isinstance(steps, list) or not steps:
            return "[task_plan error: 'steps' must be a non-empty list]"
        tid = "task_" + uuid.uuid4().hex[:8]
        task = {
            "id": tid,
            "goal": goal,
            "steps": [{"text": str(s), "status": "pending", "result": ""} for s in steps],
            "current": 0,
            "status": "running",
            "created_at": time.time(),
        }
        task["steps"][0]["status"] = "current"
        _save(task)
        runlog_append(tid, {"event": "plan", "goal": goal, "n_steps": len(steps)})
        first = task["steps"][0]["text"]
        return (
            f"task_id: {tid}  ({len(steps)} steps)\n"
            f"STEP 1/{len(steps)}: {first}\n"
            f"-> do this step now with your tools, then call "
            f"task_advance(task_id='{tid}', result='<what happened>')."
        )
    except Exception as e:
        return f"[task_plan error: {type(e).__name__}: {e}]"


def task_advance(task_id: str, result: str) -> str:
    """Record the current step's result and move to the next step.

    Returns the next step to do, or -- if that was the last step -- marks the
    whole task DONE and fires a desktop notification automatically.

    Args:
        task_id: The id from task_plan.
        result: Short description of what the current step produced.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        task = _load(task_id)
        if task is None:
            return f"[task_advance error: unknown task_id {task_id}]"
        if task["status"] != "running":
            return f"[task_advance: task is '{task['status']}', not running]"
        i = task["current"]
        task["steps"][i]["status"] = "done"
        task["steps"][i]["result"] = str(result)[:2000]
        runlog_append(task_id, {"event": "step_done", "step": i + 1, "result": str(result)[:300]})

        if i + 1 >= len(task["steps"]):
            task["status"] = "done"
            _save(task)
            done_summary = "; ".join(s["result"][:60] for s in task["steps"] if s["result"])
            _notify("✅ タスク完了", f"{task['goal'][:80]} — 全{len(task['steps'])}ステップ完了")
            try:
                from .memory_ops import memory_save
                memory_save(f"task.{task_id}", f"DONE: {task['goal']} | {done_summary}",
                            scope="tasks", tags=["task", "done"])
            except Exception:
                pass
            return (
                f"ALL DONE — task {task_id} complete ({len(task['steps'])} steps). "
                f"Desktop notification sent. Summarize the outcome for the user."
            )

        task["current"] = i + 1
        task["steps"][i + 1]["status"] = "current"
        _save(task)
        nxt = task["steps"][i + 1]["text"]
        return (
            f"STEP {i + 2}/{len(task['steps'])}: {nxt}\n"
            f"-> do this step, then call task_advance(task_id='{task_id}', result='...'). "
            f"If you cannot proceed, call task_stuck(task_id='{task_id}', reason='...')."
        )
    except Exception as e:
        return f"[task_advance error: {type(e).__name__}: {e}]"


def task_status(task_id: str) -> str:
    """Show a task's plan and progress. Use this at the start of a later turn to
    resume a task that did not finish in the previous turn."""
    try:
        task = _load(task_id)
        if task is None:
            return f"[task_status: unknown task_id {task_id}]"
        return _render(task)
    except Exception as e:
        return f"[task_status error: {type(e).__name__}: {e}]"


def task_stuck(task_id: str, reason: str) -> str:
    """Mark a task as stuck (needs a human) and fire a desktop notification.

    Args:
        task_id: The task.
        reason: Why you cannot proceed.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        task = _load(task_id)
        if task is None:
            return f"[task_stuck: unknown task_id {task_id}]"
        task["status"] = "stuck"
        task["stuck_reason"] = str(reason)
        _save(task)
        runlog_append(task_id, {"event": "stuck", "reason": str(reason)[:300]})
        _notify("⚠ タスク停止 (要確認)", f"{task['goal'][:60]} — {reason[:120]}")
        return f"task {task_id} marked STUCK. Desktop notification sent. Tell the user what is blocking."
    except Exception as e:
        return f"[task_stuck error: {type(e).__name__}: {e}]"


def task_finish(task_id: str, summary: str) -> str:
    """Explicitly mark a task DONE with a summary and notify (alternative to
    letting the final task_advance auto-finish)."""
    locked = require_unlocked()
    if locked:
        return locked
    try:
        task = _load(task_id)
        if task is None:
            return f"[task_finish: unknown task_id {task_id}]"
        task["status"] = "done"
        task["summary"] = str(summary)
        for s in task["steps"]:
            if s["status"] != "done":
                s["status"] = "done"
        _save(task)
        _notify("✅ タスク完了", f"{task['goal'][:70]} — {summary[:120]}")
        return f"task {task_id} marked DONE. Desktop notification sent."
    except Exception as e:
        return f"[task_finish error: {type(e).__name__}: {e}]"


def task_list() -> str:
    """List all tasks with their status."""
    try:
        if not TASK_DIR.is_dir():
            return "(no tasks)"
        rows = []
        for p in TASK_DIR.glob("task_*.json"):
            try:
                t = json.loads(p.read_text(encoding="utf-8"))
                done = sum(1 for s in t["steps"] if s["status"] == "done")
                rows.append((t.get("updated_at", 0), t["id"], t["status"], done, len(t["steps"]), t["goal"]))
            except Exception:
                continue
        if not rows:
            return "(no tasks)"
        rows.sort(reverse=True)
        lines = []
        for _, tid, st, done, total, goal in rows:
            lines.append(f"{tid}  {st:<8}  {done}/{total}  {goal[:50]}")
        return "\n".join(lines)
    except Exception as e:
        return f"[task_list error: {type(e).__name__}: {e}]"

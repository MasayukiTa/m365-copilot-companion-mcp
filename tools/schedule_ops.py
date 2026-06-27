import os
import re
import subprocess
from datetime import datetime
from typing import Optional

from .security import require_unlocked

_GATE_ENV = "MCP_REQUIRE_GATE_FOR_SIDE_EFFECTS"
_PERSISTENT_TRIGGERS = {"onlogon", "onstart"}


def _side_effects_gated() -> bool:
    """Return True when the HITL confirmation gate is active (default on)."""
    return os.environ.get(_GATE_ENV, "1") == "1"

PREFIX = "m365-copilot-companion-mcp-"
DAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}
TIMEOUT = 30


def _run(args: list[str]) -> tuple[int, str, str]:
    r = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT, shell=False)
    return r.returncode, r.stdout, r.stderr


def _full_name(name: str) -> str:
    if not name:
        raise ValueError("task name is required")
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
        raise ValueError("name must match [A-Za-z0-9_-]+")
    return name if name.startswith(PREFIX) else PREFIX + name


def _build_args(name: str, command: str, trigger: dict) -> list[str]:
    if not isinstance(trigger, dict) or "kind" not in trigger:
        raise ValueError("trigger must be a dict with a 'kind' key")
    kind = trigger["kind"].lower()
    args = ["schtasks", "/Create", "/F", "/TN", name, "/TR", command]
    if kind == "daily":
        time = trigger.get("time", "09:00")
        args += ["/SC", "DAILY", "/ST", time]
    elif kind == "weekly":
        days = trigger.get("days") or ["MON"]
        for d in days:
            if d.upper() not in DAYS:
                raise ValueError(f"invalid day {d!r}")
        time = trigger.get("time", "09:00")
        args += ["/SC", "WEEKLY", "/D", ",".join(d.upper() for d in days), "/ST", time]
    elif kind == "monthly":
        time = trigger.get("time", "09:00")
        day_of_month = trigger.get("day", 1)
        args += ["/SC", "MONTHLY", "/D", str(day_of_month), "/ST", time]
    elif kind == "once":
        dt = trigger.get("datetime")
        if not dt:
            raise ValueError("once trigger needs 'datetime': 'YYYY-MM-DD HH:MM'")
        date_part, time_part = dt.split(" ", 1)
        y, m, d = date_part.split("-")
        sd = f"{m}/{d}/{y}"
        args += ["/SC", "ONCE", "/SD", sd, "/ST", time_part]
    elif kind == "minutes":
        every = int(trigger.get("every", 15))
        if not 1 <= every <= 1439:
            raise ValueError("'every' minutes must be 1..1439")
        args += ["/SC", "MINUTE", "/MO", str(every)]
    elif kind == "hourly":
        every = int(trigger.get("every", 1))
        args += ["/SC", "HOURLY", "/MO", str(every)]
    elif kind == "onlogon":
        args += ["/SC", "ONLOGON"]
    elif kind == "onstart":
        args += ["/SC", "ONSTART"]
    else:
        raise ValueError(
            "trigger.kind must be one of: daily, weekly, monthly, once, minutes, hourly, onlogon, onstart"
        )
    return args


def schedule_create(
    name: str,
    command: str,
    trigger: dict,
    description: Optional[str] = None,
    confirm: bool = False,
) -> str:
    """Register a Windows Scheduled Task that runs a command on the given trigger.

    Task names are automatically prefixed with `m365-copilot-companion-mcp-` so they are easy
    to find and won't collide with other system tasks.

    Args:
        name: Short identifier. Letters/digits/underscore/hyphen only.
        command: Shell command line to execute (e.g.
            'C:\\\\Users\\\\you\\\\m365-copilot-companion-mcp\\\\.venv\\\\Scripts\\\\python.exe C:\\\\Users\\\\you\\\\scripts\\\\weekly.py').
            Quote paths that contain spaces.
        trigger: Dict describing when to fire. Examples:
            {"kind": "daily", "time": "09:00"}
            {"kind": "weekly", "days": ["MON", "FRI"], "time": "08:30"}
            {"kind": "monthly", "day": 1, "time": "10:00"}
            {"kind": "once", "datetime": "2026-06-01 14:00"}
            {"kind": "minutes", "every": 15}
            {"kind": "hourly", "every": 2}
            {"kind": "onlogon"}   <- requires confirm=True when gate is active
            {"kind": "onstart"}   <- requires confirm=True when gate is active
        description: Optional human description (not used by schtasks, kept for self-docs).
        confirm: Required to be True for onlogon/onstart triggers when
            MCP_REQUIRE_GATE_FOR_SIDE_EFFECTS=1 (the default). These triggers
            persist across reboots/logins and execute automatically, making them
            a significant persistent side effect. Re-call with confirm=True after
            reviewing the command and trigger. Not required for time-based triggers.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        full = _full_name(name)
        if not command or not command.strip():
            return "[schedule_create error: command is required]"
        if "&" in command and ";" not in command:
            # schtasks /TR with shell metacharacters is fragile; warn rather than fail.
            pass
        # HITL gate: onlogon/onstart tasks persist and run automatically at login/boot.
        kind = (trigger.get("kind") or "").lower() if isinstance(trigger, dict) else ""
        if kind in _PERSISTENT_TRIGGERS and _side_effects_gated() and not confirm:
            return (
                f"[confirmation required] This will register a persistent scheduled task "
                f"{full!r} that runs automatically on every {kind}. "
                f"Command: {command!r}. "
                "This action persists across reboots. "
                "Re-call with confirm=True to proceed."
            )
        args = _build_args(full, command, trigger)
        rc, out, err = _run(args)
        body = (out or "").strip()
        if err.strip():
            body += "\n[stderr] " + err.strip()
        if rc != 0:
            return f"[schedule_create failed: rc={rc}]\n{body}"
        return f"created: {full}\n{body}" + (f"\n(desc: {description})" if description else "")
    except subprocess.TimeoutExpired:
        return f"[schedule_create timeout after {TIMEOUT}s]"
    except Exception as e:
        return f"[schedule_create error: {type(e).__name__}: {e}]"


def schedule_list(all_tasks: bool = False) -> str:
    """List scheduled tasks. By default only m365-copilot-companion-mcp-* tasks are shown.

    Args:
        all_tasks: When true, list every task on the system (very long output).
    """
    try:
        rc, out, err = _run(["schtasks", "/Query", "/FO", "CSV"])
        if rc != 0:
            return f"[schedule_list failed: {err.strip() or out.strip()}]"
        lines = out.strip().splitlines()
        if not lines:
            return "(no tasks found)"
        header = lines[0]
        body_lines = lines[1:]
        if not all_tasks:
            body_lines = [l for l in body_lines if PREFIX in l]
        if not body_lines:
            return "(no m365-copilot-companion-mcp-* tasks registered)"
        return "\n".join([header, *body_lines])
    except Exception as e:
        return f"[schedule_list error: {type(e).__name__}: {e}]"


def schedule_info(name: str) -> str:
    """Show the full configuration of a single scheduled task (verbose)."""
    try:
        full = _full_name(name)
        rc, out, err = _run(["schtasks", "/Query", "/TN", full, "/V", "/FO", "LIST"])
        if rc != 0:
            return f"[schedule_info failed: {err.strip() or out.strip()}]"
        return out.strip()
    except Exception as e:
        return f"[schedule_info error: {type(e).__name__}: {e}]"


def schedule_run_now(name: str) -> str:
    """Trigger a scheduled task to run immediately, without waiting for its trigger."""
    locked = require_unlocked()
    if locked:
        return locked
    try:
        full = _full_name(name)
        rc, out, err = _run(["schtasks", "/Run", "/TN", full])
        if rc != 0:
            return f"[schedule_run_now failed: {err.strip() or out.strip()}]"
        return out.strip() or f"triggered: {full}"
    except Exception as e:
        return f"[schedule_run_now error: {type(e).__name__}: {e}]"


def schedule_delete(name: str) -> str:
    """Delete a scheduled task by name (prefix auto-added)."""
    locked = require_unlocked()
    if locked:
        return locked
    try:
        full = _full_name(name)
        rc, out, err = _run(["schtasks", "/Delete", "/TN", full, "/F"])
        if rc != 0:
            return f"[schedule_delete failed: {err.strip() or out.strip()}]"
        return out.strip() or f"deleted: {full}"
    except Exception as e:
        return f"[schedule_delete error: {type(e).__name__}: {e}]"

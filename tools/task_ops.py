import json
import time
from pathlib import Path
from typing import Optional

STATE_FILE = Path(__file__).resolve().parent.parent / ".todo_state.json"
VALID_STATUS = {"pending", "in_progress", "completed"}


def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"items": []}
    return {"items": []}


def _save(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def todo_write(items: list[dict]) -> str:
    """Replace the session todo list with the given items.

    Each item is a dict with keys:
      - id (optional string, auto-generated if missing)
      - subject (required string)
      - status (optional: pending|in_progress|completed, default pending)
      - active_form (optional present-continuous label for spinners)

    Call this whenever the plan changes. The full list is rewritten on each call.

    Args:
        items: Ordered list of todo dicts.
    """
    try:
        if not isinstance(items, list):
            return "[todo_write error: items must be a list]"
        normalized = []
        for idx, raw in enumerate(items, 1):
            if not isinstance(raw, dict):
                return f"[todo_write error: item #{idx} is not an object]"
            subject = raw.get("subject")
            if not subject:
                return f"[todo_write error: item #{idx} missing 'subject']"
            status = raw.get("status", "pending")
            if status not in VALID_STATUS:
                return f"[todo_write error: item #{idx} has invalid status {status!r}]"
            normalized.append(
                {
                    "id": raw.get("id") or f"t{idx}",
                    "subject": subject,
                    "status": status,
                    "active_form": raw.get("active_form") or subject,
                }
            )
        state = {"updated_at": time.time(), "items": normalized}
        _save(state)
        return _render(state)
    except Exception as e:
        return f"[todo_write error: {type(e).__name__}: {e}]"


def todo_list() -> str:
    """Return the current session todo list."""
    return _render(_load())


def todo_clear() -> str:
    """Delete the session todo list."""
    try:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        return "Cleared todo list."
    except Exception as e:
        return f"[todo_clear error: {type(e).__name__}: {e}]"


def _render(state: dict) -> str:
    items = state.get("items", [])
    if not items:
        return "(empty todo list)"
    lines = []
    for item in items:
        mark = {
            "completed": "[x]",
            "in_progress": "[~]",
            "pending": "[ ]",
        }.get(item["status"], "[ ]")
        lines.append(f"{mark} {item['id']}  {item['subject']}")
    pending = sum(1 for i in items if i["status"] == "pending")
    progress = sum(1 for i in items if i["status"] == "in_progress")
    done = sum(1 for i in items if i["status"] == "completed")
    lines.append(f"--- total {len(items)}  done {done}  in_progress {progress}  pending {pending}")
    return "\n".join(lines)

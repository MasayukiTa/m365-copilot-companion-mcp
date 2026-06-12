"""project_memory.py -- persistent, AUTO-accumulated memory of a codebase across tasks.

Claude Code remembers a repo only through a hand-written CLAUDE.md. This goes further:
every finished code_task records a compact note (goal + outcome + a snippet of what the
agent reported) keyed by the folder, and the NEXT task on that folder gets those notes
primed into its goal. So the agent accumulates understanding of your codebase over time
without you writing anything -- "last time on this repo I ...".

Frame-side only: notes live in a plain JSON file (.fleet/project_notes.json), so no MCP
unlock is needed and nothing executes. stdlib only.
"""
from __future__ import annotations

import json
import os
import time

_MAX_PER_FOLDER = 20          # keep the most recent N notes per folder
_NOTE_CAP = 280               # chars per note snippet


def _store_path(state_dir):
    return os.path.join(state_dir or ".fleet", "project_notes.json")


def _load(state_dir):
    try:
        with open(_store_path(state_dir), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(state_dir, data):
    p = _store_path(state_dir)
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, p)


def _key(folder):
    return os.path.abspath(folder).replace("\\", "/").lower()


def record_task(folder, goal, outcome, note="", state_dir=".fleet", ts=None):
    """Append one task note for `folder` (most-recent-last, capped). `note` is a short
    free-text takeaway (e.g. a slice of the agent's final report). Never raises."""
    try:
        data = _load(state_dir)
        key = _key(folder)
        items = data.get(key) or []
        items.append({
            "ts": ts if ts is not None else time.time(),
            "goal": (goal or "")[:160],
            "outcome": outcome or "",
            "note": " ".join((note or "").split())[:_NOTE_CAP],
        })
        data[key] = items[-_MAX_PER_FOLDER:]
        _save(state_dir, data)
        return True
    except Exception:
        return False


def load_notes(folder, max_items=5, state_dir=".fleet"):
    """Return a short text block of the most recent task notes for `folder`, or "" if
    none. Suitable to prime into a goal so the agent recalls prior work on this repo."""
    try:
        items = _load(state_dir).get(_key(folder)) or []
    except Exception:
        items = []
    if not items:
        return ""
    lines = ["このリポジトリでの過去の作業（新しい順）:"]
    for it in reversed(items[-max_items:]):
        g = it.get("goal", "")
        o = it.get("outcome", "")
        n = it.get("note", "")
        lines.append("- [%s] %s%s" % (o, g, ("  / " + n) if n else ""))
    return "\n".join(lines)

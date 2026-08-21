# Taxonomy note (book SS17 / SS28.16): this module IS the "semantic memory" store
# (facts/preferences, keyed lookup) -- semantic_* below are thin backward-compat
# aliases over the same memory_* functions/state file, added so both vocabularies
# work without renaming anything callers already depend on. Procedural memory
# (reusable how-to snippets) lives in tools/procedural_memory.py, its own state
# file. Episodic memory (ordered event history of a run) is already served by
# tools/runlog_ops.py -- no new episodic store is built here.
import json
import time
from pathlib import Path
from typing import Optional

from .security import require_unlocked

STATE_FILE = Path(__file__).resolve().parent.parent / ".memory_state.json"
MAX_VALUE_CHARS = 16_000


def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"scopes": {}}
    return {"scopes": {}}


def _save(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def memory_save(
    key: str,
    value: str,
    scope: str = "global",
    tags: Optional[list[str]] = None,
) -> str:
    """Persist a value under a key for cross-session recall.

    Memory is plain text intended for the agent to remember facts about the
    user, ongoing projects, conventions, etc. Re-saving the same key overwrites
    the previous value but keeps it in history.

    Args:
        key: Short identifier (e.g. "user.role" or "project.alpha.status").
        value: The text to remember. Max ~16k characters.
        scope: Optional namespace ("global", "project_alpha", "user_prefs" ...).
        tags: Optional list of tags for later filtering.
    """
    locked = require_unlocked()
    if locked:
        return locked
    return memory_save_local(key, value, scope=scope, tags=tags)


def memory_save_local(
    key: str,
    value: str,
    scope: str = "global",
    tags: Optional[list[str]] = None,
) -> str:
    """Save without the unlock gate. FOR IN-PROCESS CALLERS ONLY; not exposed as a tool.

    WHY THIS EXISTS, AND WHY IT IS NOT A HOLE. The gate answers one question: has the REMOTE
    caller behind this HTTP request proved possession of the password for its IP. A caller
    running inside this process has no remote identity and no request, so the gate cannot
    answer it -- `require_unlocked()` denies, and `unlock()` itself needs a request too, so
    such a caller has no move that works. Denying is correct; telling it to unlock is telling
    it to do something impossible.

    MEASURED, 2026-08-21: the relay called the gated `memory_save` once per turn and discarded
    the returned lock string, so its cross-session history had NEVER been written -- silently,
    on every turn of every run. Worse, each refusal freshened the identity-less refusal slot,
    and concurrent fleet workers then read someone else's refusal as their own lock and burned
    their auto-unlock attempts.

    The boundary this keeps is the one that matters: nothing reachable from outside this
    process calls this. `memory_save` above still gates, and it is what the tool surface
    exposes. An in-process caller is already inside every boundary the gate protects.
    """
    try:
        if not key or not isinstance(key, str):
            return "[memory_save error: key must be a non-empty string]"
        if not isinstance(value, str):
            return "[memory_save error: value must be a string]"
        if len(value) > MAX_VALUE_CHARS:
            return f"[memory_save error: value exceeds {MAX_VALUE_CHARS} chars]"
        state = _load()
        bucket = state["scopes"].setdefault(scope, {})
        prev = bucket.get(key)
        bucket[key] = {
            "value": value,
            "tags": list(tags) if tags else (prev["tags"] if prev else []),
            "updated_at": time.time(),
            "history_count": (prev.get("history_count", 0) if prev else 0) + 1,
        }
        _save(state)
        return (
            f"saved [{scope}/{key}] ({len(value)} chars, "
            f"rev #{bucket[key]['history_count']})"
        )
    except Exception as e:
        return f"[memory_save error: {type(e).__name__}: {e}]"


def memory_load(key: str, scope: str = "global") -> str:
    """Retrieve a previously saved memory entry by key."""
    try:
        state = _load()
        bucket = state["scopes"].get(scope, {})
        entry = bucket.get(key)
        if not entry:
            return f"[memory_load: no entry at {scope}/{key}]"
        return entry["value"]
    except Exception as e:
        return f"[memory_load error: {type(e).__name__}: {e}]"


def memory_list(
    scope: Optional[str] = None,
    tag: Optional[str] = None,
    contains: Optional[str] = None,
) -> str:
    """List memory keys, optionally filtered by scope/tag/substring.

    Returns a compact catalog (scope/key, length, updated_at). Use memory_load
    to fetch a specific value.
    """
    try:
        state = _load()
        rows = []
        for sc, bucket in state["scopes"].items():
            if scope and sc != scope:
                continue
            for key, entry in bucket.items():
                if tag and tag not in (entry.get("tags") or []):
                    continue
                if contains and contains.lower() not in entry["value"].lower():
                    continue
                ts = entry.get("updated_at", 0)
                rows.append(
                    (
                        sc,
                        key,
                        len(entry["value"]),
                        ts,
                        ",".join(entry.get("tags") or []),
                    )
                )
        if not rows:
            return "(no memory entries)"
        rows.sort(key=lambda r: r[3], reverse=True)
        lines = [f"{'scope':<14}  {'key':<32}  {'chars':>6}  age      tags"]
        now = time.time()
        for sc, k, n, ts, tg in rows:
            age = _human_age(now - ts) if ts else "—"
            lines.append(f"{sc:<14}  {k:<32}  {n:>6}  {age:<7}  {tg}")
        lines.append(f"--- {len(rows)} entries")
        return "\n".join(lines)
    except Exception as e:
        return f"[memory_list error: {type(e).__name__}: {e}]"


def memory_delete(key: str, scope: str = "global") -> str:
    """Delete a single memory entry."""
    locked = require_unlocked()
    if locked:
        return locked
    try:
        state = _load()
        bucket = state["scopes"].get(scope, {})
        if key not in bucket:
            return f"[memory_delete: no entry at {scope}/{key}]"
        bucket.pop(key)
        _save(state)
        return f"deleted [{scope}/{key}]"
    except Exception as e:
        return f"[memory_delete error: {type(e).__name__}: {e}]"


def semantic_memory_save(
    key: str,
    value: str,
    scope: str = "global",
    tags: Optional[list[str]] = None,
) -> str:
    """Alias for memory_save (book taxonomy name for this store: "semantic memory").
    Same state file, same behavior -- see memory_save for full docs."""
    # NOTE: this require_unlocked() call is redundant with the one inside memory_save
    # (harmless -- same IP/gate check twice) but deliberate: tool_annotations.py derives
    # readOnlyHint by textually grepping a function's OWN source for "require_unlocked(",
    # so a pure passthrough wrapper would be mis-derived as read-only. Keeping the literal
    # call here keeps the mechanical derivation correct for this alias too.
    locked = require_unlocked()
    if locked:
        return locked
    return memory_save(key, value, scope=scope, tags=tags)


def semantic_memory_load(key: str, scope: str = "global") -> str:
    """Alias for memory_load. See memory_load for full docs."""
    return memory_load(key, scope=scope)


def semantic_memory_list(
    scope: Optional[str] = None,
    tag: Optional[str] = None,
    contains: Optional[str] = None,
) -> str:
    """Alias for memory_list. See memory_list for full docs."""
    return memory_list(scope=scope, tag=tag, contains=contains)


def semantic_memory_delete(key: str, scope: str = "global") -> str:
    """Alias for memory_delete. See memory_delete for full docs."""
    # See the comment in semantic_memory_save: this literal require_unlocked() call
    # keeps tool_annotations.py's mechanical readOnlyHint derivation correct for
    # this alias (it greps THIS function's own source, not memory_delete's).
    locked = require_unlocked()
    if locked:
        return locked
    return memory_delete(key, scope=scope)


def _human_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / 86400)}d"

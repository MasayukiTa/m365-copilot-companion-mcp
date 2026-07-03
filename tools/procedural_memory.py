"""Procedural memory -- reusable success snippets ("how-to" workflows).

Book-grounded (SS17 / SS28.16 taxonomy): memory_ops.py is SEMANTIC memory (facts:
"user.role", "project.alpha.status"). Procedural memory is a distinct kind: a
reusable, previously-successful HOW-TO -- a snippet/recipe/workflow the agent can
recall next time it faces a similar intent, instead of re-deriving it. Episodic
memory (what happened, in order, this run) is already served by runlog_ops.py --
this module does not duplicate that.

Mirrors memory_ops.py's JSON-state-file pattern exactly (same read/write guard
idioms, same require_unlocked() gating on writes, same substring-search style),
in its own state file so the two stores never collide. No new dependency: this
is a keyword/substring index over a JSON dict, which is the right scale for a
single-operator repeat-task environment (same judgment call memory_ops already
made).
"""
import json
import re
import time
from pathlib import Path
from typing import Optional

from .security import require_unlocked

STATE_FILE = Path(__file__).resolve().parent.parent / ".procedural_memory.json"
MAX_SNIPPET_CHARS = 16_000


def _load() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("procedures"), dict):
                return data
            return {"procedures": {}}
        except Exception:
            return {"procedures": {}}
    return {"procedures": {}}


def _save(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _slugify(intent: str) -> str:
    """Turn an intent string into a stable dict key: lowercase, non-alnum -> '_'."""
    slug = re.sub(r"[^a-z0-9]+", "_", intent.strip().lower()).strip("_")
    return slug or "untitled"


def procedural_memory_save(
    intent: str,
    snippet: str,
    tags: str = "",
    context: str = "",
) -> str:
    """Save a reusable success snippet (a how-to / learned workflow) for later reuse.

    Use this after successfully completing a non-trivial repeat-shaped task, so the
    NEXT time a similar intent comes up the recipe can be recalled instead of
    re-derived from scratch (e.g. "unlock the eval host SSH" -> the exact working command
    sequence). Distinct from memory_save (facts) and runlog_append (event history).
    Re-saving the same intent updates the entry in place and bumps its revision
    count (history is not lost, mirroring memory_ops' overwrite-with-rev pattern).

    Args:
        intent: Short description of the task/goal this snippet solves (used to
            derive the storage key via slugify, e.g. "restart prod backend").
        snippet: The reusable recipe itself -- commands, code, or steps. Max ~16k chars.
        tags: Comma-separated tags for later filtering (e.g. "the eval host,ssh,docker").
        context: Optional free-text note (when/why it worked, caveats, gotchas).
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        if not intent or not isinstance(intent, str):
            return "[procedural_memory_save error: intent must be a non-empty string]"
        if not isinstance(snippet, str) or not snippet:
            return "[procedural_memory_save error: snippet must be a non-empty string]"
        if len(snippet) > MAX_SNIPPET_CHARS:
            return f"[procedural_memory_save error: snippet exceeds {MAX_SNIPPET_CHARS} chars]"
        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
        slug = _slugify(intent)
        state = _load()
        bucket = state["procedures"]
        prev = bucket.get(slug)
        bucket[slug] = {
            "intent": intent,
            "snippet": snippet,
            "tags": tag_list,
            "context": context or "",
            "updated_at": time.time(),
            "history_count": (prev.get("history_count", 0) if prev else 0) + 1,
        }
        _save(state)
        return (
            f"saved procedure [{slug}] ({len(snippet)} chars, "
            f"rev #{bucket[slug]['history_count']})"
        )
    except Exception as e:
        return f"[procedural_memory_save error: {type(e).__name__}: {e}]"


def _score(entry: dict, tokens: list[str]) -> int:
    """Count how many query tokens appear (case-insensitively) across intent + tags
    + snippet + context. Simple relevance proxy -- no embeddings, no new deps."""
    haystack = " ".join(
        [
            entry.get("intent", ""),
            " ".join(entry.get("tags") or []),
            entry.get("snippet", ""),
            entry.get("context", ""),
        ]
    ).lower()
    return sum(1 for t in tokens if t and t in haystack)


def procedural_memory_search(query: str, limit: int = 10) -> str:
    """Search saved procedures by keyword/substring over intent, tags, snippet, context.

    Read-only. Ranks results by how many query tokens matched (simple relevance),
    newest-updated first as a tiebreak. Call this BEFORE re-deriving a recipe for a
    task that feels like something done before -- cheaper than re-solving.

    Args:
        query: Free-text query; split on whitespace into case-insensitive tokens.
        limit: Maximum number of results to return (default 10).
    """
    try:
        if not query or not isinstance(query, str):
            return "(no matches: empty query)"
        tokens = [t.lower() for t in query.split() if t.strip()]
        if not tokens:
            return "(no matches: empty query)"
        state = _load()
        bucket = state.get("procedures", {})
        scored = []
        for slug, entry in bucket.items():
            s = _score(entry, tokens)
            if s > 0:
                scored.append((s, entry.get("updated_at", 0), slug, entry))
        if not scored:
            return "(no matches)"
        scored.sort(key=lambda r: (r[0], r[1]), reverse=True)
        scored = scored[: max(0, limit)]
        lines = [f"{len(scored)} match(es) for {query!r}:"]
        for s, ts, slug, entry in scored:
            tag_str = ",".join(entry.get("tags") or [])
            snippet_preview = entry.get("snippet", "")
            if len(snippet_preview) > 200:
                snippet_preview = snippet_preview[:200] + "..."
            lines.append(
                f"- [{slug}] score={s} tags=[{tag_str}] intent={entry.get('intent', '')!r}\n"
                f"    {snippet_preview}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"[procedural_memory_search error: {type(e).__name__}: {e}]"


def procedural_memory_delete(intent_slug: str) -> str:
    """Delete a saved procedure by its slug (see procedural_memory_save/search output).

    Args:
        intent_slug: The slug key (e.g. from a procedural_memory_search result line
            like "- [restart_prod_backend] ..."), or a raw intent string (it will be
            slugified the same way procedural_memory_save does).
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        if not intent_slug or not isinstance(intent_slug, str):
            return "[procedural_memory_delete error: intent_slug must be a non-empty string]"
        state = _load()
        bucket = state["procedures"]
        key = intent_slug if intent_slug in bucket else _slugify(intent_slug)
        if key not in bucket:
            return f"[procedural_memory_delete: no entry at {intent_slug!r}]"
        bucket.pop(key)
        _save(state)
        return f"deleted procedure [{key}]"
    except Exception as e:
        return f"[procedural_memory_delete error: {type(e).__name__}: {e}]"

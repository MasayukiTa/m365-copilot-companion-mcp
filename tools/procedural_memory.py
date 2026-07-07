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

# --- markdown bulk-import helpers (procedural_memory_import_markdown) ---------------------
# Heading split: level-2 or level-3 ("## " / "### ") only -- level-1 is reserved for the
# doc title (used as the preamble chunk's intent) and level-4+ is treated as body text,
# not a chunk boundary.
_HEADING_RE = re.compile(r"^(#{2,3})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
# Cue words that flag a line as worth carrying into `context` (gotchas / stale info /
# conclusions). Deliberately case-sensitive substring match: "NG" as-cased avoids
# false-positiving on ordinary English words containing "ng".
_CUE_WORDS = ("注意", "NG", "古い", "使わない", "高速化", "結論", "落とし穴", "gotcha")
# "Table-ish" tokens: SCREAMING_SNAKE_CASE identifiers that look like DB table/column
# names in prose (e.g. internal DB docs). Purely mechanical -- no allowlist of real names.
_TABLE_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9_]{4,}\b")
# SQL syntax words that also happen to look like SCREAMING_SNAKE_CASE identifiers and
# get swept up by _TABLE_TOKEN_RE when prose includes a fenced SQL snippet (e.g.
# "GROUP BY", "HAVING COUNT(*)>1", "DATEADD(...)"). These are noise, not table/column
# names, so they're excluded from table: tags. Uppercase, matched via tok.upper() so
# the check is case-insensitive at the call site. Includes some short (3-4 letter)
# keywords too even though _TABLE_TOKEN_RE itself requires 5+ chars -- cheap safety
# net if the regex or a caller's own extraction ever gets more permissive.
SQL_STOPWORDS = frozenset({
    "SELECT", "FROM", "WHERE", "GROUP", "ORDER", "HAVING", "UNION", "EXISTS",
    "INSERT", "UPDATE", "DELETE", "INNER", "OUTER", "RIGHT", "CROSS", "APPLY",
    "DISTINCT", "BETWEEN", "VALUES", "CREATE", "TABLE", "INDEX", "USING",
    "LIMIT", "OFFSET", "COUNT", "DATEADD", "DATEDIFF", "EOMONTH", "GETDATE",
    "CONVERT", "ISNULL", "COALESCE", "NULLIF", "ROUND", "SUBSTRING",
    "CHARINDEX", "DECLARE", "WITH", "CASE", "WHEN", "THEN", "ELSE", "CAST",
    "JOIN", "LEFT", "NOLOCK", "TOP", "ASC", "DESC", "LIKE", "AND", "NOT",
    "NULL", "INTO", "SET",
})


def _split_markdown_chunks(text: str, basename: str) -> list[tuple[str, str]]:
    """Split markdown text into (intent_seed, body) pairs on ## / ### headings.

    Content before the first heading becomes its own preamble chunk, with intent
    seeded from the document's H1 title if present, else the file's basename."""
    headings = list(_HEADING_RE.finditer(text))
    chunks: list[tuple[str, str]] = []

    first_start = headings[0].start() if headings else len(text)
    preamble = text[:first_start]
    if preamble.strip():
        h1_match = _H1_RE.search(preamble)
        preamble_intent = h1_match.group(1).strip() if h1_match else Path(basename).stem
        chunks.append((preamble_intent, preamble))

    for i, m in enumerate(headings):
        heading_text = m.group(2).strip()
        body_start = m.end()
        body_end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        chunks.append((heading_text, text[body_start:body_end]))

    return chunks


def _extract_snippet(body: str, limit: int = 800) -> str:
    """Fenced code blocks (joined) if any, else the first `limit` chars of the body."""
    blocks = _CODE_BLOCK_RE.findall(body)
    if blocks:
        joined = "\n\n".join(b.strip("\n") for b in blocks)
        return joined[:MAX_SNIPPET_CHARS]
    return body[:limit]


def _extract_context(body: str, limit: int = 800) -> str:
    """Lines containing any cue word, joined, capped at `limit` chars. May be empty."""
    hits = [ln.strip() for ln in body.splitlines() if any(w in ln for w in _CUE_WORDS)]
    context = "\n".join(hits)
    return context[:limit]


def _extract_table_tags(body: str, max_tokens: int = 8) -> list[str]:
    """Up to `max_tokens` distinct SCREAMING_SNAKE_CASE-ish tokens, as "table:<lower>" tags.

    Tokens that are pure SQL syntax (SELECT, GROUP, HAVING, DATEADD, ...) are dropped
    via SQL_STOPWORDS -- they show up whenever the prose includes a fenced SQL snippet
    and are noise, not table/column names.
    """
    seen: list[str] = []
    for m in _TABLE_TOKEN_RE.finditer(body):
        tok = m.group(0)
        if tok.upper() in SQL_STOPWORDS:
            continue
        if tok not in seen:
            seen.append(tok)
        if len(seen) >= max_tokens:
            break
    return [f"table:{t.lower()}" for t in seen]


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


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
    re-derived from scratch (e.g. "unlock kiyus SSH" -> the exact working command
    sequence). Distinct from memory_save (facts) and runlog_append (event history).
    Re-saving the same intent updates the entry in place and bumps its revision
    count (history is not lost, mirroring memory_ops' overwrite-with-rev pattern).

    Args:
        intent: Short description of the task/goal this snippet solves (used to
            derive the storage key via slugify, e.g. "restart prod backend").
        snippet: The reusable recipe itself -- commands, code, or steps. Max ~16k chars.
        tags: Comma-separated tags for later filtering (e.g. "kiyus,ssh,docker").
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


def procedural_memory_import_markdown(path: str, tags: str = "") -> str:
    """Bulk-import a markdown doc into procedural memory, one procedure per ## / ###
    heading section (e.g. DB-exploration notes, a runbook, a design doc's "gotchas").

    Splits `path` on level-2/3 headings (content before the first heading becomes its
    own preamble chunk, seeded from the H1 title or the filename). Per chunk: intent
    comes from the heading text; snippet is the chunk's fenced code blocks if any, else
    its first ~800 chars; context pulls out lines containing cue words (注意/NG/古い/
    使わない/高速化/結論/落とし穴/gotcha); tags get `tags` + "import" + "src:<basename>"
    plus up to 8 SCREAMING_SNAKE_CASE-ish tokens found in the chunk (as "table:<name>",
    lowercased) -- a mechanical guess at DB table/column names mentioned in prose.
    Reuses procedural_memory_save for the actual writes (same slug/rev semantics).

    Args:
        path: Path to a local markdown (.md) file to import.
        tags: Comma-separated tags applied to every chunk saved from this file.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        if not path or not isinstance(path, str):
            return "[procedural_memory_import_markdown error: path must be a non-empty string]"
        p = Path(path)
        if not p.exists() or not p.is_file():
            return f"[procedural_memory_import_markdown: file not found: {path}]"
        try:
            text = p.read_text(encoding="utf-8-sig")
        except Exception as e:
            return (
                f"[procedural_memory_import_markdown error: could not read file: "
                f"{type(e).__name__}: {e}]"
            )

        caller_tags = [t.strip() for t in (tags or "").split(",") if t.strip()]
        basename = p.name

        imported = 0
        skipped = 0
        for chunk_intent, body in _split_markdown_chunks(text, basename):
            body_stripped = body.strip()
            if not body_stripped:
                skipped += 1
                continue
            intent = chunk_intent.strip()[:120] or basename
            snippet = _extract_snippet(body_stripped)
            context = _extract_context(body_stripped)
            chunk_tags = _dedup_preserve_order(
                caller_tags + ["import", f"src:{basename}"] + _extract_table_tags(body_stripped)
            )
            procedural_memory_save(
                intent=intent,
                snippet=snippet,
                tags=",".join(chunk_tags),
                context=context,
            )
            imported += 1
        return f"imported {imported} chunks from {basename} (skipped {skipped} empty)"
    except Exception as e:
        return f"[procedural_memory_import_markdown error: {type(e).__name__}: {e}]"

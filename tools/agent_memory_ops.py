"""Agent memory WRITE engine over the human-readable hierarchical JSON store at
agent_memory/ (topics/ + facts/ + sessions/ + index.json -- see
agent_memory/README.md and agent_memory/templates/*.json for the schema this
module implements).

WHY THIS EXISTS: agent_memory/ has existed as a read/restore convention (an
agent reads index.json + a topic file at session start to "remember" context),
but nothing ever WROTE to it programmatically -- topics were hand-edited or
copy-pasted from templates/topic_template.json. This module is the missing
write path, callable at ARBITRARY times (not just "restore my context") so an
agent can carve durable, reusable knowledge the moment it discovers it: an SSH
procedure, a data-reference source, a report format, a fact, a decision.

Distinct from the OTHER memory stores in this repo:
  - memory_ops.py         -- semantic memory: flat key/value facts (a KV store)
  - procedural_memory.py  -- procedural memory: reusable how-to snippets (a
                              flat, slug-keyed dict, single JSON state file)
  - runlog_ops.py         -- episodic memory: ordered event history of a run
  - agent_memory_ops.py (this module) -- STRUCTURED per-topic notebooks: each
    topic is its own JSON file with summary/data_sources/method/key_facts/
    hypotheses/decisions/artifacts/open_questions/next_actions, plus a top
    index.json for fast "what do I remember" search. Human-readable and
    hand-editable by design (that is the whole point of agent_memory/).

Pure stdlib (json, os, pathlib, datetime, tempfile, re) -- no new dependency.
Every public function is best-effort: it must NEVER raise into the MCP server,
always returning a status string instead (same contract as memory_ops.py /
procedural_memory.py).

All writes are ATOMIC: build the full JSON in memory, write it to a temp file
in the SAME directory as the target, then os.replace() it over the target.
os.replace is atomic on both POSIX and Windows (NTFS), so a crash mid-write
never leaves a half-written / corrupt topic or index file. Encoding is plain
UTF-8 with NO byte-order-mark (opening a file with encoding="utf-8" never
writes a BOM in Python; only "utf-8-sig" does, and this module never uses
that mode for writing).

IMPORTANT FOR TESTS: every path this module touches is derived from the
module-level MEM_DIR global, looked up FRESH inside each function (never
cached into a second module-level constant at import time). Monkeypatching
`agent_memory_ops.MEM_DIR` to a temp directory is therefore enough to redirect
every read/write this module performs -- see tools/test_agent_memory_ops.py.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .security import require_unlocked

# Repo root resolved from __file__ (tools/agent_memory_ops.py -> repo root is
# one level up), matching the convention used by memory_ops.py / procedural_memory.py.
REPO_ROOT = Path(__file__).resolve().parent.parent
MEM_DIR = REPO_ROOT / "agent_memory"

_VALID_CONFIDENCE = ("high", "medium", "low")
_VALID_ARTIFACT_TYPES = ("report", "figure", "data", "code", "other")


# ===========================================================================
# path helpers -- all derive from the MODULE GLOBAL MEM_DIR, looked up fresh
# each call (not captured into a second constant), so monkeypatching MEM_DIR
# alone is enough to redirect every path below.
# ===========================================================================


def _topics_dir() -> Path:
    return MEM_DIR / "topics"


def _facts_dir() -> Path:
    return MEM_DIR / "facts"


def _sessions_dir() -> Path:
    return MEM_DIR / "sessions"


def _index_path() -> Path:
    return MEM_DIR / "index.json"


def _safe_id(topic_id: str) -> str:
    """Sanitize topic_id into a filesystem-safe basename.

    topic_id is agent-supplied input, so this defensively strips anything
    that could escape TOPICS_DIR (path separators, '..' traversal) rather
    than trusting callers to already pass a clean snake_case id."""
    cleaned = re.sub(r"[^A-Za-z0-9_\-.]+", "_", (topic_id or "").strip())
    cleaned = cleaned.replace("..", "_")
    return cleaned or "untitled"


def _topic_path(topic_id: str) -> Path:
    return _topics_dir() / f"{_safe_id(topic_id)}.json"


def _ensure_dirs() -> None:
    for d in (MEM_DIR, _topics_dir(), _facts_dir(), _sessions_dir()):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ===========================================================================
# atomic read/write
# ===========================================================================


def _read_json(path: Path, default):
    """Tolerant JSON read: missing file, unreadable file, or malformed JSON
    all fall back to `default` instead of raising."""
    try:
        if not path.exists() or not path.is_file():
            return default
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data
    except Exception:
        return default


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write `data` as pretty JSON to `path` atomically: tmp file in the same
    directory + os.replace(). UTF-8, no BOM."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".tmp_{path.stem}_", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.remove(tmp_name)
        except Exception:
            pass
        raise


# ===========================================================================
# small helpers: normalization + de-dup
# ===========================================================================


def _as_str_list(value) -> list[str]:
    """Accept a list/tuple, a comma-separated string, or None; return a clean
    list[str] (empty strings dropped)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _merge_dedup(existing: list[str], new_items: list[str]) -> list[str]:
    out = list(existing)
    seen = set(out)
    for item in new_items:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _append_dict_unique(lst: list, entry: dict, dedup_field: str) -> bool:
    """Append `entry` to `lst` unless an existing item already has the same
    value for `dedup_field` (the field that identifies this kind of record --
    e.g. "fact" for key_facts, "path" for data_sources/artifacts, "decision"
    for decisions). Returns True iff appended."""
    target = entry.get(dedup_field)
    for item in lst:
        if isinstance(item, dict) and item.get(dedup_field) == target:
            return False
    lst.append(entry)
    return True


def _append_str_unique(lst: list, value: str) -> bool:
    if value in lst:
        return False
    lst.append(value)
    return True


# ===========================================================================
# topic template + index defaults
# ===========================================================================


def _new_topic(topic_id: str, title: Optional[str] = None) -> dict:
    """A fresh topic matching agent_memory/templates/topic_template.json's
    schema exactly, minus the "_template"/"_note" scaffolding keys and with
    empty arrays (the template ships one placeholder example entry per array
    for a human to look at; a programmatically created topic starts clean)."""
    now = _now_iso()
    return {
        "topic_id": topic_id,
        "title": title or topic_id,
        "status": "active",
        "created": now,
        "updated": now,
        "tags": [],
        "keywords": [],
        "summary": "",
        "data_sources": [],
        "method": [],
        "key_facts": [],
        "hypotheses": [],
        "decisions": [],
        "artifacts": [],
        "open_questions": [],
        "next_actions": [],
        "related_topics": [],
    }


def _load_topic(topic_id: str) -> Optional[dict]:
    topic = _read_json(_topic_path(topic_id), None)
    return topic if isinstance(topic, dict) else None


def _load_index() -> dict:
    idx = _read_json(_index_path(), None)
    if not isinstance(idx, dict):
        idx = {}
    idx.setdefault("version", "1.0")
    idx.setdefault("last_updated", "")
    # NEVER invent an owner -- this field may hold a real email in the live
    # store; a freshly created index.json leaves it blank rather than guessing.
    idx.setdefault("owner", "")
    idx.setdefault(
        "description",
        "Agent pseudo-memory top index. Read this first at session/task start.",
    )
    idx.setdefault("facts", [])
    idx.setdefault("topics", [])
    idx.setdefault("boot_protocol", [])
    idx.setdefault("shutdown_protocol", [])
    if not isinstance(idx.get("topics"), list):
        idx["topics"] = []
    if not isinstance(idx.get("facts"), list):
        idx["facts"] = []
    return idx


def _upsert_index(topic: dict) -> None:
    idx = _load_index()
    tid = topic.get("topic_id", "")
    one_liner = (topic.get("summary") or topic.get("title") or "").replace("\n", " ").strip()
    if len(one_liner) > 160:
        one_liner = one_liner[:157] + "..."
    entry = {
        "id": tid,
        "title": topic.get("title", tid),
        "status": topic.get("status", "active"),
        "tags": list(topic.get("tags", []) or []),
        "keywords": list(topic.get("keywords", []) or []),
        "last_active": topic.get("updated", _now_iso()),
        "file": f"topics/{_safe_id(tid)}.json",
        "one_liner": one_liner,
    }
    topics = idx.get("topics", [])
    for i, t in enumerate(topics):
        if isinstance(t, dict) and t.get("id") == tid:
            topics[i] = entry
            break
    else:
        topics.append(entry)
    idx["topics"] = topics
    idx["last_updated"] = _now_iso()
    _atomic_write_json(_index_path(), idx)


# ===========================================================================
# public API
# ===========================================================================


def memory_save(
    topic_id: str,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    tags=None,
    keywords=None,
    key_fact: Optional[str] = None,
    confidence: str = "medium",
    data_source: Optional[str] = None,
    source_note: Optional[str] = None,
    method_step: Optional[str] = None,
    decision: Optional[str] = None,
    rationale: Optional[str] = None,
    artifact: Optional[str] = None,
    artifact_type: str = "other",
    artifact_note: Optional[str] = None,
    next_action: Optional[str] = None,
    open_question: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    """Carve a piece of durable, reusable knowledge into agent_memory/topics/<topic_id>.json.

    Loads the topic (creating it from the standard template if it does not
    exist yet), appends whichever of the optional pieces were passed to the
    matching array/field, merges tags/keywords (de-duplicated), refreshes
    summary/status/updated, then UPSERTs the topic's summary row into
    agent_memory/index.json. Every write is atomic. Re-saving an identical
    key_fact/data_source/method_step/decision/artifact/next_action/
    open_question that is already present is a no-op for that piece (no
    duplicate rows pile up in the topic file).

    Only topic_id is required -- pass just the pieces relevant to this call
    (e.g. only key_fact + confidence to log one fact; only decision +
    rationale to log one decision).

    Args:
        topic_id: Stable snake_case id for this topic/project/theme (also the
            filename: topics/<topic_id>.json). Reused across calls to keep
            building the same notebook.
        title: Human-readable title (set/overwritten if provided).
        summary: 2-4 line summary shown first on session restore (overwritten
            if provided -- callers should pass the FULL updated summary, not a
            delta).
        tags: List (or comma-separated string) of tags; merged into existing.
        keywords: List (or comma-separated string) of keywords; merged into existing.
        key_fact: A single durable fact to append to key_facts[].
        confidence: "high" | "medium" | "low" for key_fact (default "medium").
        data_source: A path/URI to append to data_sources[] (e.g. a DB view,
            a file path, an API endpoint).
        source_note: Optional note paired with data_source.
        method_step: A single step to append to method[] (an ordered-ish list
            of how this topic's work gets done).
        decision: A decision to append to decisions[].
        rationale: Optional rationale paired with decision.
        artifact: A path to append to artifacts[] (a produced report/figure/
            data file/code file).
        artifact_type: "report" | "figure" | "data" | "code" | "other" (default "other").
        artifact_note: Optional note paired with artifact.
        next_action: A single action to append to next_actions[].
        open_question: A single open question to append to open_questions[].
        status: "active" | "paused" | "done" | "archived" (overwrites current status).
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        if not topic_id or not isinstance(topic_id, str):
            return "[memory_save error: topic_id must be a non-empty string]"

        _ensure_dirs()
        topic = _load_topic(topic_id)
        created_new = topic is None
        if topic is None:
            topic = _new_topic(topic_id, title=title)

        changed: list[str] = []

        if title:
            if topic.get("title") != title:
                topic["title"] = title
                changed.append("title")
        if summary:
            if topic.get("summary") != summary:
                topic["summary"] = summary
                changed.append("summary")
        if status:
            if topic.get("status") != status:
                topic["status"] = status
                changed.append("status")

        tag_list = _as_str_list(tags)
        if tag_list:
            before = list(topic.get("tags", []) or [])
            topic["tags"] = _merge_dedup(before, tag_list)
            if topic["tags"] != before:
                changed.append("tags")

        kw_list = _as_str_list(keywords)
        if kw_list:
            before = list(topic.get("keywords", []) or [])
            topic["keywords"] = _merge_dedup(before, kw_list)
            if topic["keywords"] != before:
                changed.append("keywords")

        if key_fact:
            conf = confidence if confidence in _VALID_CONFIDENCE else "medium"
            entry = {"fact": key_fact, "confidence": conf}
            if _append_dict_unique(topic.setdefault("key_facts", []), entry, "fact"):
                changed.append("key_fact")

        if data_source:
            entry = {"path": data_source, "note": source_note or ""}
            if _append_dict_unique(topic.setdefault("data_sources", []), entry, "path"):
                changed.append("data_source")

        if method_step:
            if _append_str_unique(topic.setdefault("method", []), method_step):
                changed.append("method_step")

        if decision:
            entry = {"decision": decision, "rationale": rationale or "", "when": _now_iso()}
            if _append_dict_unique(topic.setdefault("decisions", []), entry, "decision"):
                changed.append("decision")

        if artifact:
            a_type = artifact_type if artifact_type in _VALID_ARTIFACT_TYPES else "other"
            entry = {"path": artifact, "type": a_type, "note": artifact_note or ""}
            if _append_dict_unique(topic.setdefault("artifacts", []), entry, "path"):
                changed.append("artifact")

        if next_action:
            if _append_str_unique(topic.setdefault("next_actions", []), next_action):
                changed.append("next_action")

        if open_question:
            if _append_str_unique(topic.setdefault("open_questions", []), open_question):
                changed.append("open_question")

        topic["updated"] = _now_iso()
        _atomic_write_json(_topic_path(topic_id), topic)
        _upsert_index(topic)

        verb = "created" if created_new else "updated"
        if not changed:
            return (
                f"memory_save: {verb} topic '{topic_id}' "
                f"(index refreshed; no new content -- everything given was already present)"
            )
        return f"memory_save: {verb} topic '{topic_id}' -- appended/updated: {', '.join(changed)}"
    except Exception as e:
        return f"[memory_save error: {type(e).__name__}: {e}]"


def _topic_haystack(topic: dict) -> str:
    fields = [
        topic.get("title", "") or "",
        topic.get("summary", "") or "",
        " ".join(topic.get("tags", []) or []),
        " ".join(topic.get("keywords", []) or []),
    ]
    fields += [f.get("fact", "") for f in (topic.get("key_facts") or []) if isinstance(f, dict)]
    fields += [
        f"{d.get('path', '')} {d.get('note', '')}"
        for d in (topic.get("data_sources") or [])
        if isinstance(d, dict)
    ]
    return " ".join(fields).lower()


def _topic_snippet(topic: dict) -> str:
    snippet = topic.get("summary") or topic.get("title") or ""
    snippet = snippet.replace("\n", " ").strip()
    return snippet[:200]


def memory_search(query: str, limit: int = 10) -> str:
    """Search agent_memory/ for topics (and standalone facts) matching `query`.

    Read-only. Scans index.json + every topics/*.json (matching against
    title/tags/keywords/summary/one_liner AND each topic's key_facts/
    data_sources text) plus every facts/*.json. Ranks by number of distinct
    query tokens matched. Call this BEFORE re-deriving/re-discovering
    something that might already be recorded -- cheaper than re-solving.

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

        _ensure_dirs()
        results: list[tuple[int, str, str, str, str]] = []  # score, last_active, kind, id, snippet

        topic_ids: set[str] = set()
        idx = _load_index()
        for t in idx.get("topics", []):
            if isinstance(t, dict) and t.get("id"):
                topic_ids.add(t["id"])
        topics_dir = _topics_dir()
        if topics_dir.exists():
            for p in topics_dir.glob("*.json"):
                topic_ids.add(p.stem)

        for tid in sorted(topic_ids):
            topic = _load_topic(tid)
            if not topic:
                continue
            haystack = _topic_haystack(topic)
            score = sum(1 for tok in tokens if tok in haystack)
            if score <= 0:
                continue
            results.append((score, topic.get("updated", "") or "", "topic", tid, _topic_snippet(topic)))

        facts_dir = _facts_dir()
        if facts_dir.exists():
            for p in facts_dir.glob("*.json"):
                data = _read_json(p, None)
                if data is None:
                    continue
                try:
                    text = json.dumps(data, ensure_ascii=False)
                except Exception:
                    text = str(data)
                score = sum(1 for tok in tokens if tok in text.lower())
                if score <= 0:
                    continue
                snippet = text.replace("\n", " ").strip()[:200]
                results.append((score, "", "fact", p.stem, snippet))

        if not results:
            return "(no matches)"
        results.sort(key=lambda r: (r[0], r[1]), reverse=True)
        results = results[: max(0, limit)]
        lines = [f"{len(results)} match(es) for {query!r}:"]
        for score, _ts, kind, rid, snippet in results:
            lines.append(f"- [{kind}:{rid}] score={score}\n    {snippet}")
        return "\n".join(lines)
    except Exception as e:
        return f"[memory_search error: {type(e).__name__}: {e}]"


def memory_read(topic_id: str) -> str:
    """Return the full topic JSON for `topic_id` (pretty-printed), or a
    not-found message. Read-only.

    Args:
        topic_id: The topic's id (same string passed to memory_save).
    """
    try:
        if not topic_id or not isinstance(topic_id, str):
            return "[memory_read error: topic_id must be a non-empty string]"
        topic = _load_topic(topic_id)
        if topic is None:
            return f"[memory_read: no topic found at '{topic_id}']"
        return json.dumps(topic, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"[memory_read error: {type(e).__name__}: {e}]"


def memory_list() -> str:
    """List every topic in agent_memory/index.json (id, status, one_liner,
    last_active) -- the "what do I remember" overview. Read-only.

    Falls back to scanning agent_memory/topics/*.json directly if index.json
    is missing/empty (defensive: the index should always be kept in sync by
    memory_save, but a hand-edited or partially-migrated store should still
    be usable)."""
    try:
        _ensure_dirs()
        idx = _load_index()
        topics = idx.get("topics", [])
        if isinstance(topics, list) and topics:
            lines = [f"{len(topics)} topic(s):"]
            for t in topics:
                if not isinstance(t, dict):
                    continue
                lines.append(
                    f"- {t.get('id', '?')}  [{t.get('status', '?')}]  "
                    f"last_active={t.get('last_active', '?')}  {t.get('one_liner', '')}"
                )
            return "\n".join(lines)

        # index empty/missing -> fall back to scanning topics/ directly
        topics_dir = _topics_dir()
        ids = sorted(p.stem for p in topics_dir.glob("*.json")) if topics_dir.exists() else []
        if not ids:
            return "(no topics in memory yet)"
        lines = [f"{len(ids)} topic(s) (index.json empty/missing, listing topics/ directly):"]
        for tid in ids:
            t = _load_topic(tid) or {}
            lines.append(
                f"- {tid}  [{t.get('status', '?')}]  "
                f"{(t.get('summary') or t.get('title') or '')[:160]}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"[memory_list error: {type(e).__name__}: {e}]"


# ===========================================================================
# MCP-tool-facing wrappers (unique names -- see main.py registration comment):
# tools/memory_ops.py ALREADY exports module-level names memory_save/
# memory_list, so this module's memory_save/memory_search/memory_read/
# memory_list (the plain functions above -- this module's own public API,
# matching the schema/spec 1:1) cannot ALSO be registered under those same
# names on the MCP server (name collision in main.py's _ALL_TOOLS dict, keyed
# by function __name__). These thin wrappers give the MCP-registered tools
# distinct names while keeping the underlying implementation identical.
# ===========================================================================


def agent_memory_save(
    topic_id: str,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    tags=None,
    keywords=None,
    key_fact: Optional[str] = None,
    confidence: str = "medium",
    data_source: Optional[str] = None,
    source_note: Optional[str] = None,
    method_step: Optional[str] = None,
    decision: Optional[str] = None,
    rationale: Optional[str] = None,
    artifact: Optional[str] = None,
    artifact_type: str = "other",
    artifact_note: Optional[str] = None,
    next_action: Optional[str] = None,
    open_question: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    """再利用可能な事実・手順・参照元・レポート形式・決定を agent_memory/ に刻む。
    ユーザーが「刻んで」「覚えておいて」と言ったとき、またはSSH手順・データ参照元・
    レポートフォーマット・その他の再利用可能な知識が判明したときに、会話の任意の
    タイミングで呼ぶ（セッション終了時だけではない）。

    Carve durable, reusable knowledge into the agent_memory/ topic store. Call
    this whenever the user says "remember this" / "刻んで", or whenever a
    reusable fact, procedure (e.g. an SSH sequence), data-reference source, or
    report format is discovered -- at ANY point in the conversation, not just
    context-restore/shutdown. topic_id is the only required argument; pass
    just the piece(s) relevant to this call (e.g. only key_fact to log one
    fact). See memory_search / memory_read / memory_list to recall what is
    already stored before re-deriving something from scratch. Full parameter
    docs: tools.agent_memory_ops.memory_save.
    """
    # Literal require_unlocked() call here (redundant with the one inside
    # memory_save) so tool_annotations.py's mechanical readOnlyHint derivation
    # -- which greps THIS function's own source -- correctly marks this
    # wrapper as gated (same pattern as semantic_memory_save in memory_ops.py).
    locked = require_unlocked()
    if locked:
        return locked
    return memory_save(
        topic_id,
        title=title,
        summary=summary,
        tags=tags,
        keywords=keywords,
        key_fact=key_fact,
        confidence=confidence,
        data_source=data_source,
        source_note=source_note,
        method_step=method_step,
        decision=decision,
        rationale=rationale,
        artifact=artifact,
        artifact_type=artifact_type,
        artifact_note=artifact_note,
        next_action=next_action,
        open_question=open_question,
        status=status,
    )


def agent_memory_search(query: str, limit: int = 10) -> str:
    """agent_memory/ の中から話題・事実を検索する（読み取り専用）。
    「前に調べた〜って覚えてる？」のような再利用可能知識の有無確認に使う。

    Search agent_memory/ (topics + facts) for anything matching `query`.
    Read-only -- call this before re-deriving something that might already be
    recorded. See tools.agent_memory_ops.memory_search for full docs.
    """
    return memory_search(query, limit=limit)


def agent_memory_read(topic_id: str) -> str:
    """agent_memory/ の指定トピックの全内容を読む（読み取り専用）。

    Return the full stored topic JSON for `topic_id`. Read-only. See
    tools.agent_memory_ops.memory_read for full docs.
    """
    return memory_read(topic_id)


def agent_memory_list() -> str:
    """agent_memory/ に何が記録されているか一覧表示する（読み取り専用）。
    セッション開始時の「何を覚えているか」の全体像を得るのに使う。

    List every topic currently in agent_memory/index.json -- the "what do I
    remember" overview. Read-only. See tools.agent_memory_ops.memory_list for
    full docs.
    """
    return memory_list()

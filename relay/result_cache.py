# -*- coding: utf-8 -*-
"""Read-only tool result caching, SHADOW ONLY: it records the time it would have saved.

WHERE IT SITS. Every tool the model calls on the connector-less route goes through one
chokepoint (relay/tool_bridge.py:run_tool). A read-only tool called twice with the same
arguments returns the same answer both times; the second call spends its latency for nothing.
This module watches that chokepoint and, for a read-only call whose (tool, arguments) it has
seen answer before, records that a cache would have returned the earlier answer -- how long
that saved, and whether the fresh answer actually matched the one a cache would have served.
It writes that beside the call. Nothing else.

WHAT THIS MODULE IS NOT. It never returns a cached answer, never skips a call, never edits a
prompt and never changes an output: every tool still runs for real and the model sees the
real, fresh result. The setting `result_cache` accepts `off` and `shadow` and nothing else;
there is no value that serves from the cache by mistake. A cache that actually served would
be a later change with its own review, once the report says the served answers would have
matched often enough. In `off` nothing here reads a file or records.

THE RULE IT RECORDS (kept small on purpose, and stated so a reader can disagree with it).
  * Only read-only calls are considered: a tool the caller marks read_only=True. A write or
    an unknown tool is never a cache candidate (its repeat is not safe to serve).
  * The key is the tool name and its arguments, canonicalised (JSON, sorted keys). Two calls
    with the same key are the same question.
  * The FIRST time a key is seen is a miss: there was nothing to serve. A later call with a
    live entry (seen within TTL_S) is a hit; its saved time is the fresh call's own duration.
  * A hit is `matched` when the fresh result equals the stored one, `differed` otherwise. A
    differed hit is the case a real cache would have got wrong, so it is counted apart.

HONESTY RULES. A write error is counted and swallowed: the recorder runs inside the tool
chokepoint and must never raise into it. The file is bounded (one rotated copy), append-only.
A row holds the tool name, a hash of the arguments and numbers only -- never argument values
and never result content.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import time

KEY = "result_cache"
#: ONLY these. There is deliberately no value that serves from the cache (see the docstring).
MODES = ("off", "shadow")

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   ".fleet", "result_cache_shadow.jsonl")
#: The log is rotated to `<LOG>.1` (replacing the old copy) when it reaches this size, so the
#: records on disk never exceed about twice this.
MAX_BYTES = 1024 * 1024
#: How long a remembered answer stays a cache candidate. A repeat after this is a fresh miss,
#: the same as a real cache whose entry has expired.
TTL_S = 600.0
#: Keys remembered at once (least-recently-seen dropped). Bounds the in-memory table.
MAX_ENTRIES = 4096
SCHEMA = 1

_STATE = {"seen": 0, "hits": 0, "matched": 0, "differed": 0, "write_errors": 0}
#: key -> {"at": seen_ts, "digest": result_digest}. Insertion-ordered for the LRU drop.
_TABLE = {}
_settings_cache = {}


# ------------------------------------------------------------------ what is safe to repeat

#: Inner tool names a repeat is safe to serve from a cache: they read and do not change state.
#: Kept as an explicit allow-list on purpose -- an unlisted or write tool is never a candidate,
#: so a new tool is a miss until someone adds it here with that judgement in mind.
READ_ONLY_TOOLS = frozenset({
    "read_file", "list_directory", "read_excel", "file_metadata", "env_info",
    "get_job_status", "read_image", "memory_load", "list_my_tools",
})


def is_read_only(tool):
    """True when a repeat of this inner tool with the same arguments is safe to serve from a
    cache. Only names on the explicit allow-list qualify; everything else (writes, unknown
    tools, discovery) is treated as not cacheable."""
    return str(tool or "") in READ_ONLY_TOOLS


# ------------------------------------------------------------------ the setting

def mode(settings_path=None):
    """`result_cache=` from settings.txt: "shadow" or "off". Anything else reads as off.
    Re-read when the file changes (each_gate). Never raises."""
    try:
        path = settings_path
        if path is None:
            from tools.settings_path import settings_file
            path = settings_file()
        if not path:
            return "off"
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        hit = _settings_cache.get(path)
        if hit is not None and hit[0] == key:
            return hit[1]
        val = "off"
        with io.open(path, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith(KEY + "="):
                    v = ln.split("=", 1)[1].strip().lower()
                    val = v if v in MODES else "off"
                    break
        _settings_cache[path] = (key, val)
        return val
    except Exception:
        return "off"


# ------------------------------------------------------------------ the key and the digest

def cache_key(tool, arguments):
    """The canonical (tool, arguments) identity: the tool name and a stable hash of the
    arguments. Two calls that would ask a cache the same question share this."""
    try:
        blob = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        blob = repr(arguments)
    h = hashlib.sha256(("%s\x00%s" % (tool, blob)).encode("utf-8", "replace")).hexdigest()
    return (str(tool or ""), h[:16])


def _digest(result):
    """A content hash of a result, so a hit can be called matched or differed without the row
    ever holding the result itself."""
    try:
        blob = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        blob = repr(result)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


# ------------------------------------------------------------------ the decision (pure)

def decide(entry, now, ttl_s=TTL_S):
    """Given the table entry for a key (or None) and the current time, say whether a cache
    would have had a live answer to serve. Returns {"hit", "age"}; hit is False on a first
    sighting or an entry past its TTL."""
    if not entry:
        return {"hit": False, "age": None}
    age = now - entry.get("at", now)
    if age < 0 or age >= ttl_s:
        return {"hit": False, "age": age}
    return {"hit": True, "age": age}


# ------------------------------------------------------------------ the recorder

def _append(row):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        try:
            if os.path.getsize(LOG) >= MAX_BYTES:
                os.replace(LOG, LOG + ".1")
        except OSError:
            pass
        with io.open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception:
        _STATE["write_errors"] += 1
        return False


def _remember(key, digest, now):
    """Record that this key was just answered, for the next call to compare against. Drops the
    oldest entry when the table is full."""
    _TABLE.pop(key, None)
    _TABLE[key] = {"at": now, "digest": digest}
    while len(_TABLE) > MAX_ENTRIES:
        oldest = next(iter(_TABLE))
        _TABLE.pop(oldest, None)


def shadow_observe(tool, arguments, result, read_only, duration_s=None, run_id="",
                   settings_path=None, now=None):
    """Called by the tool chokepoint once per completed call, after the real result is in hand
    and before it is returned to the model. In `shadow` it compares this call against the last
    answer for the same (tool, arguments) and appends one row when the tool is read-only. In
    `off`, or for a write/unknown tool, it returns at once. Never raises, never mutates its
    arguments, returns nothing."""
    try:
        if mode(settings_path) != "shadow":
            return None
        if not read_only:
            return None
        t = time.time() if now is None else now
        key = cache_key(tool, arguments)
        entry = _TABLE.get(key)
        verdict = decide(entry, t)
        digest = _digest(result)
        hit = verdict["hit"]
        matched = bool(hit and entry and entry.get("digest") == digest)
        differed = bool(hit and not matched)
        row = {
            "v": SCHEMA, "ts": round(t, 1), "run_id": str(run_id or ""), "mode": "shadow",
            "tool": str(tool or ""), "arg_hash": key[1], "read_only": True,
            "hit": hit, "matched": matched, "differed": differed,
            "age_s": (round(verdict["age"], 1) if verdict["age"] is not None else None),
            "saved_s": (round(float(duration_s), 3)
                        if hit and duration_s is not None else (0.0 if hit else None)),
        }
        if _append(row):
            _STATE["seen"] += 1
            if hit:
                _STATE["hits"] += 1
                _STATE["matched"] += 1 if matched else 0
                _STATE["differed"] += 1 if differed else 0
        _remember(key, digest, t)
        return None
    except Exception:
        return None


def status_block(settings_path=None):
    """{"result_cache": {mode, seen, hits, differed}} for status.json (additive)."""
    return {KEY: {"mode": mode(settings_path), "seen": int(_STATE["seen"]),
                  "hits": int(_STATE["hits"]), "differed": int(_STATE["differed"])}}


def _reset_for_test():
    _STATE.update({"seen": 0, "hits": 0, "matched": 0, "differed": 0, "write_errors": 0})
    _TABLE.clear()
    _settings_cache.clear()

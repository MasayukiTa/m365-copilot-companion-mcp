"""Alias/synonym store for widening DB-discovery search queries.

Dept-local terms and abbreviations mean the same real-world thing gets asked
about in many different words (e.g. one team says "原料", another says "投入"
or "配合" for the same concept). tools/data_discovery.py's find_db_objects
scores procedural-memory matches by literal keyword overlap, so a question
phrased in one team's vocabulary can silently miss memory saved under a
colleague's synonym for the same thing. This module is a tiny, local,
gitignored {term: [synonym, ...]} store that widens the keyword list before
the search runs.

Store file: .procedural_memory_aliases.json at the repo root -- same locate
pattern as tools/procedural_memory.py's STATE_FILE (anchored off
Path(__file__), never cwd-relative). Never shipped pre-filled: it would
otherwise have to contain real department vocabulary, so it is created only
on first write (data_aliases_add) and stays out of git (see .gitignore).
A missing or corrupt file is always treated as {} -- never raises.

Schema: a flat JSON object, {"term": ["synonym1", "synonym2", ...], ...}.
Storage is a one-directional adjacency list, but USE (expand_terms) treats
every key/synonym pair as bidirectional and transitively connected: if "A"
maps to ["B", "C"], then A, B, and C are all synonyms of one another, so a
query containing any one of them pulls in the other two.

Gating: data_aliases_add is a write, so it goes through require_unlocked()
like every other mutating tool in this repo. expand_terms and
data_aliases_list are read-only and ungated on purpose -- expand_terms in
particular is called from inside find_db_objects, which is itself an ungated
gateway tool (see tools/data_discovery.py and tools/data_memory_hook.py's
docstring for the same not-yet-unlocked-remote-client reasoning), so gating
expand_terms would silently break that caller for locked clients.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .security import require_unlocked

STATE_FILE = Path(__file__).resolve().parent.parent / ".procedural_memory_aliases.json"

_EXPAND_CAP_DEFAULT = 16


def _load_aliases() -> dict:
    """Load the alias store as {str: [str, ...]}. Missing file, corrupt JSON,
    a non-dict top level, or a non-list value under some key are all
    tolerated by falling back to {} (or dropping just the bad key) -- never
    raises."""
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        clean: dict = {}
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, list):
                clean[k] = [s for s in v if isinstance(s, str)]
        return clean
    except Exception:
        return {}


def _save_aliases(data: dict) -> None:
    """Atomic write (tmp file + os.replace, same pattern as tools/auth_stats.py
    and tools/contract_gate.py) so a crash mid-write never corrupts the store.
    utf-8 + ensure_ascii=False so Japanese terms round-trip as literal
    characters, not \\uXXXX escapes."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(STATE_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(STATE_FILE))


def _connected_group(term: str, data: dict) -> list[str]:
    """Every term transitively connected to `term` via the adjacency store
    (key<->values, value<->siblings), NOT including `term` itself.
    Order-preserving breadth-first walk over dict-iteration order (Python
    dicts preserve insertion order), so results are deterministic given a
    fixed store -- not sorted, but stable and reproducible."""
    seen = {term}
    group: list[str] = []
    frontier = [term]
    while frontier:
        next_frontier: list[str] = []
        for node in frontier:
            for key, synonyms in data.items():
                members = [key] + list(synonyms)
                if node not in members:
                    continue
                for m in members:
                    if m not in seen:
                        seen.add(m)
                        group.append(m)
                        next_frontier.append(m)
        frontier = next_frontier
    return group


def expand_terms(terms: list) -> list:
    """Widen a list of search terms with any known synonyms, bidirectionally
    and transitively (see module docstring). Returns the ORIGINAL terms
    first, in their original order, followed by newly-added synonyms
    (order-preserving, deduped against the originals and each other).

    Never raises: garbage input (not a list, or a list with non-string
    entries) is returned as-received rather than raising -- callers (e.g.
    find_db_objects) are expected to wrap this in their own try/except too,
    but this function does not depend on that.
    """
    try:
        if not isinstance(terms, list):
            return terms
        out: list[str] = []
        seen: set = set()
        for t in terms:
            if isinstance(t, str) and t not in seen:
                seen.add(t)
                out.append(t)
        if not out:
            return out
        data = _load_aliases()
        if not data:
            return out
        for t in list(out):  # snapshot: only expand from the ORIGINAL terms
            try:
                for syn in _connected_group(t, data):
                    if syn not in seen:
                        seen.add(syn)
                        out.append(syn)
            except Exception:
                continue
        return out
    except Exception:
        return terms


def data_aliases_add(term: str, synonyms: str) -> str:
    """Register synonyms for `term` in the local alias store (merged with any
    existing entry, deduped, atomic write). Mutating -- requires unlock.

    Use this to teach find_db_objects that a dept-local word/abbreviation
    means the same thing as another word already used elsewhere in
    procedural memory, so future questions phrased either way match.

    Args:
        term: The term to attach synonyms to (e.g. a dept-local abbreviation
            or a term already seen in procedural memory).
        synonyms: Comma-separated synonym(s) to merge in, e.g. "投入,配合".
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        if not term or not isinstance(term, str):
            return "[data_aliases_add error: term must be a non-empty string]"
        if not isinstance(synonyms, str) or not synonyms.strip():
            return "[data_aliases_add error: synonyms must be a non-empty comma-separated string]"
        syn_list = [s.strip() for s in synonyms.split(",") if s.strip()]
        if not syn_list:
            return "[data_aliases_add error: no usable synonyms found in input]"

        data = _load_aliases()
        merged = list(data.get(term, []))
        for s in syn_list:
            if s != term and s not in merged:
                merged.append(s)
        data[term] = merged
        _save_aliases(data)
        return f"aliases[{term}] -> {len(merged)} synonym(s): {', '.join(merged)}"
    except Exception as e:
        return f"[data_aliases_add error: {type(e).__name__}: {e}]"


def data_aliases_list(term: str = "") -> str:
    """Read-only listing of the local alias store; no unlock needed.

    Args:
        term: If given, show just that term's synonyms. If omitted, list
            every stored term.
    """
    try:
        data = _load_aliases()
        if not data:
            return "no aliases yet; add with data_aliases_add(term, synonyms)"
        if term:
            syns = data.get(term)
            if not syns:
                return f"no aliases found for {term!r}"
            return f"{term}: {', '.join(syns)}"
        lines = [f"{len(data)} term(s):"]
        for k, v in data.items():
            lines.append(f"  - {k}: {', '.join(v)}")
        return "\n".join(lines)
    except Exception as e:
        return f"[data_aliases_list error: {type(e).__name__}: {e}]"

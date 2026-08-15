"""Genome archive for the open-ended self-improvement loop (L4 design, section 2 + 4).

Moves from "current scaffold + linear keep/revert" to a *versioned archive of scaffold genomes*, so
improvement can branch and build on the best ancestor rather than a single mutable HEAD (the
Darwin-Goedel / SICA insight: a linear chain gets stuck; an archive lets a later iteration revive a
dormant-but-promising branch).

  1. genome / genome_id  -- a serialisable scaffold diff over the frozen base + a stable short hash
  2. Archive             -- append-only jsonl ledger of validated genomes (file-backed, reloadable)
  3. descriptors /
     cell_key            -- coarse behaviour descriptors for quality-diversity (MAP-Elites cells)
  4. qd_map /
     select_parent       -- the MAP-Elites elite map + parent-selection strategies (best | qd)

stdlib only; deterministic ids (no random, no time in the hash). This is the *advisory* half of the
guard split (section 7) -- descriptors and selection are refinable; nothing here enforces honesty.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Iterable

# --------------------------------------------------------------------------------------------------
# 1. Genome representation + stable id
# --------------------------------------------------------------------------------------------------

# A genome is a plain dict:
#   {"knobs": {ENV_VAR: "0"/"1"/...}, "cards": {name: text}, "parent_id": str|None, "note": str}
# The id is a function of knobs+cards ONLY (the heritable content), so the same scaffold always hashes
# to the same id regardless of parent lineage or human note.


def _canonical(genome: dict) -> str:
    """Canonical JSON of the heritable content, with sorted keys.

    TWO SCHEMAS LIVE HERE. This archive predates the typed manifest and hashed only knobs and
    cards; the controller writes components and parameters. Neither of those keys existed
    here, so EVERY controller candidate hashed to the same id -- one archive row's worth of
    identity for an entire campaign, silently. Both shapes are hashed now, and a genome that
    carries neither is still distinguishable from one that carries something, because the
    empty dicts differ from populated ones.
    """
    content = {
        "knobs": dict(genome.get("knobs") or {}),
        "cards": dict(genome.get("cards") or {}),
    }
    # Added only when present, so a knobs/cards genome hashes exactly as it always did and
    # rows already on disk keep their ids. Rewriting every historical id to fix a new bug
    # would break the joins the archive exists to support.
    for key in ("components", "parameters"):
        if genome.get(key):
            content[key] = dict(genome[key])
    return json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def genome_id(genome: dict) -> str:
    """Short stable hash of a genome's knobs+cards: first 12 hex chars of sha1(canonical json).

    Deterministic -- same knobs+cards always yield the same id (no random, no time). parent_id and
    note do NOT affect the id, so an identical scaffold reached by two lineages collides on purpose.
    """
    digest = hashlib.sha1(_canonical(genome).encode("utf-8")).hexdigest()
    return digest[:12]


# --------------------------------------------------------------------------------------------------
# 3. Behaviour descriptors for quality-diversity (declared before Archive so qd_map can use them)
# --------------------------------------------------------------------------------------------------

# Coarse, fixed thresholds -- a deliberate design stub (section 9: "QD descriptors for coding scaffold
# behaviour are a design bet"). Kept simple and deterministic; refine the bins later without changing
# the cell_key contract.
#
#   diff_bin   by avg diff_size (lines touched):   <30 -> "surgical", <200 -> "medium", else "broad"
#   turns_bin  by avg turns used:                  <8  -> "short",    <20  -> "mid",    else "long"
#   dominant_miss = the single most common miss_class across the records (ties -> stable alphabetical)

_MISS_CLASSES = ("precision", "underfit", "regression", "wrong_layer", "other")

_DIFF_THRESHOLDS = ((30, "surgical"), (200, "medium"))   # else "broad"
_TURNS_THRESHOLDS = ((8, "short"), (20, "mid"))          # else "long"


def _bin(value: float, thresholds, default: str) -> str:
    for limit, label in thresholds:
        if value < limit:
            return label
    return default


def descriptors(records: Iterable[dict]) -> dict:
    """Coarse behaviour descriptors for a genome from its per-instance records.

    Each record: {"diff_size": int, "turns": int, "miss_class": str} (miss_class in _MISS_CLASSES).
    Returns {"diff_bin", "turns_bin", "dominant_miss"}. Empty input -> a stable "empty" cell so a
    genome with no records still lands somewhere addressable.
    """
    recs = list(records)
    if not recs:
        return {"diff_bin": "empty", "turns_bin": "empty", "dominant_miss": "none"}

    n = len(recs)
    avg_diff = sum(int(r.get("diff_size", 0)) for r in recs) / n
    avg_turns = sum(int(r.get("turns", 0)) for r in recs) / n

    counts: dict[str, int] = {}
    for r in recs:
        mc = r.get("miss_class") or "other"
        if mc not in _MISS_CLASSES:
            mc = "other"
        counts[mc] = counts.get(mc, 0) + 1
    # most common; ties broken by alphabetical class name for determinism
    dominant = min(counts, key=lambda k: (-counts[k], k))

    return {
        "diff_bin": _bin(avg_diff, _DIFF_THRESHOLDS, "broad"),
        "turns_bin": _bin(avg_turns, _TURNS_THRESHOLDS, "long"),
        "dominant_miss": dominant,
    }


def cell_key(desc: dict) -> str:
    """Stable string key for a MAP-Elites cell from a descriptors dict."""
    return "%s|%s|%s" % (
        desc.get("diff_bin", "empty"),
        desc.get("turns_bin", "empty"),
        desc.get("dominant_miss", "none"),
    )


# --------------------------------------------------------------------------------------------------
# 2. The archive (append-only jsonl ledger)
# --------------------------------------------------------------------------------------------------

_DEFAULT_ARCHIVE = os.path.join(os.path.dirname(__file__), "archive", "entries.jsonl")


def _metric_of(entry: dict, metric: str) -> float:
    v = entry.get(metric)
    try:
        return float(v) if v is not None else float("-inf")
    except (TypeError, ValueError):
        return float("-inf")


class Archive:
    """Append-only, file-backed ledger of validated scaffold genomes.

    Each entry records a genome, the (now-burned) slice it was validated on, its pass@1 + CI, the gate
    verdict, its parent id, and behaviour descriptors. The archive lets the proposer build the next
    candidate on a chosen ancestor (best, or a least-explored quality-diversity cell) instead of a
    single mutable HEAD.
    """

    def __init__(self, path: str = _DEFAULT_ARCHIVE):
        self.path = path
        self._entries: list[dict] = []
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._entries.append(json.loads(line))
                    except Exception:
                        pass

    def add(self, genome: dict, *, slice_ids, pass_at_1, ci=None, gate_verdict=None,
            descriptors=None, ts=None) -> str:
        """Append a validated genome and return its id.

        Entry = {"id", "genome", "parent_id", "slice_ids", "pass_at_1", "ci", "gate_verdict",
        "descriptors", "ts"}. parent_id is taken from genome["parent_id"].
        """
        eid = genome_id(genome)
        entry = {
            "id": eid,
            "genome": genome,
            "parent_id": genome.get("parent_id"),
            "slice_ids": list(slice_ids),
            "pass_at_1": pass_at_1,
            "ci": ci,
            "gate_verdict": gate_verdict,
            "descriptors": descriptors,
            "ts": ts,
        }
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._entries.append(entry)
        return eid

    def get(self, entry_id: str) -> dict | None:
        """Return the most recent entry with this id, or None."""
        for entry in reversed(self._entries):
            if entry.get("id") == entry_id:
                return entry
        return None

    def all(self) -> list[dict]:
        """All entries in insertion (append) order."""
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def best(self, metric: str = "pass_at_1") -> dict | None:
        """The entry with the highest metric (None if empty; ties -> the most recently added)."""
        if not self._entries:
            return None
        best_entry = None
        best_val = float("-inf")
        for entry in self._entries:                  # forward scan; >= lets a later tie win
            val = _metric_of(entry, metric)
            if best_entry is None or val >= best_val:
                best_entry, best_val = entry, val
        return best_entry

    # ---- quality-diversity (MAP-Elites) ----------------------------------------------------------

    def qd_map(self, metric: str = "pass_at_1") -> dict:
        """MAP-Elites elite map: {cell_key: best entry in that cell} keyed by behaviour descriptors.

        An entry's cell is derived from its stored "descriptors"; entries without descriptors are
        skipped (they have no behaviour coordinates). Within a cell, the highest-metric entry wins;
        ties go to the most recently added (same rule as best()).
        """
        elites: dict[str, dict] = {}
        for entry in self._entries:
            desc = entry.get("descriptors")
            if not desc:
                continue
            key = cell_key(desc)
            cur = elites.get(key)
            if cur is None or _metric_of(entry, metric) >= _metric_of(cur, metric):
                elites[key] = entry
        return elites

    def _descendant_counts(self) -> dict:
        """Map id -> number of archive entries whose parent_id is that id."""
        counts: dict[str, int] = {}
        for entry in self._entries:
            pid = entry.get("parent_id")
            if pid is not None:
                counts[pid] = counts.get(pid, 0) + 1
        return counts

    def select_parent(self, strategy: str = "best", metric: str = "pass_at_1") -> dict | None:
        """Choose an ancestor to build the next proposal on (None if the archive is empty).

        - "best": the global best() by metric.
        - "qd":   from the MAP-Elites elite map, pick the elite in the LEAST-EXPLORED cell -- the one
                  with the FEWEST descendants (so under-explored cells get attention). Ties broken by
                  LOWEST metric, so weaker elites are revisited first; further ties by id for
                  determinism. This is deliberate, not random -- a fixed archive always yields the
                  same parent.
        """
        if not self._entries:
            return None
        if strategy == "qd":
            elites = self.qd_map(metric)
            if not elites:
                return self.best(metric)
            counts = self._descendant_counts()
            # rank: fewest descendants first, then lowest metric, then id (all ascending)
            return min(
                elites.values(),
                key=lambda e: (counts.get(e.get("id"), 0), _metric_of(e, metric), e.get("id") or ""),
            )
        # default / "best"
        return self.best(metric)

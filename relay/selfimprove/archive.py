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
import re
import time
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
    """Stable string key for a MAP-Elites cell from a descriptors dict.

    SEMANTIC DESCRIPTORS WIN WHEN THEY ARE PRESENT. The original axes bin by diff size and
    turn count, which are properties of the EPISODE rather than of the harness, so two
    genomes that behave completely differently land in one cell and the map keeps one elite
    at the other's expense. Phase 7 added behavioural axes and stored them under
    "semantic" -- and this function went on reading the top level, so every row carrying the
    new descriptors resolved to `empty|empty|none` and the whole archive collapsed into a
    single cell. A diversity map with one cell reports maximum quality and no diversity, and
    nothing about that looks like a failure.
    """
    semantic = desc.get("semantic")
    if isinstance(semantic, dict) and semantic:
        from relay.selfimprove import qd as QD
        return QD.cell_key(semantic)
    return "%s|%s|%s" % (
        desc.get("diff_bin", "empty"),
        desc.get("turns_bin", "empty"),
        desc.get("dominant_miss", "none"),
    )


# --------------------------------------------------------------------------------------------------
# 2. The archive (append-only jsonl ledger)
# --------------------------------------------------------------------------------------------------

_DEFAULT_ARCHIVE = os.path.join(os.path.dirname(__file__), "archive", "entries.jsonl")


#: A public benchmark instance id -- SWE-bench and SWE-bench Pro shapes. Defined here rather than
#: only in the test that checks the published file, so the rule has ONE definition and the write
#: path and the audit cannot drift apart.
PUBLIC_BENCH_ID = re.compile(
    r"^(instance_)?[A-Za-z0-9_.\-]+__[A-Za-z0-9_.\-]+-[0-9a-f]+(-v[0-9a-f]+|-vnan)?$")


class NotPublishable(ValueError):
    """Refused: this entry would put private work into the published archive."""


def _refuse_if_unpublishable(path, genome, slice_ids):
    """Nothing may enter the TRACKED archive that is not a public benchmark record.

    THE DEFAULT PATH IS THE PUBLISHED ONE. Archive() with no argument writes to
    relay/selfimprove/archive/entries.jsonl, which is tracked in a public repository, and
    scheduler.nightly() -- reached from its own CLI with no archive_path -- takes that default.
    So the loop's ordinary operation appends live genomes to a file that is pushed. Today the
    file holds two benchmark rows and nothing has escaped; the route is simply open.

    A test already audits the published file, but auditing is the wrong moment: it runs after
    the commit exists, and the first person to learn would be whoever pulled it. This refuses
    at the write, which is the only point where the alternative (write elsewhere) is still
    available.

    Two rules, both fail-closed:

      * every slice id must look like a public benchmark instance. This is the rule the audit
        already applies to slice_ids, moved to where it can prevent rather than report.
      * card VALUES must be flags, not prose. Cards are named switches today, and .gitignore
        already predicts that `genome.cards` will hold learned prompt text -- which is the
        actual leak, since prompt text learned from real work quotes real work. A card whose
        value is a string is refused here rather than published and noticed later.

    Any other path -- the runtime archive under .fleet, a temp file in a test -- is
    unrestricted. The restriction is a property of the destination, not of the data.
    """
    if os.path.abspath(path) != os.path.abspath(_DEFAULT_ARCHIVE):
        return
    bad = [str(s) for s in (slice_ids or []) if not PUBLIC_BENCH_ID.match(str(s))]
    if bad:
        raise NotPublishable(
            "refusing to write %d slice id(s) that are not public benchmark instances into the "
            "published archive (%s). Pass archive_path= a runtime archive such as "
            ".fleet/selfimprove/archive.jsonl instead. First: %r"
            % (len(bad), _DEFAULT_ARCHIVE, bad[0]))
    cards = (genome or {}).get("cards") or {}
    if isinstance(cards, dict):
        prose = sorted(k for k, v in cards.items() if isinstance(v, str))
        if prose:
            raise NotPublishable(
                "refusing to publish genome.cards carrying text rather than flags: %s. Learned "
                "prompt text quotes the work it was learned from; write it to a runtime archive."
                % ", ".join(prose))


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
            descriptors=None, ts=None, note=None) -> str:
        """Append a validated genome and return its id.

        Entry = {"id", "genome", "parent_id", "slice_ids", "pass_at_1", "ci", "gate_verdict",
        "descriptors", "ts", "note"}. parent_id is taken from genome["parent_id"].

        `note` is free text about THIS measurement, and it exists because of one the archive
        could not explain. The same genome was recorded at 0.34 and then at 0.50; the first
        grade had been corrupted by the grading host and was re-run in isolation. Since the id
        is a content hash, the second row supersedes the first by construction -- the archive
        already says WHICH row was replaced. What it could not say was WHY, so a reader a month
        later saw two measurements of one scaffold and no way to tell a correction from a
        genuine change. The reason lived only in a commit message.

        Not written into `descriptors`: those are behavioural coordinates and the QD map is
        built from them, so prose there would invent cells.
        """
        eid = genome_id(genome)
        entry = {
            "id": eid,
            "note": note,
            "genome": genome,
            "parent_id": genome.get("parent_id"),
            "slice_ids": list(slice_ids),
            "pass_at_1": pass_at_1,
            "ci": ci,
            "gate_verdict": gate_verdict,
            "descriptors": descriptors,
            # STAMPED HERE IF THE CALLER DID NOT. Every existing row has `ts: null`,
            # because the only caller never passed one -- so the durable record of an
            # experiment could not say when it ran, could not be ordered against
            # another, and could not be correlated with anything else that happened
            # that day. A default of "now" is not a guess: the row is being written
            # now, and an explicit `ts` still wins for a replayed or backfilled entry.
            "ts": time.time() if ts is None else ts,
        }
        # Checked BEFORE the file is touched, so a refusal leaves nothing half-written.
        _refuse_if_unpublishable(self.path, genome, entry["slice_ids"])
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

    #: Verdicts a row may carry and still be selected as a parent or an elite. KEEP is proven
    #: better; INCONCLUSIVE ran cleanly and was not proven worse, which is a legitimate place
    #: to build from. Everything else is disqualifying, and the omission mattered in two
    #: directions: a REJECTED or SECURITY_REJECTED scaffold could be picked as the parent of
    #: the next generation, and -- worse -- an INFRA_ABORT stayed selectable, so a candidate
    #: that could force an abort had a strictly better outcome than one that accepted a
    #: rejection. Neither activates, but only one survives as a lineage.
    #: TWO VOCABULARIES REACH THIS FIELD and both have to be spelled out: the SWE gate writes
    #: its verdict strings (keep / suggestive / negligible / underpowered / non-positive) and
    #: the controller writes decision states (KEEP / REJECT / INFRA_ABORT / ...). An allow-list
    #: rather than a deny-list, because a verdict nobody thought about should default to "not
    #: a parent" -- being conservative about lineage costs an opportunity, being permissive
    #: costs the meaning of the archive.
    SELECTABLE_VERDICTS = frozenset({
        "KEEP", "keep",                      # proven better
        "INCONCLUSIVE", "inconclusive",      # ran cleanly, not proven worse
        "suggestive",                        # positive direction, underpowered -- same thing
        "underpowered",                      # too few instances to say; not a failure
        "negligible",                        # significant but small; still an improvement
        "",                                  # a row that predates the field
    })

    #: How far a lineage may run on unproven steps before it must be re-validated.
    #: Keeping "not proven worse" selectable is right -- most real experiments are
    #: underpowered, and discarding them kills promising branches. But chaining them without
    #: limit lets a lineage drift arbitrarily far from anything the gate ever accepted, one
    #: statistically invisible step at a time, and the archive would still call the result a
    #: descendant of a validated scaffold. Bounded exploration; the bound is the point.
    MAX_UNVALIDATED_DEPTH = 3

    @classmethod
    def _verdict_ok(cls, entry) -> bool:
        verdict = entry.get("gate_verdict")
        if verdict is None:
            return True                      # predates the field
        return str(verdict) in cls.SELECTABLE_VERDICTS

    @staticmethod
    def _is_accepted(entry) -> bool:
        return str(entry.get("gate_verdict") or "") in ("KEEP", "keep")

    def lineage(self, tip_id: str) -> list:
        """The chain from the root down to `tip_id`, oldest first. Empty if the tip is unknown.

        WHY A LINEAGE AND NOT THE WHOLE ARCHIVE

        `plateaued` asks "has this DIRECTION stopped paying". Fed the flat archive, it answers
        a different question the moment a second branch exists: one branch's KEEP resets the
        plateau for a branch that has been failing for weeks, and the loop keeps spending
        nights on it. The archive was built to branch -- that is the first thing its module
        docstring says -- so the statistic that decides whether to keep going has to be scoped
        to the line being pursued.

        Health is deliberately NOT scoped this way. An INFRA_ABORT is a property of the
        instrument, not of the branch it happened on, and it is a reason not to run tonight
        whichever line the run would have followed.
        """
        by_id = {e.get("id"): e for e in self._entries if e.get("id")}
        chain, seen = [], set()
        cur = by_id.get(tip_id)
        while cur is not None:
            cur_id = cur.get("id")
            if cur_id in seen:          # a cycle in recorded lineage; stop rather than spin
                break
            seen.add(cur_id)
            chain.append(cur)
            parent = cur.get("genome", {}).get("parent_id") or cur.get("parent_id")
            cur = by_id.get(parent)
        chain.reverse()
        return chain

    def tip(self):
        """The most recently appended entry, or None. The line the loop is currently on."""
        return self._entries[-1] if self._entries else None

    def _unvalidated_depth(self, entry) -> int:
        """How many unaccepted steps sit between this row and its nearest KEEP ancestor."""
        by_id = {e.get("id"): e for e in self._entries if e.get("id")}
        depth, seen = 0, set()
        cur = entry
        while cur is not None and not self._is_accepted(cur):
            cur_id = cur.get("id")
            if cur_id in seen:               # a cycle in recorded lineage; stop counting
                break
            seen.add(cur_id)
            depth += 1
            parent = cur.get("genome", {}).get("parent_id") or cur.get("parent_id")
            cur = by_id.get(parent)
            if cur is None:
                break                        # root, or a parent not in this archive
        return depth

    def _selectable(self, entry) -> bool:
        """A row with no verdict at all predates the field and is left selectable."""
        if not self._verdict_ok(entry):
            return False
        return self._unvalidated_depth(entry) <= self.MAX_UNVALIDATED_DEPTH

    def best(self, metric: str = "pass_at_1", include_unselectable: bool = False) -> dict | None:
        """The best SELECTABLE entry (None if empty; ties -> the most recently added).

        `include_unselectable` exists for reporting -- "what was the highest score we ever
        saw, including the ones we threw away" is a fair question, and a different one from
        "what should the next candidate descend from".
        """
        entries = (self._entries if include_unselectable
                   else [e for e in self._entries if self._selectable(e)])
        if not entries:
            return None
        best_entry = None
        best_val = float("-inf")
        for entry in entries:                        # forward scan; >= lets a later tie win
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
            if not self._selectable(entry):
                continue                              # see SELECTABLE_VERDICTS
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

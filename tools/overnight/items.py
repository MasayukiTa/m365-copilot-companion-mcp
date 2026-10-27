"""Input: the list of things to investigate, and the one-to-two sentence goal built from each.

items.txt   one short Japanese request per line. Optional leading "ID: " (e.g. "A1: ...").
            A line "# 前置き: <one sentence>" is a preamble shared by every item.
            Blank lines and other "#" lines are ignored.
items.json  [{"id": "A1", "ask": "...", "tags": ["..."], "max_minutes": 20}, ...]
            optional {"preamble": "...", "items": [...]} wrapper.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MAX_ITEMS = 50
MAX_ASK_CHARS = 200
MAX_PREAMBLE_CHARS = 80
MAX_GOAL_CHARS = 400

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")
_LEADING_ID_RE = re.compile(r"^([A-Za-z]{1,4}\d{1,4})\s*[:：]\s*(.+)$")
_PREAMBLE_RE = re.compile(r"^#\s*前置き\s*[:：]\s*(.*)$")


@dataclass
class Item:
    id: str
    ask: str
    tags: list = field(default_factory=list)
    max_minutes: float | None = None


@dataclass
class ParseResult:
    items: list
    preamble: str = ""
    warnings: list = field(default_factory=list)
    #: (item id, reason) for lines that were read but will not be run (duplicate, over the cap).
    skipped: list = field(default_factory=list)


def _clean_preamble(text: str) -> str:
    return text.strip()[:MAX_PREAMBLE_CHARS].strip()


def _read_text(path) -> str:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _raw_entries(path):
    """-> (preamble, [dict(id|None, ask, tags, max_minutes)])"""
    p = Path(path)
    text = _read_text(p)
    entries = []
    preamble = ""
    if p.suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            preamble = str(data.get("preamble") or data.get("前置き") or "")
            data = data.get("items", [])
        if not isinstance(data, list):
            raise ValueError("items.json must be a list of objects")
        for row in data:
            if isinstance(row, str):
                row = {"ask": row}
            if not isinstance(row, dict):
                continue
            entries.append({
                "id": (str(row["id"]).strip() if row.get("id") else None),
                "ask": str(row.get("ask") or "").strip(),
                "tags": list(row.get("tags") or []),
                "max_minutes": row.get("max_minutes"),
            })
        return preamble, entries
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = _PREAMBLE_RE.match(s)
        if m:
            preamble = m.group(1)
            continue
        if s.startswith("#"):
            continue
        m = _LEADING_ID_RE.match(s)
        if m:
            entries.append({"id": m.group(1), "ask": m.group(2).strip(), "tags": [],
                            "max_minutes": None})
        else:
            entries.append({"id": None, "ask": s, "tags": [], "max_minutes": None})
    return preamble, entries


def parse_items(path, max_items: int = DEFAULT_MAX_ITEMS) -> ParseResult:
    preamble, entries = _raw_entries(path)
    res = ParseResult(items=[], preamble=_clean_preamble(preamble))
    if len(preamble.strip()) > MAX_PREAMBLE_CHARS:
        res.warnings.append("preamble was cut to %d characters" % MAX_PREAMBLE_CHARS)
    used_ids = {e["id"] for e in entries if e["id"]}
    seen_id = set()
    auto = 0
    for e in entries:
        ask = e["ask"]
        iid = e["id"]
        if not iid:
            while True:
                auto += 1
                iid = "I%03d" % auto
                if iid not in used_ids and iid not in seen_id:
                    break
        if not _ID_RE.match(iid):
            res.warnings.append("invalid id %r replaced" % iid)
            auto += 1
            iid = "I%03d" % auto
        if iid in seen_id:
            res.skipped.append((iid, "duplicate id", ask))
            res.warnings.append("duplicate id %s: later entry skipped" % iid)
            continue
        if not ask:
            res.skipped.append((iid, "empty request", ask))
            continue
        if len(ask) > MAX_ASK_CHARS:
            res.warnings.append("%s: request longer than %d chars was cut" % (iid, MAX_ASK_CHARS))
            ask = ask[:MAX_ASK_CHARS]
        # TWO REQUESTS WITH THE SAME WORDING ARE BOTH KEPT. This used to drop a later entry whose
        # normalised ask matched an earlier one's -- a content match, exactly what the owner said
        # must never stop a job (2026-10-26/27). Duplicate IDs are still rejected above, because
        # that is an identity collision (two rows claiming the same id), not a resemblance
        # between two different requests.
        seen_id.add(iid)
        if len(res.items) >= max_items:
            res.skipped.append((iid, "over the item limit (%d)" % max_items, ask))
            continue
        mm = e["max_minutes"]
        try:
            mm = float(mm) if mm not in (None, "") else None
        except (TypeError, ValueError):
            mm = None
        res.items.append(Item(id=iid, ask=ask, tags=e["tags"], max_minutes=mm))
    over = [s for s in res.skipped if s[1].startswith("over the item limit")]
    if over:
        res.warnings.append("%d item(s) over the limit of %d were not queued" % (len(over), max_items))
    if not res.items:
        res.warnings.append("no runnable items")
    return res


def _one_sentence(text: str) -> str:
    """No sentence break inside: the goal must stay at one or two sentences."""
    t = re.sub(r"\s+", " ", text.replace("\r", " ").replace("\n", " ")).strip()
    t = t.replace("。", "、").replace("．", "、")
    return t.rstrip("、")


def build_goal(item: Item, out_dir, preamble: str = "", variant: int = 0) -> str:
    """One line, at most two sentences, Japanese. The item id is in it so results can be matched."""
    folder = ("%s/%s/" % (str(out_dir).replace("\\", "/").rstrip("/"), item.id))
    ask = _one_sentence(item.ask)
    pre = _one_sentence(preamble) if preamble else ""
    head = "[%s] " % item.id
    if variant:
        head += "(項目%s・再依頼%d) " % (item.id, variant)
    first = (pre + "、" if pre else "") + ask
    goal = "%s%s。結果の表と図は %s に保存して、要点を3行で答えて。" % (head, first, folder)
    if len(goal) > MAX_GOAL_CHARS:
        raise ValueError("goal for %s is %d chars (limit %d)" % (item.id, len(goal), MAX_GOAL_CHARS))
    return goal

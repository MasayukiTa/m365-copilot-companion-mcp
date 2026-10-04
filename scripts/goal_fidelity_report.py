#!/usr/bin/env python
"""Goal-fidelity analyzers over fleet transcripts (read-only, deterministic, no model calls).

Two questions about a worker that runs many turns on one goal:

B1  CONSTRAINT RETENTION.  "Hard constraint tokens" are pulled out of the goal by a few
    transparent rules (below) or given explicitly with --must. For every assistant turn we
    record which tokens the reply contains. DRIFT SCORE = share of the turns after turn 1
    whose reply contains none of the tokens, counted only for transcripts where an earlier
    turn did contain one (a goal whose constraints were never echoed cannot "drift").

B2  FALSE DENIALS.  Assistant sentences that claim the original text lacks something
    ("原文に存在しません", "指示にありません", ...). The item named in the sentence (quoted
    text, a time/date, or a --must token) is checked by string containment against the goal:
    present in the goal = FALSE denial (the claim is wrong), absent = true denial, no
    checkable item in the sentence = unresolved.

Token extraction rules (goal text only, NFKC-normalised, whitespace ignored when matching):
  1. quoted strings: 「...」 『...』 "..." (2-40 chars)
  2. clock times: 14:00, 4:00
  3. dates: 10/3, 2026/10/2, 10月3日
  4. cue runs: within 30 chars after a cue word (絶対 必ず だけ ただし 必須 固定 厳守 のみ)
     up to three katakana runs (3+ chars) or kanji runs (2+ chars)
At most MAX_TOKENS (40) tokens, first seen first. This is a heuristic on purpose: pass the
exact tokens with --must for an experiment.

Usage:
  python scripts/goal_fidelity_report.py r6abc7f6c_a0 r6abcdb87_a0 [--dir .fleet/transcripts]
  python scripts/goal_fidelity_report.py "some/dir/*.jsonl*" --must "14:00" --json out.json
A spec is a run id (prefix of the file names, e.g. r6abc7f6c_a0) or a glob. .jsonl and
.jsonl.gz are both read. Transcripts are opened read-only.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import io
import json
import os
import re
import sys
import unicodedata

MAX_TOKENS = 40

CUES = ("絶対", "必ず", "だけ", "ただし", "必須", "固定", "厳守", "のみ")

_QUOTE_RE = re.compile(r"「([^」]{2,40})」|『([^』]{2,40})』|\"([^\"]{2,40})\"")
_TIME_RE = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)")
_DATE_RE = re.compile(r"(?<!\d)(?:\d{4}/)?\d{1,2}/\d{1,2}(?!\d)|\d{1,2}月\d{1,2}日")
_KATA_RE = re.compile(r"[ァ-ヴー]{3,}")
_KANJI_RE = re.compile(r"[一-龥々]{2,}")

DENIAL_RE = re.compile(
    "|".join([
        r"原文に(?:は)?(?:存在|ありません|見当たりません|含まれ)",
        r"元の(?:文章|指示|ゴール|依頼)(?:に(?:は)?)(?:存在|ありません|含まれ|記載)",
        r"ユーザー(?:は|が)(?:そう)?言っていません",
        r"(?:ユーザー)?(?:原文|指示|依頼)にありません",
        r"記載がありません",
        r"元の指示に含まれていません",
        r"ユーザー原文に存在しません",
        r"指定(?:は|が)ありません",
    ]))

_SENT_SPLIT = re.compile(r"(?<=[。！？!?])|\n+")


def norm(text):
    """NFKC + drop whitespace, so full-width digits/colons and line breaks do not hide a match."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))


def extract_tokens(goal):
    g = unicodedata.normalize("NFKC", str(goal or ""))
    found = []

    def add(tok):
        tok = tok.strip()
        if tok and tok not in found:
            found.append(tok)

    for m in _QUOTE_RE.finditer(g):
        add(next(x for x in m.groups() if x))
    for m in _TIME_RE.finditer(g):
        add(m.group(0))
    for m in _DATE_RE.finditer(g):
        add(m.group(0))
    for cue in CUES:
        start = 0
        while True:
            i = g.find(cue, start)
            if i < 0:
                break
            window = g[i + len(cue): i + len(cue) + 30]
            runs = sorted(
                [(m.start(), m.group(0)) for m in _KATA_RE.finditer(window)]
                + [(m.start(), m.group(0)) for m in _KANJI_RE.finditer(window)])
            for _, run in runs[:3]:
                add(run)
            start = i + len(cue)
    return found[:MAX_TOKENS]


def load_transcript(path):
    """(goal, [assistant reply text in order]) from one .jsonl / .jsonl.gz, read-only."""
    opener = gzip.open if path.endswith(".gz") else open
    goal, replies = "", []
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("meta"):
                goal = goal or str(rec.get("goal") or "")
            elif rec.get("role") == "assistant":
                text = str(rec.get("text") or "")
                if text.strip():
                    replies.append(text)
    return goal, replies


def retention(tokens, replies):
    """Per-turn presence and the drift score (B1)."""
    ntoks = [(t, norm(t)) for t in tokens]
    per_turn = []
    for text in replies:
        nt = norm(text)
        per_turn.append([t for t, n in ntoks if n and n in nt])
    later = per_turn[1:]
    drifted = 0
    for i in range(1, len(per_turn)):
        if not per_turn[i] and any(per_turn[:i]):
            drifted += 1
    score = (drifted / len(later)) if later else 0.0
    return {"per_turn": per_turn, "later_turns": len(later), "drifted_turns": drifted,
            "drift_score": round(score, 4)}


def _items_in_sentence(sentence, must):
    s = unicodedata.normalize("NFKC", sentence)
    items = []
    for m in _QUOTE_RE.finditer(s):
        items.append(next(x for x in m.groups() if x))
    items += _TIME_RE.findall(s)
    items += _DATE_RE.findall(s)
    ns = norm(s)
    for t in must:
        if norm(t) and norm(t) in ns and t not in items:
            items.append(t)
    return items


def denials(goal, replies, must=()):
    """Deterministic false-denial detection (B2)."""
    ngoal = norm(goal)
    out = {"false": 0, "true": 0, "unresolved": 0, "examples": []}
    for turn, text in enumerate(replies, 1):
        for sent in _SENT_SPLIT.split(text):
            if not sent or not DENIAL_RE.search(sent):
                continue
            items = _items_in_sentence(sent, must)
            if not items:
                out["unresolved"] += 1
                continue
            hit = [i for i in items if norm(i) and norm(i) in ngoal]
            if hit:
                out["false"] += 1
                if len(out["examples"]) < 3:
                    out["examples"].append({"turn": turn, "item": hit[0], "sentence": sent.strip()[:160]})
            else:
                out["true"] += 1
    return out


def resolve_files(specs, base_dir):
    files = []
    for spec in specs:
        if any(c in spec for c in "*?[") or os.sep in spec or "/" in spec:
            hits = glob.glob(spec)
        else:
            hits = glob.glob(os.path.join(base_dir, spec + "_w*.jsonl*")) \
                or glob.glob(os.path.join(base_dir, spec + "*.jsonl*"))
        files.append((spec, sorted(set(hits))))
    return files


def analyse(specs, base_dir, must=()):
    runs = []
    for spec, paths in resolve_files(specs, base_dir):
        agg = {"run": spec, "transcripts": 0, "assistant_turns": 0, "later_turns": 0,
               "drifted_turns": 0, "false_denials": 0, "true_denials": 0,
               "unresolved_denials": 0, "examples": []}
        for p in paths:
            goal, replies = load_transcript(p)
            if not replies:
                continue
            toks = list(must) or extract_tokens(goal)
            ret = retention(toks, replies)
            den = denials(goal, replies, must)
            agg["transcripts"] += 1
            agg["assistant_turns"] += len(replies)
            agg["later_turns"] += ret["later_turns"]
            agg["drifted_turns"] += ret["drifted_turns"]
            agg["false_denials"] += den["false"]
            agg["true_denials"] += den["true"]
            agg["unresolved_denials"] += den["unresolved"]
            agg["examples"] += [dict(e, file=os.path.basename(p)) for e in den["examples"]]
        agg["drift_score"] = round(agg["drifted_turns"] / agg["later_turns"], 4) if agg["later_turns"] else 0.0
        agg["examples"] = agg["examples"][:5]
        runs.append(agg)
    total = {k: sum(r[k] for r in runs) for k in
             ("transcripts", "assistant_turns", "later_turns", "drifted_turns",
              "false_denials", "true_denials", "unresolved_denials")}
    total["drift_score"] = round(total["drifted_turns"] / total["later_turns"], 4) if total["later_turns"] else 0.0
    return {"runs": runs, "total": total}


def format_table(result):
    cols = ("run", "transcripts", "assistant_turns", "later_turns", "drifted_turns", "drift_score",
            "false_denials", "true_denials", "unresolved_denials")
    rows = [cols] + [tuple(str(r[c]) for c in cols) for r in result["runs"]] \
        + [("TOTAL",) + tuple(str(result["total"][c]) for c in cols[1:])]
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(cols))]
    return "\n".join("  ".join(str(v).ljust(w) for v, w in zip(r, widths)) for r in rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("specs", nargs="+", help="run ids or file globs")
    ap.add_argument("--dir", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                  ".fleet", "transcripts"))
    ap.add_argument("--must", action="append", default=[], help="exact constraint token (repeatable)")
    ap.add_argument("--json", nargs="?", const="-", default=None, help="write JSON (path, or - for stdout)")
    args = ap.parse_args(argv)
    result = analyse(args.specs, args.dir, args.must)
    print(format_table(result))
    if args.json:
        blob = json.dumps(result, ensure_ascii=False, indent=2)
        if args.json == "-":
            print(blob)
        else:
            with io.open(args.json, "w", encoding="utf-8") as fh:
                fh.write(blob)
    return 0


if __name__ == "__main__":
    sys.exit(main())

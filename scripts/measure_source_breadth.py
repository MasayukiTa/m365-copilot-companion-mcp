# -*- coding: utf-8 -*-
"""How many kinds of source does a worker actually consider before it gives up?

THE RUN THAT PROMPTED THIS. A worker was asked for a 成績書 item and searched one database
exhaustively -- every inspection view, the item master, its alias column, every column name
and every master value -- found no exact match, and reported STUCK asking what the term
meant. The answer was in a spreadsheet. Counting its own words across ten turns: database
terms 39 times, master-table terms 25 times, spreadsheet or file or folder terms ZERO. The
protocol it was given mentions files and spreadsheets repeatedly; the worker never did.

That is one run, and one run is an anecdote. This counts the same thing across every
transcript on disk, so the question "do workers lock onto a single kind of source" has a
number instead of a story. The interesting cut is the STUCK runs: a worker that finished is
not evidence about giving up too early.

WHAT IT MEASURES AND WHAT IT DOES NOT. It counts VOCABULARY in the worker's own replies, not
tool calls -- the transcript records what was said, and a reply that never names a file is
strong evidence that files were not considered, while it is not proof that none was opened.
The ASSISTANT text only: the protocol preamble is repeated in every user turn and mentions
every source family, so counting it would make every worker look broad-minded.
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
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: Deliberately plain words, the ones a worker would use while describing what it did. Not
#: tool names: a worker that says "spreadsheet" considered spreadsheets whether or not it
#: managed to name the tool.
FAMILIES = {
    "database": ("DB", "データベース", "テーブル", "マスタ", "ビュー", "SELECT", "select",
                 "クエリ", "スキーマ", "列名", "レコード"),
    "files": ("ファイル", "フォルダ", "ディレクトリ", "パス", "read_file", "list_directory",
              "glob", "デスクトップ"),
    "spreadsheet": ("Excel", "excel", "xlsx", "xls", "シート", "ブック", "スプレッドシート"),
    "documents": ("pptx", "docx", "PowerPoint", "Word", "PDF", "pdf", "スライド"),
    "mail": ("Outlook", "メール", "mail", "受信トレイ"),
    "web": ("Web", "web", "検索", "URL", "http"),
}

STUCK_RE = re.compile(r"\bSTUCK\b")


def _open(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return io.open(path, encoding="utf-8", errors="replace")


def _assistant_text(path) -> str:
    out = []
    try:
        with _open(path) as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if (r.get("role") or "") == "assistant":
                    out.append(str(r.get("text") or ""))
    except OSError:
        return ""
    return "\n".join(out)


def families_in(text: str):
    hits = {}
    for fam, words in FAMILIES.items():
        n = sum(text.count(w) for w in words)
        if n:
            hits[fam] = n
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default=r"C:\Users\USER\resonac-mcp\.fleet\transcripts")
    ap.add_argument("--limit", type=int, default=0, help="0 = every transcript")
    ap.add_argument("--min-chars", type=int, default=400,
                    help="ignore runs too short to have considered anything (default 400)")
    args = ap.parse_args(argv)

    paths = sorted(glob.glob(os.path.join(args.dir, "*.jsonl"))
                   + glob.glob(os.path.join(args.dir, "*.jsonl.gz")),
                   key=os.path.getmtime, reverse=True)
    if args.limit:
        paths = paths[:args.limit]
    print("transcripts: %d" % len(paths))

    single = Counter()
    breadth = Counter()
    stuck_single = Counter()
    stuck_total = 0
    considered = 0
    examples = []

    for p in paths:
        text = _assistant_text(p)
        if len(text) < args.min_chars:
            continue
        considered += 1
        hits = families_in(text)
        breadth[len(hits)] += 1
        is_stuck = bool(STUCK_RE.search(text))
        if is_stuck:
            stuck_total += 1
        if len(hits) == 1:
            fam = next(iter(hits))
            single[fam] += 1
            if is_stuck:
                stuck_single[fam] += 1
                if len(examples) < 8:
                    examples.append((os.path.basename(p), fam, hits[fam], len(text)))

    print("with enough of the worker's own words to judge: %d" % considered)
    print("of those, ended with STUCK somewhere in their replies: %d" % stuck_total)
    print()
    print("how many source families a run's own words touched:")
    for n in sorted(breadth):
        pct = 100.0 * breadth[n] / considered if considered else 0
        print("   %d famil%s  %5d  %5.1f%%" % (n, "y " if n == 1 else "ies", breadth[n], pct))
    print()
    print("runs that named exactly ONE family, by which one:")
    for fam, n in single.most_common():
        print("   %-12s %5d   (of which STUCK: %d)" % (fam, n, stuck_single.get(fam, 0)))

    ss = sum(stuck_single.values())
    print()
    if stuck_total:
        print("SINGLE-SOURCE AMONG THE RUNS THAT GAVE UP: %d of %d (%.1f%%)"
              % (ss, stuck_total, 100.0 * ss / stuck_total))
        print("A run that finished is not evidence about giving up too early, which is why")
        print("this is the cut that matters.")
    if examples:
        print()
        print("first few single-source runs that ended STUCK:")
        for name, fam, n, chars in examples:
            print("   %-38s only %-11s (%d mentions, %d chars of reply)"
                  % (name, fam, n, chars))
    return 0


if __name__ == "__main__":
    sys.exit(main())

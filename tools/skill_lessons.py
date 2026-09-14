# -*- coding: utf-8 -*-
"""What one instruction said that a failing instruction for the same work did not.

WHY NOT THE GOAL TEXT ALONE. The first attempt at grounding a generated Skill read each repeated
goal and looked for what a Skill needs -- where the files are, what the output must look like.
Measured: of 156 qualified candidates only 25 name an absolute path, 65 declare an output
contract, and 7 carry both. Seven is not a corpus.

WHY NOT `skill_candidates.normalise` EITHER, WHICH IS THE MISTAKE THIS FILE WAS FIRST WRITTEN
WITH. That function groups two runs as the same work when their goals normalise to the same
text -- and it rewrites every path to `<path>`. So "do X" and "do X, the files are at C:\\..."
normalise apart and land in different groups. Grouping that way and then asking what the
instructions differed by returned, when run, exactly 0: by construction the only pairs it could
see were byte-identical instructions. The grouping has to be by THE WORK, not by the wording,
because the wording is the thing being compared.

WHAT THIS GROUPS ON INSTEAD. Content-word overlap (Jaccard over words of 3+ latin letters or 2+
kana/kanji, stopwords dropped). Two attempts are the same work at `MIN_SIMILARITY`, which is
high enough that the pairs it returns are recognisably one job and low enough to let an added
paragraph through. It is a heuristic and it is printed, not hidden: `--show N` prints both
instructions so a person can disagree with the pairing.

WHAT IT EXTRACTS. The segments of the working instruction that the failing one did not contain.
Nothing is paraphrased and nothing is explained -- a generated account of WHY something worked
is a guess wearing the clothes of a finding, and this repository has spent a day removing those.
A segment is quoted or it is absent.

THE RECURRING ADDITION IS THE REAL FINDING. One pair is an anecdote: a thousand things differ
between two runs and only one of them is written down. The same addition appearing across
several unrelated jobs is evidence. `recurring()` clusters the added segments and reports how
many distinct jobs each was added to -- measured on this ledger, the top cluster is telling the
worker to use its own native search instead of the `call_tool` gateway, which separated STUCK
from DONE in four different investigations.

NOT A VERDICT ON THE ANSWER. `DONE` is the worker saying DONE; no row in this ledger carries an
external check (measured: zero have a `verified` field, and the only externally-judged signal is
the negative `EVIDENCE_CONTRADICTED`). A lesson claims only: *this attempt ended DONE, that one
did not, and here is the text one had.* Whether the output was any good is the human approval
step, which nothing here replaces.

    python -m tools.skill_lessons              # recurring additions, then the pairs
    python -m tools.skill_lessons --show N     # both instructions of pair N, to disagree with
    python -m tools.skill_lessons --json
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import math
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools.skill_candidates import LEDGER, is_benchmark  # noqa: E402

#: Outcomes that mean the attempt did not deliver.
FAILED = ("STUCK", "REFUSED", "EVIDENCE_CONTRADICTED", "CANCELLED")

#: How much of the content has to be shared before two attempts are "the same work". Measured
#: on this ledger: 0.5 yields 20 pairs whose two sides are recognisably one job. Lower admits
#: unrelated investigations that merely share a vocabulary; higher admits only near-duplicates,
#: whose difference is too small to have caused anything.
MIN_SIMILARITY = 0.5

#: Below this many content words a goal is too short for overlap to mean anything.
MIN_WORDS = 4

#: A word that carries content. Latin runs of 3+, or CJK/kana runs of 2+ -- Japanese has no
#: spaces, so this deliberately over-segments rather than pretending to tokenise properly.
_WORD = re.compile(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}|[\u30a0-\u30ff]{2,}")

_STOP = frozenset("the and for with from this that you your please into out all any are was "
                  "were have has had not but its use using can will should".split())

#: Where one instruction ends a thought. Japanese operators write in 【bracketed】 headings and
#: 。-terminated sentences; English ones end on `. `. All are split so an added paragraph
#: surfaces as its own segment rather than being swallowed into a wall of text nobody can
#: compare. The ASCII rule requires the following whitespace, so `module_06.py` stays whole; a
#: decimal followed by a space would split, which costs a spurious segment boundary and nothing
#: more. Written first with only the Japanese rules, an English instruction came back as one
#: indivisible segment and every difference inside it was invisible.
#: Clause boundaries are split too, not only sentence ends: an operator who added the missing
#: fact often added it as a trailing clause of a sentence that was already there ("do X, the
#: files are at D:/..."). Segmenting only at sentence ends leaves that whole sentence as one
#: piece which then reads as a rewording of the old one, and the addition disappears.
_SEGMENT = re.compile(r"(?<=。)|(?<=[.!?])\s+|(?<=[、,])|(?=【)|\n+")

#: Phrases THIS REPOSITORY writes into goals itself -- fan-out headers from `relay/fanout.py`
#: and the retry preamble from `relay/project_memory.py`. Run without this filter, they were the
#: top three "recurring lessons" by a wide margin, which is the failure mode that matters most
#: here: a generator that mines its own boilerplate reports the machine's habits back as though
#: an operator had discovered them. They are excluded not because they are unhelpful but because
#: nobody chose them, so their presence ahead of a success is evidence of nothing.
#:
#: `test_a_lesson_is_not_our_own_boilerplate` asserts each of these still appears in the module
#: that emits it: if the fan-out wording is reworded, the test goes red rather than this filter
#: quietly ceasing to match while the counts drift back up.
#: A third phrase was listed here on the strength of it topping the unfiltered lesson list --
#: and the test that each mark must appear in the module named beside it found that no module
#: contains it. It was typed into a goal by a person, not emitted by this code, so the reason
#: for excluding it did not hold and it is not excluded. That is what the test is for.
MACHINE_AUTHORED = (
    ("担当する範囲", "relay/fanout.py"),
    ("サブタスクに分割", "relay/fanout.py"),
)


def _rows(ledger=None):
    try:
        for line in io.open(ledger or LEDGER, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("event") == "worker_done":
                yield row
    except OSError:
        return


def words(text):
    return {w.lower() for w in _WORD.findall(text or "")} - _STOP


def similarity(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def segments(text):
    """The instruction cut into comparable pieces, in order, blanks dropped."""
    return [s.strip() for s in _SEGMENT.split(str(text or "")) if s and s.strip()]


def is_machine_authored(segment):
    """Did this repository write this text into the goal rather than a person?"""
    return any(mark in (segment or "") for mark, _src in MACHINE_AUTHORED)


def added(failed_goal, worked_goal):
    """Segments the working instruction carried that the failing one did not.

    A SEGMENT COUNTS AS ADDED ONLY IF IT BRINGS CONTENT WORDS THE FAILING INSTRUCTION NEVER
    USED. The first rule here was "not similar to any earlier segment", and it let through every
    reordering, because a reordered sentence resembles no single old one while saying nothing
    new. Requiring new vocabulary is the sharper question and the one that matches what a lesson
    is: the operator told the worker something it had not been told.

    IT DOES NOT HANDLE MORPHOLOGY. "report" and "reported" are different words to this function,
    so a rephrasing that inflects differently will be reported as an addition. The cost is a
    noisier draft, which a person reads; the alternative -- stemming two languages -- would
    silently merge things that are not the same, which a person cannot see.
    """
    known = words(failed_goal)
    out = []
    for seg in segments(worked_goal):
        if is_machine_authored(seg):
            continue
        w = words(seg)
        if not w or not (w - known):
            continue
        out.append(seg)
    return out


def pairs(ledger=None, include_benchmarks=False, min_similarity=MIN_SIMILARITY):
    """Every (failed, worked) attempt at the same work whose instructions were worded differently.

    THE BLOCKING IS AN OPTIMISATION AND IT IS PROVABLY COMPLETE, which the first version was
    not. That one skipped words appearing in more than a quarter of rows; on a two-row fixture
    every word qualifies, the candidate set empties, and the function returns nothing at all --
    a scan that silently finds less the smaller its input gets.

    The bound used instead: if |A n B| / |A u B| >= t then |A n B| >= t|A|, so at most (1-t)|A|
    of A's words lie outside the intersection, and ANY subset of A larger than that must contain
    a shared word. Probing with the ceil((1-t)|A|)+1 rarest words of A therefore cannot miss a
    pair that would have qualified, at any threshold and any corpus size.
    """
    rows = []
    for row in _rows(ledger):
        goal = str(row.get("goal") or "").strip()
        if not goal:
            continue
        if not include_benchmarks and is_benchmark(goal):
            continue
        w = words(goal)
        if len(w) < MIN_WORDS:
            continue
        rows.append((row, goal, w))

    index = collections.defaultdict(list)
    for i, (_r, _g, w) in enumerate(rows):
        for word in w:
            index[word].append(i)

    found, seen = [], set()
    for i, (ri, gi, wi) in enumerate(rows):
        if str(ri.get("outcome") or "").upper() not in FAILED:
            continue
        probe = sorted(wi, key=lambda word: len(index[word]))
        keep = int(math.ceil((1.0 - min_similarity) * len(wi))) + 1
        cand = set()
        for word in probe[:keep]:
            cand.update(index[word])
        # ONE COMPARISON PER FAILURE, AGAINST THE NEAREST SUCCESS. Without this, a single stuck
        # run that resembles twenty near-identical successes contributes twenty "pairs" -- a
        # cross-product that inflates every count downstream while carrying one fact. The
        # nearest success is also the fairest comparison available: the fewer unrelated
        # differences between the two instructions, the likelier the remainder is the reason.
        best = None
        for j in cand:
            rj, gj, wj = rows[j]
            if str(rj.get("outcome") or "").upper() != "DONE":
                continue
            if gi == gj:
                continue                     # identical wording carries no lesson
            sim = similarity(wi, wj)
            if sim < min_similarity:
                continue
            if best is None or sim > best[0]:
                best = (sim, gj, rj)
        if best is None:
            continue
        sim, gj, rj = best
        key = (gi, gj)
        if key in seen:
            continue
        seen.add(key)
        add = added(gi, gj)
        if not add:
            continue
        found.append({
            "similarity": round(sim, 3),
            "failed_outcome": str(ri.get("outcome") or ""),
            "goal_failed": gi,
            "goal_worked": gj,
            "added": add,
            "failed_ts": float(ri.get("ts") or 0.0),
            "worked_ts": float(rj.get("ts") or 0.0),
        })
    found.sort(key=lambda p: -p["similarity"])
    return found


def recurring(found, min_jobs=2):
    """Added segments that show up across SEVERAL different jobs.

    One pair is an anecdote -- a thousand things differ between two runs and one of them happens
    to be written down. The same addition made to unrelated work, each time ahead of a success,
    is the closest thing this ledger offers to evidence. Clusters are keyed by content words so
    a rephrasing counts as the same lesson; the quoted text kept is the SHORTEST member, because
    the short form is the one that says the thing without the job around it.
    """
    clusters = []
    for p in found:
        for seg in p["added"]:
            w = words(seg)
            if len(w) < 3:
                continue
            for c in clusters:
                if similarity(w, c["words"]) >= 0.6:
                    c["jobs"].add(p["goal_worked"][:120])
                    c["members"].append(seg)
                    if len(seg) < len(c["text"]):
                        c["text"] = seg
                    break
            else:
                clusters.append({"words": w, "text": seg, "members": [seg],
                                 "jobs": {p["goal_worked"][:120]}})
    out = [{"text": c["text"], "jobs": len(c["jobs"]), "times": len(c["members"])}
           for c in clusters if len(c["jobs"]) >= min_jobs]
    out.sort(key=lambda c: (-c["jobs"], -c["times"]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true", help="include benchmark traffic")
    ap.add_argument("--show", type=int, default=0, help="print both instructions of pair N")
    ap.add_argument("--min-similarity", type=float, default=MIN_SIMILARITY)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    found = pairs(include_benchmarks=args.all, min_similarity=args.min_similarity)
    if args.json:
        print(json.dumps({"pairs": found, "recurring": recurring(found)},
                         ensure_ascii=False, default=str))
        return 0

    if args.show:
        i = args.show - 1
        if i < 0 or i >= len(found):
            print("no such pair (1..%d)" % len(found))
            return 2
        p = found[i]
        print("same work at similarity %.2f -- disagree with this pairing if it is wrong\n"
              % p["similarity"])
        print("failed (%s):\n%s\n" % (p["failed_outcome"], p["goal_failed"]))
        print("worked:\n%s\n" % p["goal_worked"])
        print("the working instruction carried, and the failing one did not:")
        for seg in p["added"]:
            print("  - %s" % seg.replace("\n", " "))
        return 0

    rec = recurring(found)
    print("failure/success pairs at the same work, worded differently: %d" % len(found))
    print()
    print("ADDITIONS THAT RECUR ACROSS DIFFERENT JOBS (the lessons worth carrying):")
    if not rec:
        print("  none -- every difference was particular to its own job.")
    for c in rec:
        print("  %d jobs (%d times)  %s" % (c["jobs"], c["times"],
                                            c["text"][:150].replace("\n", " ")))
    print()
    print("%-4s %-5s %-6s %s" % ("#", "sim", "added", "work that then finished"))
    for n, p in enumerate(found[:25], 1):
        print("%-4d %-5.2f %-6d %s"
              % (n, p["similarity"], len(p["added"]),
                 p["goal_worked"][:58].replace("\n", " ")))
    print()
    print("DONE is the worker's own word. A pair says one attempt finished and another did not, "
          "and quotes what the finishing instruction carried -- not that the answer was good.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""When the same goal is finished more than once, say where the answers disagree.

WHY THIS EXISTS. A worker that goes STUCK is retried, and the retry duplicates the goal without
ever reconciling what comes back. Measured on the 290-cinema survey of 2026-09-04: one goal was
completed FOUR times, and five of its eight subjects came back with conflicting verdicts --
one cinema had three different answers across the four workers. Nothing detected it. A person
had to open each transcript and decide which worker to believe.

The ironic part, from that run's own write-up: the worker that actually opened the URLs was
closer to right than the one that carefully concluded it could not reach them. "Did not look"
is not "did not get it wrong", and a system that silently keeps whichever answer finished last
cannot tell those apart. This module does not try to pick a winner -- picking one is exactly the
judgement that needs a human or a supervising agent. It finds the places where a choice is
required and says so.

    python -m relay.fleet_reconcile                      # newest run in .fleet/transcripts
    python -m relay.fleet_reconcile --run r6a9a3360
"""
from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import io
import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRANSCRIPTS = os.path.join(REPO, ".fleet", "transcripts")

#: Where a claim stops and its supporting evidence starts. Splitting here is what lets two
#: workers that AGREE on a verdict but cite different pages read as agreement rather than as a
#: difference of prose.
_EVIDENCE_SEPARATORS = ("—", "――", " -- ", " — ")

#: A line that states one subject's result. Four shapes, all seen in one real run:
#:   - **X: verdict** — evidence          (bulleted, verdict inside the bold)
#:   - **X**: verdict — evidence          (bulleted, verdict outside)
#:   1. **X: verdict** — evidence         (numbered)
#:   **X: verdict** — evidence            (NO marker at all)
#:
#: The last shape was missed by the first version, and the claim it dropped was the most
#: interesting one in the run: the only worker that said a cinema still HAD stock wrote it as a
#: bare bold line mid-paragraph, so the reconciler reported a two-way split on that subject when
#: the real disagreement was three ways.
#: THE BOLD IS THE REQUIREMENT, the list marker is optional. Requiring a marker missed the
#: unmarked shape; allowing anything would harvest ordinary prose, which is where workers
#: explain themselves and where a stray colon would invent a subject. Every real claim in the
#: measured run leads with bold, and no prose line does.
_CLAIM_LINE = re.compile(r"^\s*(?:[-*・]\s+|\d+[.)]\s+)?(\*\*.+)$")


def _strip_markup(s: str) -> str:
    return re.sub(r"[*_`]+", "", s or "").strip()


def _norm(s: str) -> str:
    """Comparison form: no spaces, no punctuation, full-width Latin folded to ASCII.

    Subjects arrive spelled differently by different workers -- "ユナイテッド・シネマ キャナル
    シティ13", "ユナイテッド・シネマキャナルシティ13", "UCキャナルシティ13" are one cinema, and
    an exact match would report three separate subjects that never disagree with each other.
    """
    s = _strip_markup(s)
    out = []
    for ch in s:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:          # full-width ASCII -> ASCII
            ch = chr(o - 0xFEE0)
        elif ch in "　 \t":
            continue
        if ch.isalnum() or ord(ch) > 0x2FFF:
            out.append(ch.lower())
    return "".join(out)


def _bigrams(s: str):
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def same_subject(a: str, b: str) -> bool:
    """Whether two spellings name the same thing.

    CONTAINMENT FIRST, because the common case is one worker writing the bare name and another
    prefixing the chain: "キャナルシティ13" against "ユナイテッド・シネマ キャナルシティ13" scores
    0.41 on bigrams -- below any threshold that does not also start merging different cinemas --
    while one is literally inside the other. Measured: that miss split a three-way disagreement
    into a two-way one and dropped the only worker who said the item was still in stock.

    The 4-character floor keeps a short fragment from swallowing everything it appears in.
    """
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 4 and short in long_:
        return True
    return similarity(a, b) >= SUBJECT_MATCH


def similarity(a: str, b: str) -> float:
    """Character-bigram Jaccard. Cheap, language-agnostic, and enough to fold the spellings
    the same cinema is written with; it is NOT trying to decide that two different cinemas are
    the same one, which is why the threshold below is high rather than clever."""
    A, B = _bigrams(_norm(a)), _bigrams(_norm(b))
    if not A or not B:
        return 0.0
    return len(A & B) / float(len(A | B))


SUBJECT_MATCH = 0.62


def extract_claims(text: str):
    """[(subject, verdict)] from one worker's final message.

    Only list items are read. Prose around them is where workers explain themselves, and it
    does not carry per-subject verdicts in any shape worth guessing at.
    """
    claims = []
    for raw in (text or "").splitlines():
        m = _CLAIM_LINE.match(raw)
        if not m:
            continue
        line = m.group(1)
        head = line
        for sep in _EVIDENCE_SEPARATORS:
            if sep in head:
                head = head.split(sep, 1)[0]
                break
        head = _strip_markup(head)
        # THE LAST COLON, NOT THE FIRST. A real subject carried one of its own --
        # "ユナイテッド・シネマ チャチャタウン小倉（運営: ローソン・ユナイテッドシネマ）: 配布終了"
        # -- and splitting at the first produced the subject "…小倉（運営" with the verdict
        # "ローソン・ユナイテッドシネマ）: 配布終了". That matched nothing any other worker wrote,
        # so the cinema silently vanished from the comparison instead of showing a conflict.
        # The verdict is a short label at the end; the subject is the part that may be ornate.
        parts = re.split(r"[:：](?!.*[:：])", head, 1)
        if len(parts) != 2:
            continue
        subject, verdict = parts[0].strip(), _strip_markup(parts[1]).strip()
        if subject and verdict:
            claims.append((subject, verdict))
    return claims


def load_completions(run=None, transcripts=TRANSCRIPTS):
    """{goal_key: [(worker, goal, final_message)]} for transcripts that reached a final message."""
    out = collections.defaultdict(list)
    pattern = os.path.join(transcripts, ("%s*" % run) if run else "*")
    for path in glob.glob(pattern + ".jsonl"):
        goal, last = None, None
        try:
            for ln in io.open(path, encoding="utf-8", errors="replace"):
                ln = ln.strip()
                if not ln:
                    continue
                row = json.loads(ln)
                if goal is None and row.get("goal"):
                    goal = str(row["goal"])
                if row.get("role") == "assistant":
                    last = str(row.get("text") or "")
        except Exception:
            continue
        if goal and last:
            key = hashlib.sha1(goal.encode("utf-8")).hexdigest()[:10]
            out[key].append((os.path.basename(path), goal, last))
    return out


def reconcile(completions):
    """Fold several completions of ONE goal into per-subject verdicts.

    Returns [(subject_as_first_seen, {verdict: [worker, ...]})], subjects in first-seen order.
    """
    subjects = []                      # [(display, normalised)]
    table = collections.OrderedDict()  # display -> verdict -> [workers]
    for worker, _goal, text in completions:
        for subject, verdict in extract_claims(text):
            match = None
            for display, _n in subjects:
                if same_subject(display, subject):
                    match = display
                    break
            if match is None:
                match = subject
                subjects.append((subject, _norm(subject)))
                table[match] = collections.OrderedDict()
            table[match].setdefault(verdict, [])
            if worker not in table[match][verdict]:
                table[match][verdict].append(worker)
    return [(s, table[s]) for s, _n in subjects]


def lone_subjects(rows, total_completions):
    """Subjects only ONE completion mentions.

    Not a disagreement, and not nothing. Either the other workers never covered the subject, or
    they named it so differently that nothing here could fold the spellings -- measured on the
    real run with one cinema written both as "ローソン・ユナイテッドシネマ小倉" and as
    "ユナイテッド・シネマ チャチャタウン小倉（運営: ローソン・ユナイテッドシネマ）", which share
    no containment and little else. Reporting the pair as unmatched is honest; quietly matching
    them on a lower threshold would start merging genuinely different subjects.
    """
    if total_completions < 2:
        return []
    out = []
    for subject, verdicts in rows:
        workers = {w for ws in verdicts.values() for w in ws}
        if len(workers) == 1:
            out.append((subject, verdicts))
    return out


def disagreements(rows):
    """Only the subjects whose verdicts are not unanimous -- the ones needing a decision."""
    out = []
    for subject, verdicts in rows:
        distinct = {_norm(v) for v in verdicts}
        if len(distinct) > 1:
            out.append((subject, verdicts))
    return out


def report(run=None, transcripts=TRANSCRIPTS):
    lines = []
    everything = load_completions(run, transcripts)
    repeated = {k: v for k, v in everything.items() if len(v) > 1}
    lines.append("goals with a final answer: %d   completed more than once: %d"
                 % (len(everything), len(repeated)))
    if not repeated:
        lines.append("")
        lines.append("Nothing to reconcile: no goal was finished twice.")
        return "\n".join(lines)

    for key, completions in repeated.items():
        rows = reconcile(completions)
        conflicts = disagreements(rows)
        lines.append("")
        lines.append("=" * 78)
        lines.append("goal %s -- finished %d times by: %s"
                     % (key, len(completions), ", ".join(w for w, _g, _t in completions)))
        lines.append("  %s" % (completions[0][1][:70].replace("\n", " ")))
        lines.append("  subjects: %d   DISAGREEING: %d" % (len(rows), len(conflicts)))
        if not conflicts:
            continue
        lines.append("")
        for subject, verdicts in conflicts:
            lines.append("  %s" % subject)
            for verdict, workers in verdicts.items():
                lines.append("      %-14s <- %s" % (verdict, ", ".join(workers)))
        lines.append("")
        lone = lone_subjects(rows, len(completions))
        if lone:
            lines.append("  mentioned by only ONE of the %d completions -- missed, or named"
                         % len(completions))
            lines.append("  differently enough that the spellings could not be folded:")
            for subject, verdicts in lone:
                only = ", ".join(sorted({w for ws in verdicts.values() for w in ws}))
                lines.append("      %-42s %s  (%s)"
                             % (subject[:42], "/".join(verdicts.keys()), only))
            lines.append("")
        lines.append("  A duplicate completion is not a second opinion that can be averaged.")
        lines.append("  Whichever finished last is the one the ledger keeps, and nothing above")
        lines.append("  says it is the right one -- these need a decision, not a tiebreak.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="relay.fleet_reconcile",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--run", default=None, help="run id prefix, e.g. r6a9a3360")
    ap.add_argument("--transcripts", default=TRANSCRIPTS)
    a = ap.parse_args(argv)
    print(report(a.run, a.transcripts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Measure a Skill matcher against cases it must hit AND cases it must refuse.

Both consultants said the same thing about how to do this safely: the danger in removing
dilution is that EVERY score rises, so a query that used to fall safely between two skills
can start landing on the wrong one. A positives-only fixture would show only the good half.

So the set below is half negatives. `None` means the matcher must return nothing.

    python scripts/win/skill_match_bench.py           # score the live library
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from relay.skills import SkillStore, _match_tokens   # noqa: E402

#: (query, expected skill name or None). The positives are the phrasings a person actually
#: types; the two that carry a date are the ones the current matcher misses. The negatives
#: are the ones that must keep returning nothing however the scoring changes -- including
#: near-misses that share vocabulary with a skill without asking for what it does.
CASES = [
    # --- must match /mail-lookup ---
    ("メールを検索したい", "mail-lookup"),
    ("受信メールの一覧を作って", "mail-lookup"),
    ("1月から3月の受信メールを調べて", "mail-lookup"),
    ("送信済みメールを日付順に一覧化して", "mail-lookup"),
    ("2026年1月のメールを検索して一覧にしたい", "mail-lookup"),
    ("先月のメールを一覧にして", "mail-lookup"),
    ("4月の送信済みメールを宛先付きで一覧にしてください", "mail-lookup"),
    # --- must match another skill ---
    ("銅箔の保証期限超過ロットを調べたい", "copper-foil-survey"),
    ("MT18EX5 RM の期限切れロットを調査して散布図を出して", "copper-foil-survey"),
    # --- must match NOTHING ---
    ("この関数をリファクタリングして", None),
    ("PowerPointの色を変えたい", None),
    ("2026年の売上を集計して", None),
    ("先月の請求書を作って", None),                 # date + a noun no skill covers
    ("メールサーバーの障害原因を調べて", None),      # shares メール, asks something else
    ("銅箔の価格推移をグラフにして", None),          # shares 銅箔, asks something else
    ("会議室を予約して", None),
]


def evaluate(store, scorer):
    """Run every case through `scorer(query) -> (name|None, score, runner_up)`."""
    rows, hits, misses, false_hits = [], 0, 0, 0
    for query, expected in CASES:
        name, score, second = scorer(query)
        ok = (name == expected)
        if expected is None:
            if ok:
                hits += 1
            else:
                false_hits += 1
        else:
            if ok:
                hits += 1
            else:
                misses += 1
        rows.append((ok, query, expected, name, score, second))
    return rows, hits, misses, false_hits


def current_scorer(store):
    def score(query):
        best = store.match(query)
        if best:
            return best["name"], best.get("score", 0.0), 0.0
        return None, 0.0, 0.0
    return score


def main():
    store = SkillStore(REPO)
    which = next((a[2:] for a in sys.argv[1:] if a.startswith("--")), "current")
    scorer = {"candidate": candidate_scorer, "fable": fable_scorer, "both": both_scorer}.get(which, current_scorer)(store)
    print("scorer: " + which)
    rows, hits, misses, false_hits = evaluate(store, scorer)
    print("%-3s %-42s %-20s %-20s" % ("", "query", "expected", "got"))
    for ok, query, expected, name, sc, _ in rows:
        print("%-3s %-42s %-20s %-20s %s"
              % ("ok" if ok else "XX", query[:40], expected or "(nothing)",
                 name or "(nothing)", ("%.3f" % sc) if sc else ""))
    total = len(CASES)
    print("\n%d/%d correct   misses=%d   WRONG MATCHES=%d"
          % (hits, total, misses, false_hits))
    print("a wrong match is the expensive one: the agent follows a procedure meant for "
          "something else and presents it as the user's own.")
    return 0




# ---------------------------------------------------------------------------------------
# CANDIDATE. Both consultants diagnosed the same cause -- a query token that matches nothing
# still enlarges the denominator, so adding a date lowers the score -- and proposed different
# cures. codex: drop the ratio, sum IDF over matched tokens, judge against an absolute
# evidence threshold, since unmatched terms then contribute zero instead of negative. fable:
# segment CJK runs on script boundaries and discard short hiragana pieces, since the diluting
# bigrams all straddle into hiragana.
#
# Each names the other's weakness, and both are right to. A stop-list of morphemes is
# open-ended and would also delete real distinctions (送信済み loses 済み, which is what
# separates sent from unsent). An absolute IDF threshold rescales everything, so the 0.55 bar
# and the 0.15 margin would both need recalibrating against a library of six skills.
#
# What survives from both is the principle, and it can be had without either cost: divide by
# the query tokens the LIBRARY KNOWS ABOUT rather than by all of them. A date appears in no
# skill's metadata, so it leaves the denominator instead of inflating it -- codex's "zero, not
# negative" -- while the score stays a 0-1 fraction, so the existing thresholds keep meaning
# what they meant.
#
# fable's objection to exactly this shape is the one to watch: with the distinguishing tokens
# gone from the denominator, a query about something the library does not cover can match a
# skill on whatever generic vocabulary is left. That is what the negative cases above are
# for, and why a match must rest on at least two distinct tokens rather than one shared bigram.

def library_vocabulary(store):
    vocab = set()
    for skill in store.discover():
        hay = skill.description + " " + str(skill.metadata.get("when_to_use") or "")
        vocab |= _match_tokens(hay)
    return vocab


def candidate_scorer(store, bar=0.55, margin=0.15, min_tokens=2):
    vocab = library_vocabulary(store)
    skills = [s for s in store.discover()
              if s.metadata.get("disable-model-invocation") is not True]

    def score(query):
        q = _match_tokens(query)
        known = q & vocab                      # what the library has any word for
        if len(known) < min_tokens:
            return None, 0.0, 0.0
        scored = []
        for s in skills:
            hay = s.description + " " + str(s.metadata.get("when_to_use") or "")
            terms = _match_tokens(hay)
            overlap = q & terms
            if len(overlap) < min_tokens:      # one shared bigram is not evidence
                continue
            sc = len(overlap) / max(1, len(known))
            if s.name in query.lower():
                sc += 0.6
            scored.append((sc, s.name))
        if not scored:
            return None, 0.0, 0.0
        scored.sort(reverse=True)
        best, second = scored[0][0], (scored[1][0] if len(scored) > 1 else 0.0)
        if best < bar or (best - second < margin and best < 1.0):
            return None, best, second
        return scored[0][1], best, second

    return score




# ---------------------------------------------------------------------------------------
# fable's proposal, measured on the same set. Segment each CJK run on script boundaries
# (kanji / katakana / hiragana), discard hiragana segments of two characters or fewer plus a
# short stop-list of longer ones, then bigram what remains. The claim is that the diluting
# tokens all straddle a boundary into hiragana, so removing hiragana segments removes them
# without a list of the bigrams themselves.
#
# The denominator stays min(|query|,|haystack|) and the bar stays 0.55: this cures dilution
# by producing fewer, cleaner tokens rather than by changing what is divided by.

_HIRAGANA_STOP = ("したい", "ください", "ほしい", "について", "ように", "しています",
                  "しました", "できる", "できます", "ですか", "でしょうか", "して",
                  "たい", "ます", "です")


def _script_of(ch):
    o = ord(ch)
    if 0x3040 <= o <= 0x309F:
        return "hira"
    if 0x30A0 <= o <= 0x30FF:
        return "kata"
    return "han"


def segmented_tokens(text):
    import re as _re
    lowered = (text or "").lower()
    words = set(_re.findall(r"[a-z0-9][a-z0-9_-]{2,}", lowered))
    for run in _re.findall(r"[぀-ヿ㐀-鿿]{2,}", lowered):
        seg, script = "", None
        pieces = []
        for ch in run:
            sc = _script_of(ch)
            if sc != script and seg:
                pieces.append((script, seg))
                seg = ""
            script, seg = sc, seg + ch
        if seg:
            pieces.append((script, seg))
        for sc, piece in pieces:
            if sc == "hira" and (len(piece) <= 2 or piece in _HIRAGANA_STOP):
                continue
            if len(piece) < 2:
                continue
            words.update(piece[i:i + 2] for i in range(len(piece) - 1))
    return words


def fable_scorer(store, bar=0.55, margin=0.15):
    skills = [s for s in store.discover()
              if s.metadata.get("disable-model-invocation") is not True]

    def score(query):
        q = segmented_tokens(query)
        if len(q) < 2:
            return None, 0.0, 0.0
        scored = []
        for s in skills:
            hay = s.description + " " + str(s.metadata.get("when_to_use") or "")
            terms = segmented_tokens(hay)
            if not terms:
                continue
            sc = len(q & terms) / max(1, min(len(q), len(terms)))
            if s.name in query.lower():
                sc += 0.6
            scored.append((sc, s.name))
        if not scored:
            return None, 0.0, 0.0
        scored.sort(reverse=True)
        best, second = scored[0][0], (scored[1][0] if len(scored) > 1 else 0.0)
        if best < bar or (best - second < margin and best < 1.0):
            return None, best, second
        return scored[0][1], best, second

    return score



# ---------------------------------------------------------------------------------------
# BOTH, measured. fable's segmentation produces cleaner tokens (its correct matches rose from
# 0.625 to 1.000) but left the date queries at 0.500 -- below the bar -- because the date
# fragments that survive still sit in the denominator. codex's denominator removes exactly
# those, and on the raw tokeniser it fell open on three negatives. Together the cleaner tokens
# should give the library-vocabulary denominator less generic material to fail open on.
#
# min_tokens is 3 here rather than 2: the fail-opens all rested on a single shared word --
# 銅箔 in a query about prices, メール in one about a mail server -- and two bigrams of one
# word are not two pieces of evidence.

def both_scorer(store, bar=0.55, margin=0.15, min_tokens=3):
    skills = [s for s in store.discover()
              if s.metadata.get("disable-model-invocation") is not True]
    vocab = set()
    for s in skills:
        vocab |= segmented_tokens(s.description + " " + str(s.metadata.get("when_to_use") or ""))

    def score(query):
        q = segmented_tokens(query)
        known = q & vocab
        if len(known) < min_tokens:
            return None, 0.0, 0.0
        scored = []
        for s in skills:
            hay = s.description + " " + str(s.metadata.get("when_to_use") or "")
            overlap = q & segmented_tokens(hay)
            if len(overlap) < min_tokens:
                continue
            sc = len(overlap) / max(1, len(known))
            if s.name in query.lower():
                sc += 0.6
            scored.append((sc, s.name))
        if not scored:
            return None, 0.0, 0.0
        scored.sort(reverse=True)
        best, second = scored[0][0], (scored[1][0] if len(scored) > 1 else 0.0)
        if best < bar or (best - second < margin and best < 1.0):
            return None, best, second
        return scored[0][1], best, second

    return score


if __name__ == "__main__":
    sys.exit(main())

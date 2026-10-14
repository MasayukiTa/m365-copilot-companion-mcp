# -*- coding: utf-8 -*-
"""Find the defined terms and metrics in a request, and name the words that are NOT defined.

The point of the semantic layer is that a request word such as "defect" is looked up before any
SQL is written. If the dictionary has no entry, the honest answer is "undefined" -- this module
returns such words in ``undefined`` and never guesses a definition for them.

Matching is deliberately simple (no morphological analyser):

* text is NFKC-normalised and case-folded, so full-width / half-width and case variants agree;
* English words are compared after a light plural / -ing / -ed stem;
* Japanese is compared as substrings, and by character bigrams for partial matches;
* a match is ``strong`` (the defined word, or a synonym, occurs in the request) or ``weak``
  (the request word is only *part of* a defined word, or only similar to it). Only strong
  matches count as "defined". Weak ones are offered as ``near`` suggestions.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import dates
from ._text import (Run, content_runs, dice, is_pure_latin, latin_words, norm, script_runs,
                    stem)
from .store import Store, get_store

# Generic request words that are never business terms. They only suppress the "undefined"
# report; a dictionary entry with the same spelling still matches normally.
STOPWORDS_JA = frozenset("""
データ 集計 一覧 表示 取得 抽出 検索 結果 合計 平均 件数 最大 最小 推移 比較 分析 確認 全部 全て
すべて 今回 以下 以上 以内 過去 直近 最近 期間 対象 出力 作成 グラフ 表 リスト レポート 教 調 見
月 日 年 週 毎 別 ごと 当たり あたり 内訳 ランキング 上位 下位 全体 現在 最新
""".split())
STOPWORDS_EN = frozenset("""
the and of for by in on to a an show get list give me data total count sum average avg per over
last past this month week year day days from with all report summary aggregate aggregation
please what is are how many much trend top bottom between each
""".split())

MIN_SCORE = 0.45


def _strong_scan(cand: str, text: str, runs: List[Run], words: List[Run]
                 ) -> Optional[Tuple[float, int, int]]:
    """Does `cand` (normalised) occur in `text`? -> (score, start, end) of the best occurrence."""
    if not cand:
        return None
    if is_pure_latin(cand):
        cw = [stem(w) for w in cand.replace("-", " ").replace("_", " ").replace(".", " ").split()]
        if not cw:
            return None
        n = len(cw)
        for i in range(len(words) - n + 1):
            if [w.text for w in words[i:i + n]] == cw:
                return 1.0, words[i].start, words[i + n - 1].end
        return None
    if len(cand) == 1:
        for r in runs:
            if r.text == cand:
                return 1.0, r.start, r.end
        return None
    best = None
    pos = text.find(cand)
    while pos != -1:
        end = pos + len(cand)
        host = next((r for r in runs if r.start <= pos and end <= r.end), None)
        if host is None or (pos == host.start and end == host.end):
            score = 1.0
        else:
            score = 0.75 + 0.25 * len(cand) / len(host.text)
        if best is None or score > best[0]:
            best = (score, pos, end)
        pos = text.find(cand, pos + 1)
    return best


def _weak_scan(cand: str, runs: List[Run]) -> Optional[Tuple[float, str]]:
    """Request word only part of / similar to the defined word -> (score, request_word)."""
    best = None
    for r in runs:
        if r.kind not in ("han", "kata") or len(r.text) < 2 or len(cand) < 2:
            continue
        if r.text in cand and r.text != cand:
            sc = 0.45 + 0.35 * len(r.text) / len(cand)
            kind = "partial"
        elif min(len(r.text), len(cand)) >= 3 and dice(r.text, cand) >= 0.5:
            sc = 0.3 + 0.3 * dice(r.text, cand)
            kind = "fuzzy"
        else:
            continue
        if best is None or sc > best[0]:
            best = (sc, r.text, kind)
    return best


def _candidates(entry: Dict[str, Any], main_key: str) -> List[str]:
    out = [entry[main_key]] + list(entry.get("synonyms", []))
    seen, res = set(), []
    for c in out:
        n = norm(c)
        if n and n not in seen:
            seen.add(n)
            res.append(n)
    return res


def _match_entry(entry: Dict[str, Any], main_key: str, text: str, runs: List[Run],
                 words: List[Run]) -> Optional[Dict[str, Any]]:
    best = None
    for cand in _candidates(entry, main_key):
        s = _strong_scan(cand, text, runs, words)
        if s is not None:
            if best is None or not best["strong"] or s[0] > best["score"]:
                best = {"strong": True, "score": s[0], "kind": "exact" if s[0] == 1.0
                        else "substring", "cand": cand, "span": (s[1], s[2])}
    if best is not None:
        return best
    for cand in _candidates(entry, main_key):
        w = _weak_scan(cand, runs)
        if w is not None and (best is None or w[0] > best["score"]):
            best = {"strong": False, "score": w[0], "kind": w[2], "cand": cand,
                    "request_word": w[1], "span": None}
    return best


def resolve(text: str, store: Optional[Store] = None, today=None,
            min_score: float = MIN_SCORE, limit: int = 12,
            stopwords: Optional[List[str]] = None) -> Dict[str, Any]:
    """Look the request up in the dictionary.

    Returns a dict with: ok, configured, request, matches (terms and metrics, best first),
    undefined (words with no entry -- do NOT define them yourself), periods (relative dates
    found, resolved against `today`), ambiguous (one wording claimed by several entries) and
    warnings. ``ok`` is False only when the store is unusable (not configured / bad folder).
    """
    st = store if store is not None else get_store()
    err = st.error()
    if err:
        return {"ok": False, "configured": st.configured, "error": err, "request": text,
                "matches": [], "undefined": None, "periods": []}

    t_raw = norm(text)
    periods = dates.find_periods(t_raw, today)
    t = dates.mask_periods(t_raw, periods)
    runs = content_runs(t)
    words = latin_words(t)

    matches: List[Dict[str, Any]] = []
    covered: List[Tuple[int, int]] = []
    claim: Dict[Tuple[int, int], List[str]] = {}

    def add(kind: str, entry: Dict[str, Any], key: str) -> None:
        m = _match_entry(entry, key, t, runs, words)
        if m is None or m["score"] < min_score:
            return
        rec: Dict[str, Any] = {
            "type": kind, "name": entry[key], "score": round(m["score"], 3),
            "match": m["kind"], "defined": bool(m["strong"]),
            "matched_text": m["cand"], "status": entry.get("status", "draft"),
            "caveats": entry.get("caveats", []),
        }
        if kind == "term":
            rec["meaning"] = entry.get("meaning", "")
            rec["resolves_to"] = entry.get("resolves_to")
        else:
            rec["description"] = entry.get("description", "")
            rec["dimensions"] = entry.get("dimensions", [])
            rec["tables"] = entry.get("tables", [])
            rec["params"] = [p["name"] for p in entry.get("params", [])]
        if not m["strong"]:
            rec["request_word"] = m["request_word"]
            rec["note"] = ("only part of / similar to the defined word; confirm before using, "
                           "the request word itself is still undefined")
        else:
            covered.append(m["span"])
            claim.setdefault(m["span"], []).append("%s:%s" % (kind, entry[key]))
        matches.append(rec)

    for e in st.terms:
        add("term", e, "term")
    for e in st.metrics:
        add("metric", e, "name")

    matches.sort(key=lambda r: (-r["score"], 0 if r["status"] == "certified" else 1,
                                r["type"], r["name"]))
    matches = matches[:max(1, int(limit))]

    # ---- undefined words: content runs left after removing strong matches and stopwords
    extra = {norm(w) for w in (stopwords or [])}
    stop = STOPWORDS_JA | STOPWORDS_EN | extra
    near_by_word: Dict[str, List[str]] = {}
    for m in matches:
        if not m["defined"]:
            near_by_word.setdefault(m["request_word"], []).append(m["name"])
    undefined: List[Dict[str, Any]] = []
    for r in runs:
        pieces = _remaining(r, covered)
        for piece in pieces:
            w = piece.text
            if r.kind == "latin":
                if stem(w) in stop or w in stop or w.isdigit():
                    continue
            else:
                if w in stop or len(w) < 2:
                    continue
                if _is_all_stop(w, stop):
                    continue
            rec = {"word": w}
            if w in near_by_word:
                rec["near"] = near_by_word[w]
            if rec not in undefined:
                undefined.append(rec)

    ambiguous = [{"wording": t[s:e], "claimed_by": names}
                 for (s, e), names in claim.items() if len(names) > 1]
    warnings: List[str] = []
    if undefined:
        warnings.append("Words with no dictionary entry are listed in 'undefined'. Do not "
                        "invent a definition: ask the owner, or add the term to the glossary.")
    drafts = [m["name"] for m in matches if m["defined"] and m["status"] != "certified"]
    if drafts:
        warnings.append("Not certified (draft): %s. Treat these definitions as unverified."
                        % ", ".join(drafts))
    errs = st.problem_dicts("error")
    if errs:
        warnings.append("%d dictionary entr%s could not be loaded; see 'store_problems'."
                        % (len(errs), "y" if len(errs) == 1 else "ies"))
    out = {
        "ok": True, "configured": True, "request": text, "matches": matches,
        "undefined": undefined, "periods": [p.as_dict() for p in periods],
        "ambiguous": ambiguous, "warnings": warnings,
        "store": st.status(),
    }
    if st.problems:
        out["store_problems"] = st.problem_dicts()
    return out


def _is_all_stop(word: str, stop) -> bool:
    """A kanji/katakana run made only of stop words ("集計データ" -> 集計 + データ)."""
    i = 0
    while i < len(word):
        for j in range(len(word), i, -1):
            if word[i:j] in stop:
                i = j
                break
        else:
            return False
    return True


def _remaining(run: Run, covered: List[Tuple[int, int]]) -> List[Run]:
    """The parts of `run` not covered by any strong match."""
    cuts = sorted((max(s, run.start), min(e, run.end)) for s, e in covered
                  if s < run.end and run.start < e)
    pieces: List[Run] = []
    pos = run.start
    for s, e in cuts:
        if s > pos:
            pieces.append(Run(run.kind, run.text[pos - run.start:s - run.start], pos, s))
        pos = max(pos, e)
    if pos < run.end:
        pieces.append(Run(run.kind, run.text[pos - run.start:run.end - run.start], pos, run.end))
    return pieces

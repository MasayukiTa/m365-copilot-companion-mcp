"""Question -> stored example matching, with an explicit trust label.

Similarity = 0.6 * Dice over character bigrams + 0.4 * Dice over content words, both on the
normalised stream from textnorm (full/half width folded, relative periods / dates / numbers
replaced by shared placeholders). Everything is standard library.
"""
from __future__ import annotations

from . import store
from .textnorm import ngrams, normalize, words

DEFAULT_THRESHOLD = 0.5

#: The four outcomes a caller is told about, best first.
LABELS = {
    "certified": "certified",
    "validated": "validated (auto-checked: it ran and re-ran identically; business logic "
                 "NOT confirmed)",
    "draft": "draft (unverified)",
    "none": "no match",
}
_RANK = {"certified": 3, "validated": 2, "draft": 1, "deprecated": 0}


def _dice(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return 2.0 * len(a & b) / (len(a) + len(b))


def similarity(a_norm: str, b_norm: str) -> float:
    if not a_norm or not b_norm:
        return 0.0
    bi = _dice(ngrams(a_norm, 2), ngrams(b_norm, 2))
    wa, wb = set(words(a_norm)), set(words(b_norm))
    if wa and wb:
        return 0.6 * bi + 0.4 * _dice(wa, wb)
    return bi


def match(question: str, limit: int = 5, threshold: float | None = None,
          include_deprecated: bool = False) -> dict:
    """Return {"verdict", "label", "matches": [...]}.

    verdict is the best tier among matches at or above the threshold: certified, validated,
    draft, or none. Deprecated entries are never returned unless include_deprecated is set,
    and they never raise the verdict.
    """
    thr = DEFAULT_THRESHOLD if threshold is None else float(threshold)
    qn = normalize(question)
    scored = []
    for e in store.list_entries():
        if e["status"] == "deprecated" and not include_deprecated:
            continue
        s = similarity(qn, e["question_norm"])
        if s >= thr:
            scored.append((s, e))
    scored.sort(key=lambda t: (-_RANK[t[1]["status"]], -t[0], t[1]["id"]))
    matches = []
    for s, e in scored[:max(1, int(limit))]:
        matches.append({
            "id": e["id"], "score": round(s, 3), "status": e["status"],
            "label": LABELS.get(e["status"], e["status"]),
            "needs_review": e["needs_review"],
            "question": e["question"], "sql": e["sql"], "params": e["params"],
            "tables": e["tables"], "notes": e["notes"], "use_count": e["use_count"],
        })
    best = "none"
    for m in matches:
        if m["status"] != "deprecated" and _RANK[m["status"]] > _RANK.get(best, -1):
            best = m["status"]
    out = {"verdict": best, "label": LABELS[best], "threshold": thr, "matches": matches}
    if best in ("validated", "draft"):
        out["caution"] = store.DISCLAIMER
    return out

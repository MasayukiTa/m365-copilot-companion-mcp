"""How often the rule to consult a Skill first actually finds one.

THE RULE UNDER TEST. The server's own instructions tell every agent, before doing any domain
work, to call skill_match and follow a confident trusted match. That sentence costs a
round-trip on every task. Whether it earns it is an empirical question, and until skill_ops
began recording consultations there was no way to ask it -- the earlier assessment rested on
reading the sentence and reasoning about it.

WHAT THE RECORD CAN AND CANNOT SAY. The query text is deliberately not stored, only its
length and a short hash, so this cannot report WHICH requests missed. It can report the rate,
group repeated probes, and be re-run as the store grows, which is the point: a match rate is
only meaningful next to the size of the store it is matching against.
"""
from __future__ import annotations

import io
import json
import os
from collections import Counter

DEFAULT_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           ".fleet", "skill_use.jsonl")


def load(path=DEFAULT_LOG):
    rows = []
    if not os.path.exists(path):
        return rows
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def rate(rows, trusted_skills=None):
    """The measurement, with the denominator's shape stated beside it."""
    matches = [r for r in rows if r.get("kind") == "match"]
    hit = [r for r in matches if r.get("matched")]
    distinct = len({r.get("query_hash") for r in matches})
    return {
        "consultations": len(matches),
        "matched": len(hit),
        "match_rate": (len(hit) / len(matches)) if matches else None,
        "distinct_queries": distinct,
        # A rate over repeated probes of ONE question is not a rate over questions. When these
        # two are equal every consultation was a different request, which is the honest case.
        "consultations_are_distinct_questions": distinct == len(matches),
        "trusted_skills_available": trusted_skills,
        "which_skills_matched": dict(Counter(r.get("matched") for r in hit)),
        "reading": (
            "a low rate is not by itself a fault in the rule: a store that covers few of the "
            "questions asked will miss most of them, and the fix is more skills rather than "
            "fewer consultations. It IS a fault in any claim that consulting first usually "
            "helps."),
    }


def _main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=DEFAULT_LOG)
    a = ap.parse_args()
    trusted = None
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from tools.skill_ops import skill_list
        items = json.loads(skill_list())
        trusted = sum(1 for x in items if x.get("trust") == "trusted")
    except Exception:
        pass
    print(json.dumps(rate(load(a.log), trusted), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

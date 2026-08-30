"""Best-of-N ORCHESTRATION GLUE -- from "N captured prediction records" to "one ship/abstain decision".

This is the pure, testable bridge that composes the two already-built pieces of Bet #1 / Bet #2 in
bench/AGENT_STRENGTHS.md:

  * relay.bestofn   (the SELECTOR)    -- picks the winning patch among N solves of the SAME task.
  * relay.confidence (CALIBRATION)    -- turns the selection into a confidence + abstain/escalate call.

Both are imported READ-ONLY. The actual N parallel solves are fleet-heavy and come later; THIS module
operates only on N *captured prediction records* of the same instance -- the JSON the solve pipeline
already writes, one file per attempt:

    [{"instance_id": "...", "model_patch": "<unified diff>", "model_name_or_path": "companion"}]

(a list with exactly one dict). Best-of-N produces N such captures of the SAME instance_id, optionally
each carrying per-attempt signals (self-test result, refuter votes) that may or may not be present yet.

Three functions, all pure/deterministic except the directory loader (which only reads the passed dir):

  1. candidates_from_preds(pred_records)   -- prediction records -> bestofn candidate dicts.
  2. decide(pred_records, *, weights=None)  -- the one ship/abstain call (composes selector+confidence).
  3. load_candidate_dir(dir_path)           -- read N one-element-list captures from a directory.

No network, no subprocess, no real diffing. stdlib only.
"""
from __future__ import annotations

import json
import os
from typing import Iterable

from relay import bestofn
from relay import confidence


# --------------------------------------------------------------------------------------------------
# 1. prediction records -> selector candidates
# --------------------------------------------------------------------------------------------------

def candidates_from_preds(pred_records: Iterable[dict]) -> list[dict]:
    """Map N attempt prediction records (for the SAME task) to bestofn candidate dicts.

    Each input record looks like one attempt:
        {"instance_id", "model_patch" (or "diff"),
         "selftest_passed" (opt), "refuter_refuted" (opt), "refuter_total" (opt)}

    Each output candidate is the shape relay.bestofn expects:
        {"idx", "diff", "selftest_passed", "refuter_refuted", "refuter_total"}

    Defensive + deterministic:
      * "idx" is the position in input order (0..N-1) so selection is reproducible.
      * a missing/empty patch -> "" (an empty candidate the selector floors).
      * "selftest_passed" defaults to None (unknown), refuter counts default to 0.
      * non-dict entries are skipped (they still consume an idx slot so surviving candidates keep
        their original input position -- order is what carries the winner back to its pred record).
    """
    out: list[dict] = []
    for idx, rec in enumerate(pred_records):
        if not isinstance(rec, dict):
            continue
        # patch under either key; missing/None -> "" (an empty candidate, floored by the selector).
        diff = rec.get("model_patch")
        if diff is None:
            diff = rec.get("diff")
        if not isinstance(diff, str):
            diff = ""

        out.append({
            "idx": idx,
            "diff": diff,
            "selftest_passed": rec.get("selftest_passed", None),
            "refuter_refuted": rec.get("refuter_refuted", 0) or 0,
            "refuter_total": rec.get("refuter_total", 0) or 0,
        })
    return out


# --------------------------------------------------------------------------------------------------
# 2. the one ship/abstain decision
# --------------------------------------------------------------------------------------------------

def _winner_instance_id(pred_records, winner_idx) -> str | None:
    """instance_id of the winning attempt. The N captures should share it; if they differ we still
    use the winner's. Falls back to any record's id, then None."""
    recs = list(pred_records)
    if winner_idx is not None and isinstance(winner_idx, int) and 0 <= winner_idx < len(recs):
        wr = recs[winner_idx]
        if isinstance(wr, dict):
            iid = wr.get("instance_id")
            if iid is not None:
                return iid
    # fall back: first record that carries one.
    for r in recs:
        if isinstance(r, dict) and r.get("instance_id") is not None:
            return r.get("instance_id")
    return None


def decide(pred_records, *, weights=None) -> dict:
    """Compose SELECTOR + CONFIDENCE into a single ship/abstain decision for N captures of one task.

    Pipeline:
      pred_records -> candidates_from_preds -> (bestofn.select_best AND confidence.task_confidence).

    `weights` (the *selector* weights) is forwarded to BOTH select_best (called here for the explain
    string) and task_confidence (which forwards it to select_best internally). Because both use the
    same selector weights they must agree on the winner; we verify that and, on the (unexpected)
    chance they disagree, prefer task_confidence's winner_idx and note it in `explain`.

    Returns:
        {
          "winner":      {"instance_id", "diff"} | None,  # the patch to ship (None if no candidates)
          "winner_idx":  int | None,
          "confidence":  float, "level": str, "abstain": bool, "escalate": bool,
          "n":           int,
          "ranking":     [...],   # from select_best (best -> worst)
          "explain":     str,     # confidence.explain(conf, sel)
        }

    Defensive: empty pred_records -> winner None, abstain True, escalate True, explain "no candidates".
    """
    recs = list(pred_records)
    cands = candidates_from_preds(recs)

    # THE STAIRCASE, so best-of-N appears in the mechanism funnel with the rest.
    #
    # It read as "never configured" for as long as the funnel existed, because nothing
    # reported here -- and the number that matters is not how often the selector RAN but how
    # often it was asked a real question. Three populations were tried before one produced
    # candidates that differed in CORRECTNESS: the effort arms agreed outright, an easy set
    # had every sample right, and only a hard set gave a single disagreeing instance. So
    # `eligible` is "more than one distinct candidate", which is the condition without which
    # any accuracy computed here measures the population instead of the selector.
    try:
        from relay import mechanism_telemetry as _mt
        _distinct = len({(c or {}).get("diff") for c in cands})
        _mt.record("bestofn", configured=True, config_source="caller",
                   config_value={"candidates": len(cands)},
                   eligible=(_distinct > 1),
                   ineligible_reason=("" if _distinct > 1 else
                                      "%d candidate(s), %d distinct: nothing to choose between"
                                      % (len(cands), _distinct)),
                   triggered=(_distinct > 1), executed=(_distinct > 1),
                   extra={"distinct_candidates": _distinct})
    except Exception:
        pass
    _tel_ctx = {"distinct": None}
    try:
        _tel_ctx["distinct"] = len({(c or {}).get("diff") for c in cands})
    except Exception:
        pass

    # No real candidates at all -> maximally humble, escalate.
    if not cands:
        return {
            "winner": None,
            "winner_idx": None,
            "confidence": 0.0,
            "level": "low",
            "abstain": True,
            "escalate": True,
            "n": 0,
            "ranking": [],
            "explain": "no candidates",
        }

    # Run the selector once ourselves (for the explain string + ranking), and the confidence policy
    # (which re-runs the selector internally with the same weights). Same weights => same winner.
    sel = bestofn.select_best(cands, weights=weights)
    conf = confidence.task_confidence(cands, weights=weights)

    sel_idx = (sel.get("winner") or {}).get("idx")
    conf_idx = conf.get("winner_idx")

    explain = confidence.explain(conf, sel)

    # They MUST agree on the winner (both use the same selector weights). If not, trust the confidence
    # module's view (it owns the abstain decision) and surface the disagreement transparently.
    winner_idx = conf_idx
    if sel_idx != conf_idx:
        explain = "%s [NOTE: selector idx %r != confidence idx %r; using confidence's]" % (
            explain, sel_idx, conf_idx,
        )

    # Build the shippable winner: instance_id from the winning pred record + its chosen diff.
    winner_obj = None
    if winner_idx is not None and isinstance(winner_idx, int) and 0 <= winner_idx < len(cands):
        winner_diff = cands[winner_idx].get("diff", "")
        winner_obj = {
            "instance_id": _winner_instance_id(recs, winner_idx),
            "diff": winner_diff,
        }

    # WHETHER IT ACTUALLY CHOSE. A selector handed candidates it cannot tell apart still
    # returns a winner, and counting that as a decision is how "the selector ran N times"
    # becomes "the selector helped N times". A changed decision here means it picked
    # something other than the first candidate, which is the only case where selecting was
    # not the same as taking whatever came first.
    try:
        from relay import mechanism_telemetry as _mt
        _d = _tel_ctx.get("distinct")
        if _d and _d > 1:
            _mt.record("bestofn", configured=True, config_source="caller",
                       eligible=True, triggered=True, executed=True,
                       changed_decision=(winner_idx not in (None, 0)),
                       before="candidate 0", after=("candidate %s" % winner_idx),
                       extra={"abstain": bool(conf.get("abstain")),
                              "confidence": conf.get("confidence")})
    except Exception:
        pass

    return {
        "winner": winner_obj,
        "winner_idx": winner_idx,
        "confidence": conf.get("confidence", 0.0),
        "level": conf.get("level", "low"),
        "abstain": bool(conf.get("abstain")),
        "escalate": confidence.should_escalate(conf),
        "n": len(cands),
        "ranking": sel.get("ranking", []),
        "explain": explain,
    }


# --------------------------------------------------------------------------------------------------
# 3. load N captures from a directory
# --------------------------------------------------------------------------------------------------

def load_candidate_dir(dir_path: str) -> list[dict]:
    """Read every *.json file in dir_path (each a one-element list of one attempt dict) and return the
    list of the inner attempt records.

    Read-only. Each file is expected to be the solve-pipeline capture shape:
        [{"instance_id": "...", "model_patch": "...", ...}]
    We return the inner dict from each. Missing dir / unreadable / malformed files are skipped (never
    raise). Sorted by filename for determinism, so the resulting attempt order is stable.

    This lets a caller point at a directory of N captures and feed the result straight to
    candidates_from_preds / decide.
    """
    out: list[dict] = []
    try:
        if not os.path.isdir(dir_path):
            return []
        names = sorted(
            n for n in os.listdir(dir_path)
            if n.lower().endswith(".json") and os.path.isfile(os.path.join(dir_path, n))
        )
    except OSError:
        return []

    for name in names:
        path = os.path.join(dir_path, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue  # unreadable / not JSON -> skip
        # Expected: a one-element list whose element is the attempt dict. Be tolerant of a bare dict.
        if isinstance(data, list):
            for elem in data:
                if isinstance(elem, dict):
                    out.append(elem)
        elif isinstance(data, dict):
            out.append(data)
        # anything else -> skip
    return out

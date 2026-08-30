"""Run the best-of-N selector over N samples of the same instances, against the grade.

THE CONDITION THIS FILE KEEPS CHECKING FOR. Best-of-N can only add value where the candidates
differ IN CORRECTNESS -- where at least one is right and at least one is wrong. Two earlier
attempts failed that condition in opposite ways and both are worth remembering:

  * the three EFFORT arms produced identical outcomes: same five resolved, same one failed;
  * three same-effort samples on an easy population produced six byte-different patches per
    instance and every one of them correct.

In both, the selector was handed a set with nothing to choose between, and any "accuracy"
computed over that measures the population rather than the selector. So this reports the size
of the population it was actually tested on, first, and treats a zero there as the finding.
"""
from __future__ import annotations

import glob
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay.bestofn_run import decide


def _load_preds(path):
    d = json.load(io.open(path, encoding="utf-8-sig"))
    rows = d.get("predictions") if isinstance(d, dict) else d
    return {r["instance_id"]: (r.get("patch") or "") for r in (rows or [])}


def run(directory):
    samples, verdicts = {}, {}
    for p in sorted(glob.glob(os.path.join(directory, "preds_*.json"))):
        k = os.path.basename(p).replace("preds_", "").replace(".json", "")
        got = _load_preds(p)
        if got:
            samples[k] = got
    for p in sorted(glob.glob(os.path.join(directory, "verdict_*.json"))):
        k = os.path.basename(p).replace("verdict_", "").replace(".json", "")
        verdicts[k] = json.load(io.open(p, encoding="utf-8-sig"))

    keys = sorted(set(samples) & set(verdicts))
    if not keys:
        return {"error": "no sample has both predictions and a verdict",
                "samples": sorted(samples), "verdicts": sorted(verdicts)}

    ids = sorted(set().union(*[set(samples[k]) for k in keys]))
    rows, mixed, picked_right, picked_wrong, none_right, all_right = [], 0, 0, 0, 0, 0
    for inst in ids:
        truth = {k: bool(verdicts[k].get(inst)) for k in keys}
        recs = [{"instance_id": inst, "model_patch": samples[k].get(inst, ""),
                 "model_name_or_path": k} for k in keys]
        res = decide(recs)
        idx = res.get("winner_idx")
        chosen = keys[idx] if idx is not None and idx < len(keys) else None
        vals = set(truth.values())
        if len(vals) > 1:
            mixed += 1
            if chosen is not None and truth.get(chosen):
                picked_right += 1
            else:
                picked_wrong += 1
        elif True in vals:
            all_right += 1
        else:
            none_right += 1
        rows.append({"instance_id": inst, "truth_by_sample": truth,
                     "samples_disagree_on_correctness": len(vals) > 1,
                     "chosen_sample": chosen,
                     "chosen_was_correct": truth.get(chosen) if chosen else None,
                     "confidence": res.get("confidence"), "abstain": res.get("abstain")})

    # What a single sample would have scored, so best-of-N has something to beat.
    per_sample = {k: sum(1 for i in ids if verdicts[k].get(i)) for k in keys}
    oracle = sum(1 for i in ids if any(verdicts[k].get(i) for k in keys))
    return {
        "samples": keys,
        "instances": len(ids),
        # THE DENOMINATOR THAT DECIDES WHETHER ANY OF THIS MEANS ANYTHING.
        "selection_was_tested_on": mixed,
        "chose_a_correct_patch": picked_right,
        "chose_a_wrong_one_when_a_correct_existed": picked_wrong,
        "every_sample_correct": all_right,
        "no_sample_correct": none_right,
        "resolved_per_sample": per_sample,
        "best_single_sample": max(per_sample.values()) if per_sample else 0,
        # The ceiling: what a perfect selector would score. If it equals the best single
        # sample, best-of-N has no headroom on this population at all.
        "oracle_upper_bound": oracle,
        "headroom_over_best_single": oracle - (max(per_sample.values()) if per_sample else 0),
        "detail": rows,
        "reading": (
            "selection_was_tested_on is the number of instances where the samples differed in "
            "correctness. Zero means the selector was never asked a question, whatever the "
            "other numbers say. headroom_over_best_single is what a perfect selector could add "
            "here; zero means best-of-N cannot help on this population even in principle."),
    }


def _main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    a = ap.parse_args()
    print(json.dumps(run(a.dir), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

"""Run the best-of-N selector over the three effort arms, and check it against the grade.

WHAT THIS CLOSES. relay/bestofn_run.py says in its own docstring that the N parallel solves
"are fleet-heavy and come later" -- the selector and the calibration were built and unit
tested, and had never been given N real solves of the same task. The effort A/B produced
exactly that: three independent solves of the same six instances, all graded.

WHAT IT CAN AND CANNOT SHOW ON THIS DATA. The three arms agreed completely -- same five
resolved, same one failed -- so there is no instance where the selector has to choose between
a correct and an incorrect candidate. That is a finding about the population, not about the
selector, and it is the honest headline: best-of-N cannot beat a single arm on a set where the
arms never disagree. What IS checkable here is that the selector ships when every candidate is
right and does not invent confidence where every candidate is wrong.
"""
from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay.bestofn_run import decide


def load_arm(path):
    d = json.load(io.open(path, encoding="utf-8-sig"))
    rows = d.get("predictions") if isinstance(d, dict) else d
    return {r["instance_id"]: r.get("patch") or "" for r in rows}


def run(arm_paths, verdict_paths):
    arms = {name: load_arm(p) for name, p in arm_paths.items()}
    verdicts = {name: json.load(io.open(p, encoding="utf-8-sig")) for name, p in verdict_paths.items()}
    ids = sorted(set().union(*[set(a) for a in arms.values()]))

    out = {"instances": [], "n_arms": len(arms)}
    agree = disagree = 0
    picked_correct = picked_wrong = no_correct_available = 0

    for inst in ids:
        truths = {name: bool(verdicts[name].get(inst)) for name in arms}
        # The selector's own input shape: one prediction record per candidate.
        recs = [{"instance_id": inst, "model_patch": arms[name].get(inst, ""),
                 "model_name_or_path": name} for name in sorted(arms)]
        res = decide(recs)
        idx = res.get("winner_idx")
        chosen_arm = sorted(arms)[idx] if idx is not None and idx < len(arms) else None
        any_correct = any(truths.values())
        all_same = len(set(truths.values())) == 1
        if all_same:
            agree += 1
        else:
            disagree += 1
        if not any_correct:
            no_correct_available += 1
        elif chosen_arm is not None and truths.get(chosen_arm):
            picked_correct += 1
        else:
            picked_wrong += 1
        out["instances"].append({
            "instance_id": inst,
            "truth_by_arm": truths,
            "arms_agree": all_same,
            "chosen_arm": chosen_arm,
            "chosen_was_correct": (truths.get(chosen_arm) if chosen_arm else None),
            "confidence": res.get("confidence"),
            "abstain": res.get("abstain"),
        })

    out["arms_agree"] = agree
    out["arms_disagree"] = disagree
    out["chose_a_correct_patch"] = picked_correct
    out["chose_a_wrong_patch_when_a_correct_one_existed"] = picked_wrong
    out["no_correct_candidate_existed"] = no_correct_available
    # THE DENOMINATOR THAT MATTERS, and the reason it can be zero.
    out["selection_was_tested_on"] = disagree
    out["reading"] = (
        "best-of-N can only add value where the candidates disagree. On %d of %d instances "
        "they agreed, so the selector had nothing to choose between and its accuracy here "
        "says nothing about it. A meaningful measurement needs a population where candidates "
        "differ in correctness." % (agree, agree + disagree))
    return out


def _main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ab = os.path.join(here, ".fleet", "swe", "ab")
    arms = {a: os.path.join(ab, "preds_%s.json" % a) for a in ("min", "auto", "ultra")}
    verds = {a: os.path.join(ab, "verdict_%s.json" % a) for a in ("min", "auto", "ultra")}
    missing = [p for p in list(arms.values()) + list(verds.values()) if not os.path.exists(p)]
    if missing:
        print("missing inputs:\n  " + "\n  ".join(missing))
        return 1
    print(json.dumps(run(arms, verds), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

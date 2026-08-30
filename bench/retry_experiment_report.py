"""Rescue and regression from two forced attempts on the same instances.

WHY IT HAD TO BE FORCED. A goal is normally retried only when the first attempt is detected as
failed, so the retried population is conditioned on failure and the un-retried one is where
false-DONE settles: measured, DONE is correct 71.8% of the time, and the single-attempt graded
rate was 42.9%. The mechanism with the strongest signal in this system has never fired where
it is most needed, because its trigger reads the self-report.

Forcing a second attempt regardless of what the first one claimed removes that conditioning.
What the two graded attempts then give is the pair the completion floor can never separate,
because both attempts report DONE either way:

    rescue      wrong at attempt 1, correct at attempt 2
    regression  correct at attempt 1, wrong at attempt 2
"""
from __future__ import annotations

import io
import json
import os


def run(directory):
    hashes_path = os.path.join(directory, "attempt_hashes.json")
    if not os.path.exists(hashes_path):
        return {"error": "no attempt_hashes.json; the per-attempt patches were not recorded"}
    hashes = json.load(io.open(hashes_path, encoding="utf-8-sig"))
    verdicts = {}
    for k in ("1", "2"):
        p = os.path.join(directory, "verdict_%s.json" % k)
        if os.path.exists(p):
            verdicts[k] = json.load(io.open(p, encoding="utf-8-sig"))
    if len(verdicts) < 2:
        return {"error": "both attempts must be graded before this can answer",
                "have": sorted(verdicts), "expected": ["1", "2"],
                "next_step": "scripts/win/grade_retry_experiment.ps1"}

    ids = sorted(set(hashes.get("1", {})) | set(hashes.get("2", {})))
    rescued, regressed, stable_ok, stable_bad, identical, ungradable = [], [], [], [], [], []
    for inst in ids:
        h1, h2 = hashes.get("1", {}).get(inst), hashes.get("2", {}).get(inst)
        if h1 and h2 and h1 == h2:
            # The second attempt reproduced the first byte for byte. Grading it twice answers
            # nothing, and counting it would inflate the denominator.
            identical.append(inst)
            continue
        v1, v2 = verdicts["1"].get(inst), verdicts["2"].get(inst)
        if v1 is None or v2 is None:
            ungradable.append(inst)
            continue
        if not v1 and v2:
            rescued.append(inst)
        elif v1 and not v2:
            regressed.append(inst)
        elif v1 and v2:
            stable_ok.append(inst)
        else:
            stable_bad.append(inst)

    considered = len(rescued) + len(regressed) + len(stable_ok) + len(stable_bad)
    a1 = sum(1 for i in ids if verdicts["1"].get(i))
    a2 = sum(1 for i in ids if verdicts["2"].get(i))
    return {
        "instances": len(ids),
        "resolved_attempt_1": a1,
        "resolved_attempt_2": a2,
        "attempts_identical": len(identical),
        "ungradable": len(ungradable),
        "considered": considered,
        "rescued": len(rescued),
        "regressed": len(regressed),
        "stable_correct": len(stable_ok),
        "stable_wrong": len(stable_bad),
        "net": len(rescued) - len(regressed),
        "rescued_ids": rescued,
        "regressed_ids": regressed,
        "reading": (
            "net is rescued minus regressed. A retry policy that rescues some and breaks "
            "others is not the rescue rate reported as a gain, and the completion floor "
            "cannot tell the two apart because both attempts report DONE. At this n the "
            "direction is worth having and the magnitude is not."),
    }


def _main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=".fleet/swe/retry_exp")
    a = ap.parse_args()
    print(json.dumps(run(a.dir), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

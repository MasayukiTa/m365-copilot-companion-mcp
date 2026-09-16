# -*- coding: utf-8 -*-
"""Measurement 2, part 2: score an answer against the frozen task, and say what it supports.

THE CRITERION IS CONTAINMENT, NOT DISTANCE. "Within N pixels of the centre" has no fixed
right N: a wide button tolerates being missed by twenty pixels, a small icon does not, and a
single threshold flatters whichever size happens to be common on the screen that was used.
What a click cares about is whether the point lands inside the region the click would be
delivered to. Distance from the centre is printed because it is useful for seeing HOW a
model is wrong, never as the pass mark.

IT REFUSES TO TURN A HANDFUL OF SUCCESSES INTO A CLAIM. With no failures, the one-sided 95%
lower bound is 0.05**(1/n): ten successes support "at least 74%", 299 support "at least 99%",
2995 support "at least 99.9%". The bound is printed next to the result, because months later
the sentence that survives is "grounding worked", and that sentence should not be reachable
from ten trials.

AND A PER-ACTION RATE IS NOT A PER-TASK RATE. Even at 99% per action, fifty actions all
succeeding is about 61%. That multiplication is printed too, so a good-looking number here is
never mistaken for a working end-to-end run -- which is the third measurement, not this one.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RESULTS = "answers.jsonl"


def _find(task, name):
    want = (name or "").strip()
    exact = [t for t in task["targets"] if t["name"] == want]
    if exact:
        return exact[0]
    loose = [t for t in task["targets"] if want and want in t["name"]]
    if len(loose) == 1:
        return loose[0]
    if len(loose) > 1:
        raise SystemExit("'%s' matches %d targets; name one exactly:\n  %s"
                         % (want, len(loose), "\n  ".join(t["name"] for t in loose)))
    raise SystemExit("no target called '%s'. Known:\n  %s"
                     % (want, "\n  ".join(t["name"] for t in task["targets"])))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("task", help="the task.json written by make_grounding_task")
    ap.add_argument("--target", required=True, help="which target was asked about")
    ap.add_argument("--answer", required=True,
                    help="the coordinates the model gave, as X,Y in IMAGE pixels")
    ap.add_argument("--by", default="", help="what answered (model / run id), for the record")
    args = ap.parse_args(argv)

    with open(args.task, encoding="utf-8") as fh:
        task = json.load(fh)
    t = _find(task, args.target)

    try:
        xs, ys = str(args.answer).replace("(", "").replace(")", "").split(",")[:2]
        ax, ay = float(xs), float(ys)
    except Exception:
        raise SystemExit("--answer must be X,Y in image pixels, e.g. --answer 812,431")

    x0, y0, x1, y1 = t["rect_image"]
    inside = (x0 <= ax < x1) and (y0 <= ay < y1)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    dist = max(abs(ax - cx), abs(ay - cy))

    print("task   : %s  (split=%s)" % (args.task, task.get("split")))
    print("target : %s  [%s]" % (t["name"], t["class"]))
    print("         rect in image px: %.0f,%.0f .. %.0f,%.0f  (%.0fx%.0f)"
          % (x0, y0, x1, y1, x1 - x0, y1 - y0))
    print("answer : %.0f,%.0f" % (ax, ay))
    print()
    print("VERDICT: %s" % ("INSIDE the target -- a click here would reach it"
                           if inside else
                           "OUTSIDE the target -- a click here would go somewhere else"))
    chance = t.get("chance")
    if chance is None:
        print("         THIS RESULT CANNOT BE USED. The task was built before the harness"
              " measured")
        print("         how often a blind answer lands inside, so INSIDE here says nothing"
              " about")
        print("         whether the picture was read. Rebuild the task and ask again.")
    else:
        print("         an answer picked without looking lands inside %.0f%% of the time,"
              % (100.0 * chance))
        print("         so one INSIDE is worth about %.1f coin flips."
              % (0.0 if chance >= 1.0 else -math.log(chance, 2)))
    print("         (%.0f px from the centre, on the longer axis -- description, not the test)"
          % dist)
    # THE ANSWER YOU GIVE WITHOUT LOOKING. Uniform chance does not model it: a target that
    # straddles the middle of the picture is passed by this answer every time, however small
    # it is. Recorded per row so a run that keeps landing there can be seen as a run, which
    # is the only level at which the habit is visible.
    frame = task.get("frame") or {}
    iw = float(frame.get("image_width") or 0)
    ih = float(frame.get("image_height") or 0)
    centre_tell = None
    if iw > 0 and ih > 0:
        centre_tell = max(abs(ax - iw / 2.0), abs(ay - ih / 2.0))
        near = centre_tell <= 0.02 * max(iw, ih)
        print("         %.0f px from the MIDDLE OF THE PICTURE%s"
              % (centre_tell, " -- that is the answer given without looking" if near else ""))

    scale = task.get("image_pixel_covers_desktop_px") or 1.0
    if scale and abs(scale - 1.0) > 1e-9:
        print("         one image pixel covers %.2f desktop px, so nothing measured through"
              % scale)
        print("         this image can be finer than that.")

    out = os.path.join(os.path.dirname(os.path.abspath(args.task)), RESULTS)
    row = {"target": t["name"], "answer": [ax, ay], "inside": inside,
           "centre_dist_px": round(dist, 1), "by": args.by, "split": task.get("split"),
           # Written into the ROW, not looked up later from the task: the task file can be
           # regenerated from a different screen, and a result whose difficulty has to be
           # reconstructed is a result that will eventually be reconstructed wrongly.
           "chance": chance,
           "px_from_image_centre": (None if centre_tell is None else round(centre_tell, 1))}
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    rows = [json.loads(l) for l in open(out, encoding="utf-8") if l.strip()]
    held = [r for r in rows if r.get("split") == "held-out"]
    # A row with no chance recorded is not a weak data point, it is not a data point: nothing
    # in it separates a model that read the picture from one that did not.
    unusable = [r for r in held if r.get("chance") is None]
    held = [r for r in held if r.get("chance") is not None]
    n, k = len(held), sum(1 for r in held if r["inside"])
    print()
    if unusable:
        print("%d earlier held-out row(s) are excluded: no blind-hit rate was recorded, so"
              % len(unusable))
        print("  whether the answer could have been wrong is unknown.")
    print("held-out so far: %d of %d inside" % (k, n))
    if n:
        # EXPECTED HITS, NOT THE PRODUCT OVER THE HITS. The product was taken over rows that
        # were inside, so a run with no hits multiplied nothing and printed 100% -- an empty
        # product dressed as a finding. This holds at every k and n: how many a blind
        # answerer would have got, against how many were got.
        expected = sum(float(r["chance"]) for r in held)
        print("  a blind answerer would average %.2f hit(s) out of %d; this run got %d."
              % (expected, n, k))
        if k <= expected:
            print("  That is not better than not looking.")
        # A habit is only visible across rows.
        tells = [r for r in held if r.get("px_from_image_centre") is not None]
        middling = [r for r in tells if r["px_from_image_centre"] <= 32]
        if tells and len(middling) >= max(2, len(tells) // 2):
            print("  %d of %d answers landed within 32 px of the middle of the picture."
                  % (len(middling), len(tells)))
            print("  Containment cannot tell that apart from finding the target, so treat")
            print("  any INSIDE among them as unproven.")
    if n and k == n:
        bound = 0.05 ** (1.0 / n)
        print("  with no failures in %d, the one-sided 95%% lower bound is %.1f%%."
              % (n, 100.0 * bound))
        if n < 299:
            print("  %d more would support >=99%%." % (299 - n))
        # The number people actually care about is whether a whole task completes.
        print("  even at that rate, 50 actions all succeeding is about %.0f%%."
              % (100.0 * bound ** 50))
    elif n:
        print("  %d miss(es). A rate below 100%% is what the end-to-end measurement has to"
              % (n - k))
        print("  survive, and 50 actions at %.0f%% all succeeding is about %.1f%%."
              % (100.0 * k / n, 100.0 * (k / float(n)) ** 50))
    print("  wrote %s" % out)
    return 0 if inside else 1


if __name__ == "__main__":
    sys.exit(main())

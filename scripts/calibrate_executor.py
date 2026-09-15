# -*- coding: utf-8 -*-
"""Measurement 1 of 3: does a coordinate mean the same thing at both ends?

THE POINT OF DOING THIS FIRST. A computer-use run has two independent ways to
click the wrong thing: the model can read the wrong place off the picture, or the
picture's pixels can be converted to desktop coordinates wrongly. Measured
together they are indistinguishable, and the second one is a constant bias that
would be recorded as "the model is bad at coordinates" -- a conclusion that
survives every later experiment because nothing afterwards ever isolates it.

So this run uses NO MODEL AT ALL. It takes rectangles the OS states, converts each
one's centre into the captured image's pixels and back out again through the same
Frame a model's answer would travel through, and then asks the OS what is at the
resulting point. Agreement means the transform is sound and any later error
belongs to the model. Disagreement means no model accuracy would have helped.

WHAT IT DOES NOT DO: click anything. WindowFromPoint runs the same hit test that
decides where a click is delivered, so it is evidence about clicks, and it presses
nothing on the operator's desktop. See tools/window_probe.py.

Three outcomes, kept apart on purpose:

  HIT       the point landed on the target, or on one of its children.
  OCCLUDED  it landed on a DIFFERENT top-level window. The point is covered by
            something the operator left open. Correct behaviour, not a transform
            error, and counting it as a miss would make the result depend on what
            happened to be on screen.
  MISS      it landed inside the same top-level window but not on the target, or
            on nothing at all. This is the one that means something is wrong.

Run it as:  .venv\\Scripts\\python.exe -m scripts.calibrate_executor
Add --downscale N to measure what capturing at a reduced size costs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools import window_probe as W
from tools.screen_capture import capture, capture_reports_what_it_did
from tools.screen_frame import round_trip_error


def _grid(t: W.Target, n: int = 5):
    """Points spread across a rectangle, named by where they sit.

    The centre alone would hide the failure that matters most. A constant offset --
    a wrong origin, an unaccounted title bar -- still lands inside a large window
    when aimed at its middle, and only falls out near the edges. So the sweep
    spreads over the rectangle, and an inset keeps it off the one-pixel border
    where the neighbouring window legitimately owns the point.
    """
    for r in range(n):
        for c in range(n):
            fx = (c + 0.5) / n
            fy = (r + 0.5) / n
            x = int(t.left + fx * t.width)
            y = int(t.top + fy * t.height)
            if t.contains(x, y):
                yield ("%d,%d" % (c, r), x, y)


def _probe_points(t: W.Target, occluders, want: int):
    """Up to `want` points inside `t` that nothing in front of it covers.

    WHY THIS MATTERS FOR THE NUMBER AT THE END. A first version aimed at five fixed
    points per rectangle and 87% of them came back OCCLUDED, leaving 38 decisive
    probes. Thirty-eight successes is not thirty-eight successes' worth of evidence:
    the one-sided 95% lower bound on a run with no failures is 0.05**(1/n), so 38
    supports only "at least 92.4%" -- while 299 supports "at least 99%". The
    occlusion was not telling us anything about the transform; it was telling us the
    operator had windows open. Choosing points that are actually exposed converts
    that wasted evidence into decisions, without making any probe easier to pass.

    `occluders` are the rectangles of windows ABOVE this one in z-order. EnumWindows
    walks topmost-first, so a caller that accumulates as it goes has exactly them.
    """
    out = []
    for name, x, y in _grid(t, n=7):
        if any(o.contains(x, y) for o in occluders):
            continue
        out.append((name, x, y))
        if len(out) >= want:
            break
    return out


def _classify(t: W.Target, got, x, y):
    if got is None:
        return "MISS", "nothing is at that point"
    if got.hwnd == t.hwnd:
        return "HIT", ""
    # A child of the target is still the target as far as a click is concerned:
    # clicking a button inside a pane delivers to the button, and the pane is what
    # we named. Same root AND inside the target's rectangle is the honest test.
    if got.root == t.root and t.contains(x, y):
        return "HIT", "child %s" % got.cls
    if got.root != t.root:
        return "OCCLUDED", "covered by %s" % got.label()
    return "MISS", "same window, wrong part: %s" % got.label()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--downscale", type=int, default=0,
                    help="capture at this max dimension instead of full size, to measure "
                         "what a reduced capture costs in precision")
    ap.add_argument("--min-side", type=int, default=24,
                    help="ignore rectangles thinner than this (default 24)")
    ap.add_argument("--per-rect", type=int, default=6,
                    help="how many exposed points to probe per rectangle (default 6)")
    ap.add_argument("--out", default="",
                    help="write the full result as JSON here")
    args = ap.parse_args(argv)

    print(capture_reports_what_it_did())
    img, frame = capture(max_dimension=args.downscale)
    print(frame.describe())
    if frame.is_downscaled:
        print("  note: an image pixel covers %.2f desktop pixels, so no model reading "
              "this image can be more precise than that." % frame.scale)
    print()

    # z-order, topmost first (EnumWindows' own order). A window's occluders are
    # exactly the top-level windows already seen, so accumulate as we descend.
    targets = []
    above = []
    for top in W.top_level_windows(min_side=max(64, args.min_side)):
        targets.append((top, list(above)))
        for child in W.children_of(top.hwnd, min_side=args.min_side):
            targets.append((child, list(above)))
        above.append(top)
    print("rectangles the OS states: %d" % len(targets))

    counts = {"HIT": 0, "OCCLUDED": 0, "MISS": 0, "OUTSIDE": 0}
    misses = []
    worst_round_trip = 0.0
    rows = []

    for t, occluders in targets:
        for where, sx, sy in _probe_points(t, occluders, want=args.per_rect):
            if not frame.contains_screen(sx, sy):
                # The rectangle is partly off the captured area. A model could never
                # have seen this point, so it is not a transform failure -- but it is
                # worth counting, because a large number here means the capture is
                # not covering the desktop the windows actually live on.
                counts["OUTSIDE"] += 1
                continue
            worst_round_trip = max(worst_round_trip, round_trip_error(frame, sx, sy))
            # Quantised to a whole image pixel, because that is what a model can
            # say. Carrying the fraction through would measure a path nothing
            # takes, and would report a downscaled capture as free.
            ix, iy = frame.to_image(sx, sy)
            bx, by = frame.to_screen(int(round(ix)), int(round(iy)))
            got = W.window_at(bx, by)
            verdict, note = _classify(t, got, bx, by)
            counts[verdict] += 1
            rows.append({"target": t.label(), "where": where, "aimed": [sx, sy],
                         "through_frame": [bx, by], "verdict": verdict, "note": note})
            if verdict == "MISS":
                misses.append((t, where, sx, sy, bx, by, note))

    probed = counts["HIT"] + counts["OCCLUDED"] + counts["MISS"]
    print("probes: %d  (plus %d outside the capture)" % (probed, counts["OUTSIDE"]))
    print()
    for k in ("HIT", "OCCLUDED", "MISS"):
        pct = (100.0 * counts[k] / probed) if probed else 0.0
        print("  %-9s %5d  %5.1f%%" % (k, counts[k], pct))
    print()
    print("worst round-trip displacement through the frame: %.0f px" % worst_round_trip)

    # The verdict is deliberately about MISS alone. OCCLUDED says something was in
    # front; that is the desktop's business, not the transform's.
    decided = counts["HIT"] + counts["MISS"]
    if decided == 0:
        print("\nINCONCLUSIVE: every probe was occluded. Nothing was measured.")
    elif counts["MISS"] == 0:
        # An all-success run does not mean "100%". The one-sided 95% lower bound on a
        # binomial with no failures is 0.05**(1/n) -- 299 successes buys "at least
        # 99%", 2995 buys "at least 99.9%". Printed next to the result so a small
        # sweep cannot be read as a strong claim later, when only the word SOUND is
        # remembered.
        bound = 0.05 ** (1.0 / decided)
        print("\nTRANSFORM SOUND: %d of %d uncovered probes landed on their target. "
              "A wrong click from here is the model's, not the coordinate system's."
              % (counts["HIT"], decided))
        print("  with no failures in %d, the one-sided 95%% lower bound is %.1f%%."
              % (decided, 100.0 * bound))
        if decided < 299:
            print("  %d more decisive probes would support >=99%%."
                  % (299 - decided))
    else:
        print("\nTRANSFORM NOT SOUND: %d of %d uncovered probes landed somewhere else. "
              "Measuring model grounding before fixing this would attribute this error "
              "to the model." % (counts["MISS"], decided))
        print("\nfirst misses:")
        for t, where, sx, sy, bx, by, note in misses[:12]:
            print("  %-34s %-6s aimed (%d, %d) -> (%d, %d)  %s"
                  % (t.label()[:34], where, sx, sy, bx, by, note))

    if args.out:
        payload = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "capture": capture_reports_what_it_did(),
                   "frame": frame._asdict(),
                   "counts": counts,
                   "worst_round_trip_px": worst_round_trip,
                   "probes": rows}
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        print("\nwrote %s (%d probes)" % (args.out, len(rows)))

    return 0 if counts["MISS"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

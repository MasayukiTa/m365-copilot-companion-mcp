# -*- coding: utf-8 -*-
"""Measurement 2, part 1: build a grounding task from one frozen picture of the screen.

WHY THE PICTURE IS FROZEN AND THE AGENT DOES NOT TAKE ITS OWN. If the agent captures the
screen itself, the ground truth has to be captured separately -- and between the two the
operator's clock ticks, a window repaints, a notification slides in. The answer would then
be scored against a screen that is not the one it saw, and every disagreement would be
ambiguous between "the model was wrong" and "the screen moved". So one capture is taken
here, the rectangles the OS states are recorded from the SAME instant, and the agent is
pointed at that file. This is the static grounding measurement, deliberately separate from
the live one.

WHAT COUNTS AS CORRECT. Not "within N pixels of the centre" -- that question has no fixed
right answer, because a large button tolerates being missed by twenty pixels and a small
icon does not. The measure is whether the reported point falls INSIDE the target's
rectangle, which is the region a click on it would actually be delivered to. Distance from
the centre is recorded too, but as description, not as the criterion.

WHAT IS DELIBERATELY NOT HERE. No model is called. This writes the task and the truth; the
asking is a separate step (a short instruction to a worker, the length a person would type),
and the scoring is scripts/score_grounding.py. Keeping them apart is what lets the same
frozen task be re-asked later, or asked of something else, and compared.

Held-out discipline: pass --split held-out for the screens whose results are allowed to
count. A screen that has been looked at while debugging the harness is burned, and the
split is written into the task file so a later reader cannot lose track of which it was.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


#: A target has to be nameable in words without naming its position, or the question leaks
#: its own answer. A window with a title qualifies; an unnamed panel does not.
MIN_SIDE = 24

#: THE LARGEST SHARE OF THE PICTURE A TARGET MAY COVER AND STILL BE A QUESTION. Scoring by
#: containment asks "would a click here land on it", which is the right question -- but a
#: rectangle that covers the whole image is passed by every answer that exists, including a
#: number picked without looking. The first held-out run on 2026-09-16 scored INSIDE against
#: a maximised window whose rectangle was 0,0..1600,900: the full image. The result was
#: recorded, a lower bound was printed next to it, and none of it distinguished a model from
#: a coin. A ceiling here does not make the scoring stricter; it stops the harness handing
#: out questions whose answer cannot be wrong.
MAX_CHANCE = 0.25


def chance_of_a_blind_hit(rect_image, image_width, image_height):
    """How often an answer picked without looking at the picture would land inside.

    This is the null hypothesis the measurement has to beat, and it is per-target: a target
    covering a quarter of the screen is passed by one blind answer in four, so four such
    successes are worth about as much as one coin landing heads four times -- which is to
    say, not nothing, but not what "grounding works" means either.
    """
    x0, y0, x1, y1 = rect_image
    frame_area = float(image_width) * float(image_height)
    if frame_area <= 0:
        return 1.0
    return max(0.0, (x1 - x0)) * max(0.0, (y1 - y0)) / frame_area


def _nameable(t) -> str:
    """How a person would refer to this on screen, or "" if they could not.

    Position words are exactly what must not appear here. "the Save button" is a question;
    "the button near the top-left" is most of the answer.
    """
    title = (t.title or "").strip()
    if title:
        return title
    return ""


def _targets(min_side: int):
    from tools import window_probe as W          # binds ctypes.windll at import

    out = []
    above = []
    for top in W.top_level_windows(min_side=max(64, min_side)):
        name = _nameable(top)
        if name:
            out.append((top, name, list(above)))
        for child in W.children_of(top.hwnd, min_side=min_side):
            cname = _nameable(child)
            if cname:
                out.append((child, "%s の中の「%s」" % (name or top.cls, cname), list(above)))
        above.append(top)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="", help="directory to write the task into")
    ap.add_argument("--max-dimension", type=int, default=1600,
                    help="longest edge of the saved image; the reduction is recorded")
    ap.add_argument("--min-side", type=int, default=MIN_SIDE)
    ap.add_argument("--max-chance", type=float, default=MAX_CHANCE,
                    help="drop targets a blind guess would hit more often than this")
    ap.add_argument("--split", choices=["dev", "held-out"], default="dev",
                    help="dev screens may be looked at while building the harness; "
                         "held-out ones may not, and only their results count")
    args = ap.parse_args(argv)

    from tools.screen_capture import capture, capture_reports_what_it_did

    out_dir = args.out or os.path.join(
        os.environ.get("TEMP", "."), "grounding-%s" % time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)

    img, frame = capture(max_dimension=args.max_dimension)
    png = os.path.join(out_dir, "screen.png")
    img.save(png, optimize=True)

    targets = _targets(args.min_side)
    if not targets:
        print("no nameable target on screen; nothing to ask about")
        return 2

    items = []
    too_easy = []
    for t, name, occluders in targets:
        # Only ask about something that is actually visible: a target buried under another
        # window cannot be found in the picture, and scoring it would measure the desktop's
        # arrangement rather than the model.
        cx, cy = t.centre
        if any(o.contains(cx, cy) for o in occluders):
            continue
        if not frame.contains_screen(cx, cy):
            continue
        ix0, iy0 = frame.to_image(t.left, t.top)
        ix1, iy1 = frame.to_image(t.right, t.bottom)
        # CLAMPED TO THE PICTURE, because the scoring region has to be a region an answer
        # could name. A window that runs off the edge of the capture produced a rectangle
        # starting at y = -3, and a model cannot point at a pixel that is not in the image
        # it was shown; scoring against the unclamped rectangle would credit or blame it for
        # ground it never saw.
        ix0 = max(0.0, min(ix0, frame.image_width))
        iy0 = max(0.0, min(iy0, frame.image_height))
        ix1 = max(0.0, min(ix1, frame.image_width))
        iy1 = max(0.0, min(iy1, frame.image_height))
        if ix1 - ix0 < args.min_side / frame.scale or iy1 - iy0 < args.min_side / frame.scale:
            continue        # what remains visible is too small to ask about honestly
        rect = [round(ix0, 1), round(iy0, 1), round(ix1, 1), round(iy1, 1)]
        chance = chance_of_a_blind_hit(rect, frame.image_width, frame.image_height)
        if chance > args.max_chance:
            # Too big to be wrong about. Kept out of the task rather than scored leniently,
            # because a question nobody can fail is not a lenient question, it is not one.
            too_easy.append((name, chance))
            continue
        items.append({
            "name": name,
            "class": t.cls,
            # In IMAGE pixels, because that is the frame an answer will be given in.
            "rect_image": rect,
            "rect_screen": [t.left, t.top, t.right, t.bottom],
            # Carried WITH the target, so the scorer never has to guess what beating it means.
            "chance": round(chance, 5),
        })

    if not items:
        print("nothing askable on this screen: %d target(s) seen, %d of them cover more than "
              "%.0f%% of the picture and would be hit blind."
              % (len(targets), len(too_easy), 100.0 * args.max_chance))
        return 2

    task = {
        "made_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "split": args.split,
        "max_chance": args.max_chance,
        "capture": capture_reports_what_it_did(),
        "frame": frame._asdict(),
        "image": png,
        # The floor under any accuracy measured through this image: one image pixel stands
        # for this many desktop pixels, so no answer can be more precise than that.
        "image_pixel_covers_desktop_px": round(frame.scale, 3),
        "targets": items,
    }
    task_path = os.path.join(out_dir, "task.json")
    with open(task_path, "w", encoding="utf-8") as fh:
        json.dump(task, fh, ensure_ascii=False, indent=1)

    print(task["capture"])
    print(frame.describe())
    print("wrote %s  (%d target(s), split=%s)" % (task_path, len(items), args.split))
    if too_easy:
        print("withheld %d target(s) a blind answer would hit more than %.0f%% of the time "
              "(largest: %s at %.0f%%)"
              % (len(too_easy), 100.0 * args.max_chance,
                 max(too_easy, key=lambda p: p[1])[0],
                 100.0 * max(p[1] for p in too_easy)))
    print()
    print("Ask about ONE of these at a time. The question names the thing and nothing else --")
    print("no position words, no size, no colour; those are the answer, not the question:")
    for it in items[:12]:
        print("   %s" % it["name"])
    if len(items) > 12:
        print("   ... and %d more" % (len(items) - 12))
    print()
    print("Then score with:")
    print("  .venv\\Scripts\\python.exe -m scripts.score_grounding %s --target \"<name>\" "
          "--answer X,Y" % task_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Run the FLEET'S OWN ANALYZE path with an attachment, and see whether it opens a tab.

WHAT THIS MEASURES, AND WHY THE OTHER PROBE DOES NOT. scripts/probes/send_image_over_socket.py
built its own SocketRoute and sent its own turn: it answered "can an annotation sent over a
socket reach a model that sees it" and it did. It did NOT touch the code the fleet runs. And
that distinction turned out to matter: `relay/transport_policy`'s ATTACHMENT rule was retired
on the strength of that probe, the suite went green, and **the fleet still opened a tab for
every attachment**, because a second copy of the same rule lived in
`ResearchSession._try_socket` and that is the one the fleet consults.

So this probe constructs the session THE FLEET CONSTRUCTS -- `ResearchSession(context, instr,
model_name="", profile=ANALYST, upload_path=...)`, copied from relay/relay_fleet.py's ANALYZE
branch -- and drives its own start()/poll() loop to the end.

THE THREE THINGS IT READS, separately, because a single verdict hides which half failed:

    took a socket           `session.transport` == "socket"
    opened no tab           the context's page count is the same afterwards as before
    the model SAW the file  the report contains a phrase that exists only in the pixels

THE FIRST VERSION READ THE WRONG FLAGS AND CALLED A PASS A FAILURE. It read `session.socket`
and `session.page` after the loop, but `_finish()` calls `close()`, which sets `socket = False`
and drops the page -- so a finished socket run and a finished tab run look identical. The run
it scored as "still went to a tab" had `[socket_attachment] uploaded ... -> docId` in its own
log and the phrase in its report. The instrument was wrong, not the code. `transport` is set
when the turn goes out and is never cleared, which is why it exists.

The phrase is randomly generated and appears in no filename, no path and no instruction. The
instruction is the length a person would type and does not say what to look for; writing the
expected answer into the prompt is how a verification stops being one.

    python scripts/probes/analyze_over_socket.py

It sends ONE turn to the Analyst. That is an outward action and it is the experiment.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

CDP = os.environ.get("MCP_CDP_URL", "http://127.0.0.1:9222")
OUT = os.path.join(os.environ.get("TEMP", "."), "analyze_socket_%d.jsonl" % int(time.time()))

#: How long to let the Analyst work. The fleet gives it minutes; so does this.
BUDGET_S = float(os.environ.get("MCP_ANALYZE_PROBE_BUDGET_S", "420"))

#: Same word list and the same reason as send_image_over_socket.py: an ordinary sentence on a
#: plain background. A box around random characters is a CAPTCHA, and the model refuses those --
#: which is evidence the picture arrived and useless as an answer.
_WORDS = ("みかん", "たまねぎ", "えんぴつ", "せんぷうき", "ゆきだるま", "にんじん",
          "ひまわり", "けん玉", "ふうりん", "こけし", "かみひこうき", "どんぐり")


def _write(rec):
    try:
        with open(OUT, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _make_png(phrase):
    from PIL import Image, ImageDraw, ImageFont

    path = os.path.join(os.environ.get("TEMP", "."), "analyze_socket_probe.png")
    img = Image.new("RGB", (640, 160), (255, 255, 255))
    d = ImageDraw.Draw(img)
    font = None
    for cand in (r"C:\Windows\Fonts\meiryo.ttc", r"C:\Windows\Fonts\YuGothM.ttc",
                 r"C:\Windows\Fonts\msgothic.ttc"):
        try:
            font = ImageFont.truetype(cand, 34)
            break
        except Exception:
            continue
    # A missing font draws tofu, and the probe would then measure the font rather than the
    # transport. Said out loud rather than left to a silent fallback.
    if font is None:
        raise RuntimeError("no Japanese font available to render the probe image")
    d.text((40, 60), "本日の合言葉は %s です" % phrase, fill=(0, 0, 0), font=font)
    img.save(path, optimize=True)
    return path


def main():
    from playwright.sync_api import sync_playwright

    from relay.agent_profiles import ANALYST, ResearchSession

    phrase = "%s%d" % (random.choice(_WORDS), random.randint(10, 99))
    png = _make_png(phrase)
    print("probe image: %s  phrase on it: %s" % (png, phrase))
    print("recording to %s" % OUT)
    _write({"event": "start", "ts": time.time(), "phrase": phrase, "image": png})

    # THE INSTRUCTION SAYS WHERE, NOT WHAT. Same length a person would type.
    instr = "この画像に書かれている文を、そのまま1行で書き写して。"

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        before = len(ctx.pages)
        print("tabs before: %d" % before)

        # CONSTRUCTED THE WAY relay/relay_fleet.py's ANALYZE branch constructs it. Anything
        # else would measure a session the fleet does not build.
        s = ResearchSession(ctx, instr, model_name="", profile=ANALYST,
                            upload_path=png, timeout_s=BUDGET_S).start()

        report, deadline = None, time.time() + BUDGET_S + 120
        while report is None and time.time() < deadline:
            report = s.poll()
            if report is None:
                time.sleep(2.0)
        transport = getattr(s, "transport", "")
        took_socket = transport == "socket"
        after = len(ctx.pages)

    report = report or ""
    saw = phrase in report
    mentions = any(w in report for w in ("画像", "image", "CAPTCHA"))
    _write({"event": "done", "ts": time.time(), "transport": transport,
            "tabs_before": before, "tabs_after": after,
            "phrase_present": saw, "mentions_the_image": mentions,
            "error": getattr(s, "error", ""), "report": report[:4000]})

    print()
    print("transport     : %s" % (transport or "(none -- the turn never went out)"))
    print("tabs          : %d -> %d" % (before, after))
    print("error         : %s" % (getattr(s, "error", "") or "(none)"))
    print("REPORT        : %s" % report[:400])
    print()

    if not took_socket:
        print("READING: the ANALYZE path went by %r, not a socket. Read the"
              % (transport or "nothing"))
        print("         [socket_attachment] lines above for the reason it fell back.")
        return 1
    if saw:
        print("READING: the fleet's own ANALYZE session took a SOCKET, opened no tab, and the")
        print("         model read %s off the image. The phrase is in no filename, no path" % phrase)
        print("         and no instruction, so the pixels are its only source.")
        return 0
    if mentions:
        print("READING: it took a socket and the reply TALKS ABOUT THE IMAGE without")
        print("         transcribing it. A model cannot discuss a picture it never received,")
        print("         so the attachment arrived; the transcription failed for another")
        print("         reason -- read the report.")
        return 0
    print("READING: it took a socket, and the reply neither contains %s nor mentions an" % phrase)
    print("         image. The turn went out without the attachment, which is the one")
    print("         outcome the fallback exists to prevent. Do not call this a pass.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

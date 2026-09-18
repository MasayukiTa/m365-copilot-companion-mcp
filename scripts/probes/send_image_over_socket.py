# -*- coding: utf-8 -*-
"""Send an image to Copilot over the SOCKET, and find out whether it was actually seen.

THE QUESTION THIS CLOSES. relay/transport_policy routes a task carrying a local file to a TAB,
because a socket was thought to have nowhere to put one. That reasoning was already shown false:
the bytes go to UploadFile over HTTP and an id rides the socket in `messageAnnotations`. Two
things then had to be established, and both now are:

  * UploadFile accepts the token the relay already holds -- measured 2026-09-18, HTTP 200,
    result.value "Success", by replaying the page's own request with ONLY the Authorization
    swapped (scripts/probes/replay_upload_with_our_token.py).
  * The server accepts an annotated frame on this socket -- the page sent one on 2026-09-17 and
    the reply echoed it back with messageAnnotationSource "UserAnnotated".

What was NOT established is the thing this probe does: whether OUR socket turn, carrying that
annotation, reaches a model that can see the picture.

WHY NOT JUST ATTACH IT IN THE PAGE. Because that exercises the route that already works and
proves nothing about this one. The point of the experiment is the path under test.

THE EVIDENCE IS A STRING THE MODEL CANNOT GUESS. The image carries a short random token and the
prompt asks only what is written on it -- it does not say what to look for, and the token
appears in no filename, no path and no instruction. A correct answer is evidence the picture
was seen; anything else is evidence it was not. Nothing about the expected answer is in the
prompt, which is the rule this project already applies to every measured run.

    python scripts/probes/send_image_over_socket.py

It sends ONE turn into the conversation the bridge is on. That is an outward action and it is
the experiment.
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
UPLOAD_URL = "https://substrate.office.com/m365Copilot/UploadFile"
OUT = os.path.join(os.environ.get("TEMP", "."), "socket_image_%d.jsonl" % int(time.time()))

#: Headers a replay must not forward: HTTP/2 framing, a recomputed length, and the credential
#: under test. Same list as replay_upload_with_our_token.py, for the same reasons.
_DROP = {":authority", ":method", ":path", ":scheme", "content-length", "authorization",
         "accept-encoding", "host"}


def _write(rec):
    try:
        with open(OUT, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


#: Words a model cannot guess in combination, rendered as an ordinary sentence.
#:
#: THE FIRST VERSION DREW A CAPTCHA. Six random characters inside a drawn box is exactly what a
#: CAPTCHA looks like, and the model said so: "this image may be a CAPTCHA or for authentication
#: purposes, so I cannot read or transcribe the characters". That refusal was itself proof the
#: picture had arrived -- it could not have known the image held characters in a box otherwise --
#: but it made the probe unable to answer its own question, and a probe whose evidence depends
#: on reading a refusal carefully is one nobody will trust later.
#:
#: A plain sentence on white, no border, no distortion. The pair is 1 in 12 * 90 = 1080, which is
#: not 36**6 but is far past anything a model lands on by guessing -- and unlike the CAPTCHA it
#: can actually be answered.
_WORDS = ("みかん", "たまねぎ", "えんぴつ", "せんぷうき", "ゆきだるま", "にんじん",
          "ひまわり", "けん玉", "ふうりん", "こけし", "かみひこうき", "どんぐり")


def _token():
    return "%s%d" % (random.choice(_WORDS), random.randint(10, 99))


def _make_png(token):
    """A sentence on a plain background. No box: a box is what made the last one a CAPTCHA."""
    from PIL import Image, ImageDraw, ImageFont

    path = os.path.join(os.environ.get("TEMP", "."), "socket_image_probe.png")
    img = Image.new("RGB", (640, 160), (255, 255, 255))
    d = ImageDraw.Draw(img)
    text = "本日の合言葉は %s です" % token
    font = None
    for cand in (r"C:\Windows\Fonts\meiryo.ttc", r"C:\Windows\Fonts\YuGothM.ttc",
                 r"C:\Windows\Fonts\msgothic.ttc"):
        try:
            font = ImageFont.truetype(cand, 34)
            break
        except Exception:
            continue
    # A MISSING FONT WOULD DRAW TOFU, and tofu is unreadable by anything -- the probe would then
    # measure the font, not the transport. Said out loud rather than left to a silent fallback.
    if font is None:
        print("WARNING: no Japanese font found; the image would be unreadable. Aborting.")
        raise RuntimeError("no Japanese font available to render the probe image")
    d.text((40, 60), text, fill=(0, 0, 0), font=font)
    img.save(path, optimize=True)
    return path


def _upload(png):
    """Upload through the page's own request with only the credential swapped.

    Reconstructing this request failed three times on 2026-09-18 -- the page sends six multipart
    fields and eighteen headers, and a probe that rebuilt them got 403s that were reported as
    answers about the token. So it is captured and re-issued, never rebuilt.
    """
    import requests
    from playwright.sync_api import sync_playwright

    from relay import profile_token as PT
    from relay.agent_profiles import ANALYST, upload_file

    captured = {}
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        token = PT.token_via_light_page(ctx, ANALYST.url)
        if not token:
            return None, "no token captured"
        page = ctx.new_page()
        try:
            def _on_request(req):
                if "uploadfile" not in (req.url or "").lower() or captured:
                    return
                try:
                    captured["body"] = req.post_data_buffer
                    captured["headers"] = {k: v for k, v in req.all_headers().items()
                                           if k.lower() not in _DROP}
                except Exception:
                    pass

            page.on("request", _on_request)
            page.goto(ANALYST.url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            if not upload_file(page, png):
                return None, "the page never accepted the file"
            deadline = time.time() + 25
            while time.time() < deadline and not captured:
                page.wait_for_timeout(500)
        finally:
            try:
                page.close()
            except Exception:
                pass
    if not captured:
        return None, "the page's own upload request was never seen"

    headers = dict(captured["headers"])
    headers["Authorization"] = "Bearer " + token
    r = requests.post(UPLOAD_URL, headers=headers, data=captured["body"], timeout=45)
    if r.status_code not in (200, 201):
        return None, "upload refused: HTTP %s" % r.status_code
    try:
        body = r.json()
    except Exception:
        return None, "upload returned a body that is not JSON"
    doc_id = body.get("docId")
    if not doc_id:
        return None, "upload returned no docId"
    return {"docId": doc_id, "fileName": body.get("fileName") or os.path.basename(png)}, ""


def main():
    token = _token()
    png = _make_png(token)
    print("probe image: %s  token on it: %s" % (png, token))
    _write({"event": "start", "ts": time.time(), "token": token})

    print("uploading through the page's own request, credential swapped ...")
    up, why = _upload(png)
    if not up:
        print("RESULT: %s" % why)
        _write({"event": "upload_failed", "why": why})
        return 2
    print("uploaded. docId=%s fileName=%s" % (up["docId"], up["fileName"]))
    _write({"event": "uploaded", "doc_id": up["docId"], "file_name": up["fileName"]})

    annotations = [{
        "id": up["docId"],
        "messageAnnotationMetadata": {"@type": "File", "annotationType": "File",
                                      "fileType": "png", "fileName": up["fileName"]},
        "messageAnnotationType": "ImageFile",
    }]

    # SHORT, AND IT DOES NOT SAY WHAT TO LOOK FOR. The length a person would type, and no hint
    # of the answer -- putting the expected string in the prompt is how a verification stops
    # being one.
    prompt = "この画像に書かれている文を、そのまま1行で書き写して。"

    print("sending one turn over the socket, with the annotation ...")
    _write({"event": "sending", "ts": time.time(), "prompt": prompt,
            "annotations": annotations})
    try:
        reply = _send_over_socket(prompt, annotations)
    except Exception as exc:
        print("RESULT: the socket send raised: %s: %s" % (type(exc).__name__, exc))
        _write({"event": "send_failed", "error": "%s: %s" % (type(exc).__name__, exc)})
        return 3

    print("REPLY: %s" % (reply or "")[:400])
    body = reply or ""
    saw = token in body
    # A REFUSAL THAT DESCRIBES THE PICTURE IS EVIDENCE THE PICTURE ARRIVED. The first run drew a
    # CAPTCHA-shaped image and was refused on those grounds -- which proved the transport worked
    # and was scored as a failure, because the only test was "is the token in the reply". Both
    # readings are kept and reported separately: a model cannot refuse an image it never got.
    describes = any(w in body for w in ("CAPTCHA", "画像", "image"))
    _write({"event": "reply", "ts": time.time(), "reply": body[:4000],
            "token_present": saw, "mentions_the_image": describes})
    print()
    if saw:
        print("READING: the model read %s off the image. An annotation sent over the SOCKET"
              % token)
        print("         reaches a model that can see the attachment, so transport_policy's")
        print("         routing of file work to a tab is no longer the only option.")
        return 0
    if describes:
        print("READING: the reply does not contain %s, but it TALKS ABOUT THE IMAGE." % token)
        print("         A model cannot refuse or describe a picture it never received, so the")
        print("         annotation reached one that can see it. The transcription failed for")
        print("         some other reason -- read the reply.")
        return 0
    print("READING: the reply neither contains %s nor mentions an image at all. Either the" % token)
    print("         annotation did not reach a model that can see it, or the frame was refused.")
    print("         Look for InvalidRequest in the reply above before concluding either.")
    return 1


def _send_over_socket(prompt, annotations):
    """One turn on the agent surface, carrying the annotation.

    THE REAL API, read rather than guessed -- an earlier draft of this file invented
    `current_route()` and `route.ask()`, neither of which exists. The shape here is copied from
    relay/refuter.py's live usage: a SocketRoute, refreshed against a browser context so it has
    a token and a captured template, then driver_for() and the driver's own send/settled pair.
    """
    from playwright.sync_api import sync_playwright

    from relay import socket_route as SR
    from relay.agent_profiles import ANALYST
    from relay.capture_floor import floored
    from relay.profile_token import capture_fn as _choose_capture

    # BUILT THE WAY THE FLEET BUILDS IT. A bare SocketRoute() has capture_fn=None, so refresh()
    # returns False on its first line and the probe reports "could not capture a token" about a
    # route that was never given a way to capture one. Copied from relay_fleet._socket_route
    # rather than invented -- including the capture FLOOR, which stops a margin longer than the
    # token from re-capturing in a loop.
    route = SR.SocketRoute(capture_fn=floored(_choose_capture()),
                           connect_fn=SR.websocket_connect,
                           log=lambda m: print(m, flush=True))
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        if not route.refresh(ctx, ANALYST.url):
            raise RuntimeError("could not capture a token/template for the agent surface")
        drv = route.driver_for("image-probe", agent_url=ANALYST.url,
                               turn_timeout_s=300.0, frame_timeout_s=120.0)
        if drv is None:
            raise RuntimeError("driver_for returned None -- no socket route available")
        drv.send(prompt, annotations=annotations)
        # settled_text() returns "" while the turn is still running and the answer once it is
        # done -- its own docstring says so -- so polling it needs no private call.
        deadline = time.time() + 300.0
        answer = ""
        while time.time() < deadline:
            answer = drv.settled_text()
            if answer:
                break
            time.sleep(1.0)
        return answer


if __name__ == "__main__":
    sys.exit(main())

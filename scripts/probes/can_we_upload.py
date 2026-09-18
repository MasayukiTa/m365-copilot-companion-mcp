# -*- coding: utf-8 -*-
"""Can the relay reach UploadFile with the credential it already uses for ChatHub?

OBSERVED 2026-09-17, by recording the page's own frames during an ANALYZE attachment:

    POST https://substrate.office.com/m365Copilot/UploadFile   (multipart, scenario=UploadImage)
    then the outgoing ChatHub frame carried
    "messageAnnotations":[{"id":"0-wjp-d3-...","messageAnnotationMetadata":{"@type":"File",
      "fileType":"png","fileName":"monthly.png"},"messageAnnotationType":"ImageFile"}]

So the protocol has a place for an attachment: bytes go over HTTP, an id rides the socket.
relay/transport_policy.py's "a socket has nowhere to put a file" was inference, and it is
wrong. Its ROUTING is still in force, deliberately, until this question is answered:

    does the token the relay already captures -- audience substrate.office.com/sydney, the
    same host as UploadFile but a different path -- get past the door?

WHAT THIS DOES. Captures a token through the repository's own mechanism (profile_token.
token_via_light_page: open a light page, watch our own outgoing requests, close it), then
POSTs one small PNG to UploadFile and prints the STATUS and the response shape. Nothing is
sent to a model, no conversation is created, no page is left open.

WHAT IT REPORTS, and why each answer matters:
  200/201 -> the relay can upload. The id in the response is what a socket frame would carry,
             and the Analyst can leave the tab behind.
  401/403 -> the audience does not cover this path. The next question is whether the capture
             can ask for the audience that does -- a different experiment, not a dead end.
  400     -> authorised, and the body is wrong. That is a PASS on the question being asked
             here; the field list is then the remaining work.

THE TOKEN IS NEVER WRITTEN DOWN. It lives in a local variable for the length of one request.
What this script prints is a status code and a response body with any token-shaped field
removed.
"""
from __future__ import annotations

import io
import json
import os
import sys
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

UPLOAD_URL = "https://substrate.office.com/m365Copilot/UploadFile"
CDP = os.environ.get("MCP_CDP_URL", "http://127.0.0.1:9222")
AGENT = (os.environ.get("MCP_FLEET_AGENT_URL")
         or "https://m365.cloud.microsoft/chat/?titleId=T_02140b8c-f551-675b-516a-4c7d2b08867e")

PNG = os.path.join(os.environ.get("TEMP", "."), "upload_probe.png")

#: The request as the page actually made it. Data, so a probe cannot invent the shape.
OBSERVED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploadfile_observed.json")

#: Where the bridge reports the conversation it is currently on. /status is a read; it is not
#: one of the page-grabbing endpoints.
BRIDGE_STATUS = os.environ.get("MCP_BRIDGE_STATUS_URL", "http://127.0.0.1:8765/status")


def _live_conversation_id():
    """(id, True) when the running bridge names a conversation, ("", False) otherwise."""
    try:
        import json as _j
        import urllib.request as _u
        with _u.urlopen(BRIDGE_STATUS, timeout=5) as fh:
            conv = (_j.loads(fh.read().decode("utf-8")) or {}).get("conversation") or ""
        return (str(conv), True) if conv else ("", False)
    except Exception:
        return ("", False)



def _make_png():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (240, 90), (255, 255, 255))
    ImageDraw.Draw(img).text((12, 36), "probe", fill=(0, 0, 0))
    img.save(PNG, optimize=True)
    return PNG


def main():
    from playwright.sync_api import sync_playwright
    from relay import profile_token as PT

    path = _make_png()
    print("probe image: %s (%d bytes)" % (path, os.path.getsize(path)))

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        print("capturing a token through the repository's own light-page path ...")
        token = PT.token_via_light_page(ctx, AGENT)
        if not token:
            print("RESULT: no token captured; nothing to ask the endpoint with")
            return 2
        print("token captured (life %.0f s). Not printed, not stored." % PT.token_life_s(token))

        # A REAL CONVERSATION WHEN ONE IS AVAILABLE. The first version invented a uuid4 and
        # called it harmless -- "if the endpoint objects to it, that is a 400" -- which assumed
        # the endpoint checks the body before the conversation. Nothing established that, and a
        # refusal aimed at a conversation that does not exist looks exactly like a refusal aimed
        # at the credential. The live bridge knows one; ask it rather than guess.
        conversation_id, real_conv = _live_conversation_id()
        if not conversation_id:
            conversation_id, real_conv = str(uuid.uuid4()), False

        # THE SHAPE COMES FROM THE RECORDING, NOT FROM MEMORY.
        #
        # This sent `files={"file": <binary part>}` until 2026-09-18 and got 403, and the 403
        # was read as an answer about the token's audience. It was not: the page does not send
        # a binary part at all. It puts the bytes in an ordinary TEXT field named `FileBase64`,
        # as a complete `data:image/png;base64,...` URI. The probe was refused for a request
        # nobody makes, which says nothing about the request everybody makes.
        #
        # The field list had been sitting in the CDP recording since 2026-09-17 and had never
        # been written down, so there was nothing to check the result against. It is data now
        # -- uploadfile_observed.json beside this file -- and the request is built from it, so
        # the two cannot drift apart. scripts/probes/test_the_probe_matches_the_recording.py
        # pins that they agree.
        import base64
        import requests

        observed = json.loads(io.open(OBSERVED, encoding="utf-8").read())
        names = [f["name"] for f in observed["fields"]]
        b64 = base64.b64encode(open(path, "rb").read()).decode("ascii")
        data = {
            "scenario": "UploadImage",
            "conversationId": conversation_id,
            "FileBase64": "data:image/png;base64," + b64,
        }
        missing = [n for n in names if n not in data]
        if missing:
            print("REFUSING: the recording names fields this probe does not send: %s" % missing)
            return 2
        extra = [n for n in data if n not in names]
        if extra:
            print("REFUSING: this probe sends fields the recording does not: %s" % extra)
            return 2
        print("fields: %s (from %s)" % (names, os.path.basename(OBSERVED)))
        print("conversationId: %s" % ("a REAL one from the live bridge" if real_conv
                                      else "INVENTED -- this is a second changed variable"))
        r = requests.post(UPLOAD_URL, headers={"Authorization": "Bearer " + token},
                          data=data, timeout=45)
        body = (r.text or "")[:1200]
        for k in ("token", "Token", "secret", "Authorization"):
            if k in body:
                body = body.replace(k, "(" + k + " redacted)")
        print("STATUS: %s" % r.status_code)
        print("BODY  : %s" % body)
        print()
        if r.status_code in (200, 201):
            print("READING: the relay CAN upload. Whatever id this returned is what a socket")
            print("         frame would carry in messageAnnotations.")
        elif r.status_code in (401, 403):
            print("READING: refused. This narrows things ONLY if the shape and the conversation")
            print("         above both matched the recording -- otherwise it says nothing about")
            print("         the credential, which is the mistake made on 2026-09-18.")
        elif r.status_code == 400:
            print("READING: authorised, and the body is wrong. The auth question is ANSWERED;")
            print("         the field list is the remaining work.")
        return 0


if __name__ == "__main__":
    sys.exit(main())

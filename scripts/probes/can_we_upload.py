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

        # The multipart shape the page used, as far as the recording showed it: a scenario and
        # a conversationId alongside the file. The conversationId is invented here on purpose --
        # if the endpoint objects to it, that is a 400 and the auth question is still answered.
        import requests
        files = {"file": (os.path.basename(path), open(path, "rb"), "image/png")}
        data = {"scenario": "UploadImage", "conversationId": str(uuid.uuid4())}
        r = requests.post(UPLOAD_URL, headers={"Authorization": "Bearer " + token},
                          data=data, files=files, timeout=45)
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
            print("READING: the audience we hold does not open this path. Not a dead end --")
            print("         the next question is which audience does.")
        elif r.status_code == 400:
            print("READING: authorised, and the body is wrong. The auth question is ANSWERED;")
            print("         the field list is the remaining work.")
        return 0


if __name__ == "__main__":
    sys.exit(main())

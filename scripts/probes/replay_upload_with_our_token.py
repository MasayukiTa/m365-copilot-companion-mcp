# -*- coding: utf-8 -*-
"""The one-variable experiment: the page's own UploadFile request, with ONLY the token swapped.

WHY EVERY EARLIER ATTEMPT FAILED TO ANSWER ANYTHING. The question is whether the token the relay
captures -- audience substrate.office.com/sydney -- is accepted at
https://substrate.office.com/m365Copilot/UploadFile. Two probes were run on 2026-09-18 and both
came back 403, and neither result was about the token, because each differed from the real
request in several ways at once:

  attempt 1  binary multipart part named "file"; invented conversationId; our token
  attempt 2  the right three fields; a real conversationId; our token
             -- and still wrong, because the real body has SIX fields, not three:
                scenario, conversationId, FileBase64, and optionsSets THREE TIMES.
                The field list had been transcribed from a 400-character truncated body.

Attempt 2 also sent only Authorization, while the page sends origin, referer, x-anchormailbox,
x-gptid, x-scenario and x-variants. x-anchormailbox routes the request; origin and referer are
checked by many Microsoft endpoints. A refusal of a request that differs in six ways says
nothing about any one of them.

SO STOP RECONSTRUCTING IT. This takes the page's actual request -- bytes and headers, as sent --
and re-issues exactly that, replacing the Authorization header and nothing else. One variable.
Then, and only then, a 200 or a 403 is about the credential.

WHAT IS HELD AND WHAT IS WRITTEN. The page's own Authorization value is never read, never
logged, and never re-sent: it is replaced before the request is built. The other headers are
copied through in memory and are NOT written to disk -- several of them identify the account.
What lands in the output file is the status, the response body with token-shaped keys scrubbed,
and the names of the headers that were forwarded.

A NOTE ON WHAT A 200 WOULD MEAN. It would mean the relay can upload, and the docId in the
response is what a socket frame carries as messageAnnotations[0].id. It would NOT by itself mean
the tab can be dropped: the id's lifetime and whether a socket-sent annotation is accepted are
separate questions, and this probe does not ask them.
"""
from __future__ import annotations

import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

CDP = os.environ.get("MCP_CDP_URL", "http://127.0.0.1:9222")
UPLOAD_URL = "https://substrate.office.com/m365Copilot/UploadFile"
WANT = "uploadfile"
OUT = os.path.join(os.environ.get("TEMP", "."), "replay_upload_%d.jsonl" % int(time.time()))
PNG = os.path.join(os.environ.get("TEMP", "."), "replay_upload_probe.png")
WAIT_S = float(os.environ.get("MCP_REPLAY_WAIT_S", "25"))

#: Headers a replay must NOT forward. The pseudo-headers are HTTP/2 framing, content-length is
#: recomputed, and authorization is the one variable under test.
_DROP = {":authority", ":method", ":path", ":scheme", "content-length", "authorization",
         "accept-encoding", "host"}


def _write(rec):
    try:
        with open(OUT, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _make_png():
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (240, 90), (255, 255, 255))
    ImageDraw.Draw(img).text((12, 36), "replay", fill=(0, 0, 0))
    img.save(PNG, optimize=True)
    return PNG


def _scrub(text):
    out = str(text or "")
    for k in ("token", "Token", "secret", "Secret", "authorization", "Authorization"):
        out = out.replace(k, "(" + k + " scrubbed)")
    return out


def main():
    import requests
    from playwright.sync_api import sync_playwright

    from relay import profile_token as PT
    from relay.agent_profiles import ANALYST, upload_file

    png = _make_png()
    print("probe image: %s (%d bytes)" % (png, os.path.getsize(png)))
    print("recording to %s" % OUT)
    _write({"event": "start", "ts": time.time()})

    captured = {}

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        before = len(ctx.pages)
        print("tabs before: %d" % before)

        print("capturing a token through the repository's own light-page path ...")
        token = PT.token_via_light_page(ctx, ANALYST.url)
        if not token:
            print("RESULT: no token captured; nothing to ask the endpoint with")
            return 2
        print("our token captured (life %.0f s). Never printed, never stored."
              % PT.token_life_s(token))

        page = ctx.new_page()
        try:
            def _on_request(req):
                if WANT not in (req.url or "").lower() or captured:
                    return
                try:
                    body = req.post_data_buffer
                    hdrs = req.all_headers()
                except Exception as exc:
                    _write({"event": "capture_failed", "error": type(exc).__name__})
                    return
                captured["body"] = body
                captured["headers"] = {k: v for k, v in hdrs.items()
                                       if k.lower() not in _DROP}
                captured["url"] = req.url
                print("captured the page's own request: %d body bytes, %d headers forwarded"
                      % (len(body or b""), len(captured["headers"])))

            page.on("request", _on_request)
            print("opening the analyst surface ...")
            page.goto(ANALYST.url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            print("putting the file into the page's own file input ...")
            if not upload_file(page, png):
                print("RESULT: the page never accepted the file; nothing to replay")
                return 3
            deadline = time.time() + WAIT_S
            while time.time() < deadline and not captured:
                page.wait_for_timeout(500)
        finally:
            try:
                page.close()
            except Exception:
                pass
            print("tabs after: %d (before %d)" % (len(ctx.pages), before))

    if not captured:
        print("RESULT: the page's request was never seen; nothing to replay")
        return 4

    headers = dict(captured["headers"])
    headers["Authorization"] = "Bearer " + token
    forwarded = sorted(k for k in headers if k.lower() != "authorization")
    print("replaying with OUR token. forwarded headers: %s" % forwarded)

    r = requests.post(UPLOAD_URL, headers=headers, data=captured["body"], timeout=45)
    body = _scrub(r.text)[:1500]
    _write({"event": "replay", "ts": time.time(), "status": r.status_code,
            "forwarded_header_names": forwarded, "body": body})
    print("STATUS: %s" % r.status_code)
    print("BODY  : %s" % body)
    print()

    if r.status_code in (200, 201):
        print("READING: the relay's token IS accepted here. The docId in that body is what a")
        print("         socket frame carries as messageAnnotations[0].id. Separate questions")
        print("         remain: the id's lifetime, and whether a socket-sent annotation is")
        print("         accepted -- this probe did not ask them.")
    elif r.status_code in (401, 403):
        print("READING: refused, and THIS TIME it is about the credential -- the body and every")
        print("         forwarded header are the page's own. The next question is which audience")
        print("         does open it, and whether the capture can ask for that one.")
    else:
        print("READING: neither accepted nor refused on the credential. Read the body: the")
        print("         replay may differ from the original in a way not yet accounted for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Watch the PAGE make the UploadFile call, and record the two things never captured.

WHY THIS EXISTS. scripts/probes/uploadfile_observed.json holds the request shape as the page
made it on 2026-09-17, and says plainly what that recording did NOT contain: the Authorization
header (the recorder never reads it) and the response (35 POST events, zero response bodies).
Both gaps were then filled in by assumption -- a probe was built and a 403 read as "the audience
we hold does not cover this path" while it had never been established that the page uses a
bearer token for this call at all.

So this makes the page do it, with a listener attached, and writes down only what distinguishes
one caller from another.

NARROWER THAN AN ANALYZE ON PURPOSE. The earlier observation came from running a whole ANALYZE
turn: a conversation, a question, a model. None of that is needed to see an upload. This opens
the agent surface, puts a file into its <input type=file> exactly as relay/agent_profiles.
upload_file does, and stops. No turn is sent, no question is asked, nothing reaches a model.

WHAT IT RECORDS, AND WHAT IT WILL NOT:

    header names                  (names only)
    authorization_present         bool
    authorization_scheme          the first word only -- "Bearer", "Basic" -- never the rest
    cookie_header_present         bool
    multipart field names         (names only; the file bytes are not written)
    response status and body      the body IS written: it is the id the protocol needs, and it
                                  is not a credential. Any token-shaped key is scrubbed anyway.

No header value is stored, no token is decoded, and the raw request is never dumped.

IT CLEANS UP AFTER ITSELF. Tab counts are printed before and after, because one endpoint
touched carelessly in this repository once lit a self-feeding loop that reached 40 Edge
processes and 4.3 GB. The page this opens is closed in a finally.
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
WANT = "uploadfile"
OUT = os.path.join(os.environ.get("TEMP", "."), "real_upload_%d.jsonl" % int(time.time()))
PNG = os.path.join(os.environ.get("TEMP", "."), "observe_upload_probe.png")

#: How long to wait for the upload request after the file is set.
WAIT_S = float(os.environ.get("MCP_OBSERVE_UPLOAD_WAIT_S", "25"))


def _write(rec):
    try:
        with open(OUT, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _make_png():
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (240, 90), (255, 255, 255))
    ImageDraw.Draw(img).text((12, 36), "observe", fill=(0, 0, 0))
    img.save(PNG, optimize=True)
    return PNG


def _scrub(text):
    out = str(text or "")
    for k in ("token", "Token", "secret", "Secret", "authorization", "Authorization"):
        out = out.replace(k, "(" + k + " scrubbed)")
    return out


def _auth_shape(headers):
    """Names, presence, scheme word, cookie presence. Never a value."""
    scheme, cookie = "", False
    for k, v in (headers or {}).items():
        lk = k.lower()
        if lk == "authorization":
            scheme = str(v or "").strip().split(" ")[0][:16]
        elif lk == "cookie":
            cookie = True
    return {
        "header_names": sorted((headers or {}).keys()),
        "authorization_present": bool(scheme),
        "authorization_scheme": scheme,
        "cookie_header_present": cookie,
    }


def _fields(body):
    if not body:
        return []
    return [p.split('"')[0] for p in str(body).split('name="')[1:]][:20]


def main():
    from playwright.sync_api import sync_playwright

    from relay.agent_profiles import ANALYST, upload_file

    png = _make_png()
    print("probe image: %s (%d bytes)" % (png, os.path.getsize(png)))
    print("recording to %s" % OUT)
    _write({"event": "start", "ts": time.time()})

    seen = {"request": False, "response": False}

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        before = len(ctx.pages)
        print("tabs before: %d" % before)
        page = ctx.new_page()
        try:
            def _on_request(req):
                if WANT not in (req.url or "").lower():
                    return
                rec = {"event": "upload_request", "ts": time.time(), "url": req.url[:200],
                       "method": req.method}
                try:
                    rec.update(_auth_shape(req.all_headers()))
                except Exception as exc:
                    rec["headers_unreadable"] = type(exc).__name__
                try:
                    rec["multipart_fields"] = _fields(req.post_data)
                    rec["body_len"] = len(req.post_data or "")
                except Exception:
                    rec["multipart_fields"] = []
                seen["request"] = True
                _write(rec)
                print("SAW REQUEST: auth=%s scheme=%r cookie=%s fields=%s" % (
                    rec.get("authorization_present"), rec.get("authorization_scheme"),
                    rec.get("cookie_header_present"), rec.get("multipart_fields")))

            def _on_response(resp):
                if WANT not in (resp.url or "").lower():
                    return
                rec = {"event": "upload_response", "ts": time.time(), "status": resp.status,
                       "url": resp.url[:200]}
                try:
                    rec["body"] = _scrub(resp.text())[:2000]
                except Exception as exc:
                    rec["body_unreadable"] = type(exc).__name__
                seen["response"] = True
                _write(rec)
                print("SAW RESPONSE: status=%s" % resp.status)
                print("BODY: %s" % str(rec.get("body"))[:600])

            page.on("request", _on_request)
            page.on("response", _on_response)

            print("opening the analyst surface ...")
            page.goto(ANALYST.url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)

            print("putting the file into the page's own file input ...")
            if not upload_file(page, png):
                print("RESULT: the page never accepted the file; no upload was made")
                _write({"event": "upload_not_accepted", "ts": time.time()})
                return 2

            deadline = time.time() + WAIT_S
            while time.time() < deadline and not (seen["request"] and seen["response"]):
                page.wait_for_timeout(500)
        finally:
            try:
                page.close()
            except Exception:
                pass
            after = len(ctx.pages)
            print("tabs after: %d (before %d)" % (after, before))
            _write({"event": "stop", "ts": time.time(), "tabs_before": before,
                    "tabs_after": after, "saw": dict(seen)})

    if not seen["request"]:
        print("RESULT: no UploadFile request was seen. The file input may not have fired.")
        return 3
    if not seen["response"]:
        print("RESULT: the request was seen and the response was not. Still the open gap.")
        return 4
    print("RESULT: both halves captured. See %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())

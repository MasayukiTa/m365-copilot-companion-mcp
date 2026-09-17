# -*- coding: utf-8 -*-
"""Observe the UploadFile call precisely enough to decide whether we can make it ourselves.

WHAT IS ALREADY KNOWN, from the first recording (2026-09-17 13:37):

  POST https://substrate.office.com/m365Copilot/UploadFile   (multipart, scenario=UploadImage)
  and the outgoing ChatHub frame then carries
  "messageAnnotations":[{"id":"0-wjp-d3-...","messageAnnotationMetadata":{"@type":"File",
   "fileType":"png","fileName":"monthly.png"},"messageAnnotationType":"ImageFile"}]

So the protocol HAS a place for an attachment, and transport_policy.py's fixed rule -- "a
socket has nowhere to put a local file" -- is contradicted by observation. The <input
type=file> is the UI's door, not the protocol's requirement.

WHAT THIS RECORDS: the multipart field list, so the request shape is known, and what
UploadFile RETURNS, since that is where the annotation id must come from.

WHAT IT DELIBERATELY DOES NOT RECORD: anything about the token. An earlier draft decoded the
Authorization header for its audience, to judge whether the credential the relay already holds
would be accepted at this path. Discarding the bytes afterwards does not change what that is,
and whether to establish it is the operator's decision rather than mine. The request shape
answers what the protocol needs; it does not answer what authorises it, and those are
different questions that should be asked separately.
"""
import io
import json
import os
import sys
import threading
import time
import urllib.request

import websocket

OUT = os.path.join(os.environ.get("TEMP", "."), "upload_call_%d.jsonl" % int(time.time()))
DEADLINE = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 1200)
WANT = "uploadfile"

_lock = threading.Lock()
_seen = set()


def _write(rec):
    with _lock:
        with io.open(OUT, "a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# THE TOKEN IS NOT LOOKED AT. An earlier version decoded the Authorization header for its
# aud/exp claims, to judge whether the audience we already hold would be accepted here. That
# is credential exploration however carefully it discards the bytes, and it is the operator's
# call, not mine. What is recorded is the request SHAPE and the response, which answers what
# the protocol needs without touching what authorises it.


def targets():
    try:
        return json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5))
    except Exception:
        return []


def watch(t):
    tid = t.get("id")
    ws = None
    pending = {}
    try:
        ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=10,
                                         suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        _write({"event": "watching", "target": tid, "url": (t.get("url") or "")[:160]})
        ws.settimeout(2.0)
        nextid = 100
        while time.time() < DEADLINE:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            m = msg.get("method") or ""
            p = msg.get("params") or {}

            if m == "Network.requestWillBeSent":
                req = p.get("request") or {}
                url = req.get("url") or ""
                if WANT in url.lower():
                    hdrs = req.get("headers") or {}
                    body = req.get("postData") or ""
                    fields = [s.split('"')[1] for s in body.split("name=") if '"' in s][:20]
                    pending[p.get("requestId")] = url
                    _write({"event": "upload_request", "ts": time.time(), "url": url[:200],
                            "header_names": sorted(hdrs.keys()),
                            "has_authorization": any(k.lower()=="authorization" for k in hdrs),
                            "multipart_fields": fields,
                            "body_len": len(body),
                            "body_head": body[:1500]})
            elif m == "Network.responseReceived":
                rid = p.get("requestId")
                if rid in pending:
                    nextid += 1
                    ws.send(json.dumps({"id": nextid, "method": "Network.getResponseBody",
                                        "params": {"requestId": rid}}))
                    _write({"event": "upload_response_headers", "ts": time.time(),
                            "status": (p.get("response") or {}).get("status"),
                            "url": pending[rid][:200]})
            elif msg.get("id") and "result" in msg and "body" in (msg.get("result") or {}):
                _write({"event": "upload_response_body", "ts": time.time(),
                        "body": (msg["result"].get("body") or "")[:3000]})
    except Exception as exc:
        _write({"event": "watch_failed", "target": tid,
                "error": "%s: %s" % (type(exc).__name__, exc)})
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass


def main():
    print("recording to %s" % OUT)
    _write({"event": "start", "ts": time.time()})
    while time.time() < DEADLINE:
        for t in targets():
            if t.get("type") != "page":
                continue
            tid = t.get("id")
            if tid in _seen or not t.get("webSocketDebuggerUrl"):
                continue
            _seen.add(tid)
            threading.Thread(target=watch, args=(t,), daemon=True).start()
        time.sleep(2.0)
    _write({"event": "stop", "ts": time.time()})
    print("done; %d target(s)" % len(_seen))


if __name__ == "__main__":
    main()

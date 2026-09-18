# -*- coding: utf-8 -*-
"""Record the page's ChatHub frames WHOLE, and say so loudly when one is cut.

WHY THIS EXISTS IN THE REPOSITORY AT ALL. The frame that answers the open socket question was
captured on 2026-09-17 by a throwaway script in the temp directory, which wrote `payload[:4000]`.
The frame was 4811 characters. So the observation that mattered -- the client's own outgoing
frame carrying messageAnnotations -- was cut by 811 characters, does not parse as JSON, and is
useless as a template. The instrument that produced the evidence was not in the repository and
its cap was invisible.

That is the third time in one day a truncated record was mistaken for a record: a 400-character
body_head produced three wrong UploadFile probes, and _clean's 60-character cap ended a job's
provenance mid-sentence. The rule this repository already states for evidence_trace applies
here -- "silently keeping the first 4,000 characters means a destination named at character
4,001 is not merely missed, it is missed by a check that then reports nothing wrong".

SO: the cap is large, and A CUT IS RECORDED AS A CUT. Every frame row carries `declared_len`,
and a truncated one carries `truncated: true` plus how many characters went missing. A reader
can tell a whole frame from a fragment without parsing it.

WHAT IS NEVER RECORDED. The websocket URL. relay/chathub.build_ws_url puts the access token in
the query string, so the URL is a credential -- it is not written, not hashed, not logged. Only
frame payloads are kept, and frames do not carry the token.

WHY IT MATTERS THAT FRAMES ARE CAPTURED RATHER THAN COMPOSED. relay/chathub.py sends the
client's own captured template with this turn's ids and text written in, and its docstring
records the measurement: a COMPOSED frame was REJECTED on 2026-08-20, the captured shape was
accepted. Anything built by hand here would be answering a different question.

    python scripts/probes/record_socket_frames.py                  # 600s, annotated frames only
    python scripts/probes/record_socket_frames.py 300 --all        # everything, 300s
    python scripts/probes/record_socket_frames.py 600 --marker messageAnnotations

An annotated frame appears only when a message is SENT WITH AN ATTACHMENT. Attaching alone
produces the UploadFile POST and no chat frame -- see observe_real_upload.py.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

CDP = os.environ.get("MCP_CDP_URL", "http://127.0.0.1:9222")
OUT = os.path.join(os.environ.get("TEMP", "."), "socket_frames_%d.jsonl" % int(time.time()))

#: Generous on purpose. The frame that mattered was 4811 characters and was lost to a 4,000
#: cap; a bound exists so one pathological frame cannot fill the disk, not to trim evidence.
MAX_PAYLOAD = 262144


def _write(rec):
    try:
        with open(OUT, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _frame_row(event, target, payload, marker, keep_all):
    """One row, with the cut announced. Returns None when the frame is not wanted."""
    text = payload or ""
    if not keep_all and marker and marker not in text:
        return None
    row = {"event": event, "ts": time.time(), "target": target,
           "declared_len": len(text)}
    if len(text) > MAX_PAYLOAD:
        row["payload"] = text[:MAX_PAYLOAD]
        row["truncated"] = True
        row["cut_chars"] = len(text) - MAX_PAYLOAD
    else:
        row["payload"] = text
        row["truncated"] = False
    # A frame that should parse and does not is worth knowing about at capture time, not at
    # read time three weeks later.
    head = text.split(chr(30))[0] if text else ""
    try:
        json.loads(head)
        row["first_frame_parses"] = True
    except Exception:
        row["first_frame_parses"] = False
    return row


def targets():
    import urllib.request

    try:
        with urllib.request.urlopen(CDP + "/json/list", timeout=5) as fh:
            return json.loads(fh.read().decode("utf-8"))
    except Exception:
        return []


def watch(target, deadline, marker, keep_all):
    import websocket

    tid = target.get("id")
    url = target.get("webSocketDebuggerUrl")
    if not url:
        return
    ws = None
    try:
        # suppress_origin: without it Edge answers 403 on the devtools socket.
        ws = websocket.create_connection(url, timeout=10, suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        # The TARGET's page url is recorded (it names the surface, not a credential); the
        # WEBSOCKET url is never touched -- it carries the access token.
        _write({"event": "watching", "target": tid,
                "page_url": (target.get("url") or "")[:160]})
        while time.time() < deadline:
            try:
                ws.settimeout(2.0)
                msg = json.loads(ws.recv())
            except Exception:
                continue
            m = msg.get("method") or ""
            if m not in ("Network.webSocketFrameSent", "Network.webSocketFrameReceived"):
                continue
            p = msg.get("params") or {}
            payload = ((p.get("response") or {}).get("payloadData") or "")
            row = _frame_row(m.split(".")[-1], tid, payload, marker, keep_all)
            if row is None:
                continue
            _write(row)
            flag = " TRUNCATED" if row.get("truncated") else ""
            print("%s %s len=%d parses=%s%s" % (
                time.strftime("%H:%M:%S"), row["event"], row["declared_len"],
                row["first_frame_parses"], flag))
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
    seconds = 600.0
    marker = "messageAnnotations"
    keep_all = False
    argv = list(sys.argv[1:])
    if argv and argv[0].replace(".", "", 1).isdigit():
        seconds = float(argv.pop(0))
    while argv:
        a = argv.pop(0)
        if a == "--all":
            keep_all = True
        elif a == "--marker" and argv:
            marker = argv.pop(0)
    deadline = time.time() + seconds

    print("recording to %s" % OUT)
    print("keeping %s, cap %d chars, cuts are recorded as cuts"
          % ("every frame" if keep_all else "frames containing %r" % marker, MAX_PAYLOAD))
    _write({"event": "start", "ts": time.time(), "marker": None if keep_all else marker,
            "max_payload": MAX_PAYLOAD})

    seen = set()
    while time.time() < deadline:
        for t in targets():
            if t.get("type") != "page":
                continue
            tid = t.get("id")
            if tid in seen or not t.get("webSocketDebuggerUrl"):
                continue
            seen.add(tid)
            threading.Thread(target=watch, args=(t, deadline, marker, keep_all),
                             daemon=True).start()
        time.sleep(2.0)
    _write({"event": "stop", "ts": time.time(), "targets": len(seen)})
    print("done; %d target(s). See %s" % (len(seen), OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""OBSERVE which calls the chat page makes to list and read conversations. Sends nothing.

WHY. Design D (a resident component that runs tool blocks from a person's own chat) cannot be
built until we know how a conversation of the configured agent is found: which request lists the
user's conversations, and what shape its answer has (does an item carry the owner, the agent id,
the last message?). Nothing in the repository says (design 設計_人が直接チャットしてもツールが動く経路,
section 1.4 and the not-verified list, items 1-3 and 5). This probe opens the same light page the
token capture opens (relay/profile_token.py: loads the signed-in chat page, sends no message),
listens, and prints only SHAPES.

WHAT IT PRINTS (and nothing else):
    method, host, status, how many times
    the path as a template, every id-like segment replaced by <id>
    the query parameter NAMES (values are dropped)
    for responses of history / conversation looking paths with a JSON body: the field NAMES as
    paths (a.b[].c), every id-like key replaced by <id>
    the token's audience (the `aud` claim of the Authorization header; it names a resource, it is
    not a credential) -- the token itself is never stored or printed
It never prints a value, a body, a URL, a header value, an id, or the agent URL.

IT NEVER SENDS A TURN. It does not type, click, or call any endpoint itself. The only request made
is the page's own load. Like the token capture, the page carries no Copilot turn.

HOW IT IS RUN (the commander, later, in the hidden Edge on port 9350 -- the same one
scripts/probes/preflight_ab.py takes its token from):

    python -I -X utf8 scripts/probes/resident_observe.py observe --url-file F [--port 9350] [--seconds 25]

Refuses ports 9222-9224 (the live Edge) and 8000 / 8765 (the product servers).

    python -I -X utf8 scripts/probes/resident_observe.py --check

runs the guard (relay/resident_guard.py) against a fixed matrix of fake conversations and the
one-resident lock in a temporary directory; no network, no browser.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FORBIDDEN_PORTS = (8000, 8765, 9222, 9223, 9224)
DEFAULT_EDGE_PORT = 9350
ID = "<id>"

_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX = re.compile(r"^[0-9a-f]{8,}$", re.I)
_DIGITS = re.compile(r"^\d{5,}$")
_STATIC = re.compile(r"\.(js|mjs|css|png|jpe?g|gif|svg|ico|woff2?|ttf|map|webp|json\.br)$", re.I)
_CANDIDATE = re.compile(r"(conversation|history|thread|session|chat|message|insight|sydney|"
                        r"recent|agent)", re.I)
MAX_FIELDS = 200
MAX_DEPTH = 7


# ---------------------------------------------------------------- pure helpers (unit-tested)

def refuse_port(port):
    """Raises SystemExit when the port is one this probe must never touch."""
    if int(port) in FORBIDDEN_PORTS:
        raise SystemExit("refused: port %s is a live Edge or a product server" % port)


def looks_like_id(text):
    """True for a GUID, hex run, long number, oid@tid, or a long mixed token."""
    s = str(text or "")
    if not s:
        return False
    if "@" in s or _GUID.match(s) or _HEX.match(s) or _DIGITS.match(s):
        return True
    if len(s) >= 16 and re.search(r"\d", s) and re.search(r"[A-Za-z]", s):
        return True
    if len(s) >= 24 and re.match(r"^[A-Za-z0-9_\-=]+$", s):
        return True
    return False


def mask_segment(seg):
    return ID if looks_like_id(seg) else seg


def split_url(url):
    """(host, path_template, [query names]). Values, fragments and credentials are dropped."""
    from urllib.parse import urlsplit, unquote
    parts = urlsplit(str(url or ""))
    segs = [mask_segment(unquote(s)) for s in parts.path.split("/")]
    names = []
    for pair in (parts.query or "").split("&"):
        name = pair.split("=", 1)[0]
        if name and name not in names:
            names.append(mask_segment(unquote(name)))
    return (parts.hostname or "", "/".join(segs) or "/", sorted(names))


def field_names(obj, max_depth=MAX_DEPTH, max_fields=MAX_FIELDS):
    """Sorted field-name paths of a parsed JSON value: a.b[].c. Keys that look like ids become
    <id>. Values are never read into the result."""
    found = set()

    def walk(node, prefix, depth):
        if len(found) >= max_fields or depth > max_depth:
            return
        if isinstance(node, dict):
            for key in node:
                path = (prefix + "." if prefix else "") + mask_segment(str(key))
                found.add(path)
                walk(node[key], path, depth + 1)
        elif isinstance(node, list):
            for item in node[:20]:
                walk(item, prefix + "[]", depth + 1)

    walk(obj, "", 0)
    return sorted(found)[:max_fields]


def is_static(path):
    return bool(_STATIC.search(str(path or "").split("?")[0]))


def is_candidate(path):
    return bool(_CANDIDATE.search(str(path or "")))


def audience_of(authorization):
    """The `aud` claim of a Bearer value, or "". The token itself is not kept."""
    try:
        token = str(authorization).split(None, 1)[1]
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        aud = json.loads(base64.urlsafe_b64decode(body)).get("aud")
        return str(aud)[:80] if isinstance(aud, str) else ""
    except Exception:
        return ""


def summarize(events):
    """events: dicts {method, url, status, json (parsed body or None), authorization (str)}.
    Returns the shape rows, de-duplicated, with no value from any event."""
    rows = {}
    for ev in events:
        host, tmpl, query = split_url(ev.get("url"))
        if is_static(tmpl):
            continue
        key = (str(ev.get("method") or "").upper(), host, tmpl, tuple(query), ev.get("status"))
        row = rows.setdefault(key, {"method": key[0], "host": host, "path": tmpl, "query": list(query),
                                    "status": ev.get("status"), "count": 0, "fields": [], "aud": ""})
        row["count"] += 1
        if is_candidate(tmpl) and ev.get("json") is not None and not row["fields"]:
            row["fields"] = field_names(ev.get("json"))
        if not row["aud"] and ev.get("authorization"):
            row["aud"] = audience_of(ev.get("authorization"))
    return sorted(rows.values(), key=lambda r: (r["host"], r["path"], r["method"], str(r["status"])))


def format_report(rows):
    lines = ["calls: %d shapes" % len(rows)]
    for r in rows:
        lines.append("%s %s%s status=%s x%d%s" % (
            r["method"], r["host"], r["path"], r["status"], r["count"],
            (" aud=" + r["aud"]) if r["aud"] else ""))
        if r["query"]:
            lines.append("    query names: " + ", ".join(r["query"]))
        if r["fields"]:
            lines.append("    fields: " + ", ".join(r["fields"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- transports

class FakeTransport:
    """Hands back fixed events. The unit tests and --check use it; nothing touches a network."""

    def __init__(self, events):
        self.events = list(events)

    def collect(self):
        return list(self.events)


class EdgeTransport:
    """Opens the light page in the hidden Edge over CDP, listens for `seconds`, closes it.
    The same shape as relay/profile_token.token_via_light_page: no message is sent."""

    def __init__(self, url, port, seconds):
        self.url, self.port, self.seconds = url, int(port), float(seconds)

    def collect(self):
        from playwright.sync_api import sync_playwright
        events = []
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:%d" % self.port)
            ctx = browser.contexts[0]
            print("tabs before: %d" % len(ctx.pages))
            page = ctx.new_page()
            try:
                def on_response(resp):
                    try:
                        req = resp.request
                        ev = {"method": req.method, "url": resp.url, "status": resp.status,
                              "json": None, "authorization": (req.headers or {}).get("authorization", "")}
                        ctype = (resp.headers or {}).get("content-type", "")
                        if "json" in ctype.lower() and is_candidate(split_url(resp.url)[1]):
                            try:
                                ev["json"] = resp.json()
                            except Exception:
                                pass
                        events.append(ev)
                    except Exception:
                        pass

                page.on("response", on_response)
                try:
                    page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                end = time.time() + self.seconds
                while time.time() < end:
                    page.wait_for_timeout(250)
            finally:
                try:
                    page.close()
                except Exception:
                    pass
            print("tabs after: %d" % len(ctx.pages))
        return events


# ---------------------------------------------------------------- --check (calls the guard and the lock)

def run_check(out=print):
    """The guard against a fixed matrix, and the one-resident lock in a temporary directory.
    Returns 0 when every row behaves as stated."""
    from relay import resident_guard as G
    me, agent = "11111111-1111-1111-1111-111111111111", "agent-aaaa"

    def conv(**kw):
        base = {"owner_oid": me, "agent_id": agent, "last_message": {"role": "assistant", "block_id": "b1"}}
        base.update(kw)
        return base

    matrix = [
        ("own conversation, new block", conv(), set(), True, G.OK),
        ("another person's conversation", conv(owner_oid="22222222-2222-2222-2222-222222222222"), set(),
         False, G.OWNER_MISMATCH),
        ("another agent", conv(agent_id="agent-bbbb"), set(), False, G.AGENT_MISMATCH),
        ("block already executed", conv(), {"b1"}, False, G.ALREADY_EXECUTED),
        ("owner not readable", conv(owner_oid=None), set(), False, G.OWNER_UNREADABLE),
        ("no conversation", None, set(), False, G.NO_CONVERSATION),
    ]
    bad = 0
    for name, c, ledger, want_ok, want_reason in matrix:
        ok, reason = G.may_execute(c, me, agent, ledger)
        good = (ok, reason) == (want_ok, want_reason)
        bad += 0 if good else 1
        out("%-34s -> %s %s%s" % (name, ok, reason, "" if good else "   UNEXPECTED"))
    with tempfile.TemporaryDirectory(prefix="resident_check_") as d:
        a = G.ResidentLock(d, me, pid=os.getpid())
        b = G.ResidentLock(d, me, pid=os.getpid() + 1, alive=lambda pid: True)
        steps = [("first resident takes the lock", a.acquire(), (True, G.LOCK_TAKEN)),
                 ("second resident is refused", b.acquire(), (False, G.LOCK_HELD)),
                 ("first releases", a.release(), (True, G.LOCK_RELEASED))]
        for name, got, want in steps:
            good = got == want
            bad += 0 if good else 1
            out("%-34s -> %s %s%s" % (name, got[0], got[1], "" if good else "   UNEXPECTED"))
    out("check: %s" % ("all as expected" if not bad else "%d unexpected" % bad))
    return 1 if bad else 0


# ---------------------------------------------------------------- entry

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="run the guard and lock matrix; no network")
    sub = ap.add_subparsers(dest="cmd")
    ob = sub.add_parser("observe", help="open the light page in the hidden Edge and print call shapes")
    ob.add_argument("--url-file", required=True)
    ob.add_argument("--port", type=int, default=DEFAULT_EDGE_PORT)
    ob.add_argument("--seconds", type=float, default=25.0)
    args = ap.parse_args(argv)
    if args.check:
        return run_check()
    if args.cmd != "observe":
        ap.print_help()
        return 2
    refuse_port(args.port)
    with open(args.url_file, encoding="ascii", errors="replace") as fh:
        url = fh.read().strip()
    rows = summarize(EdgeTransport(url, args.port, args.seconds).collect())
    print(format_report(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())

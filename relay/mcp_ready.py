# -*- coding: utf-8 -*-
"""MCP-ready creation gate and MCP-initialize detector for fleet conversations.

WHY (measured 2026-10-15, see Documents\\フリート設計見直し_20261014\\ツール欠落の原因.md)
Copilot Studio binds the MCP connector (initialize + tools/list) when a CONVERSATION IS CREATED.
A conversation created while this PC's MCP server (127.0.0.1:8000, reached through the devtunnel)
is unreachable stays WITHOUT the connector tools for its whole life; the server coming back does
not re-bind it. 2 runs (10-14 18:30, 10-15 08:19) lost 8/8 and 24/24 first conversations this way.

Two fixes, both here, both pure of side effects on the browser (Copilot never runs in a tab):

1. CREATION GATE (`ReadyGate`). A fleet worker may not send the first message of a fresh
   conversation (first goal, fresh replay, recycle) until the local server answers /health with
   status ok, has been up for MIN_UPTIME_S, and (when a tunnel is configured) the tunnel path
   answers and reaches the SAME server_pid. Otherwise the worker stays `ready` with the reason
   `waiting for MCP server (...)`, registered in status.json `token_wait` (token_provider hold).
   The verdict is cached RECHECK_S so 24 workers cost one probe per 5 s, never a busy loop.

2. INITIALIZE DETECTOR (`InitDetector`). After the first send of a fresh conversation an MCP
   `initialize` from Copilot Studio must reach the server within INIT_WINDOW_S (measured ~5 s).
   `.fleet/mcp_inbound.jsonl` rows carry method / ua / ts / a hash of the MCP session id but NO
   conversation id, so a row cannot be tied to a conversation. The association is by time window
   and by COUNTING: each initialize satisfies at most ONE expectation (the earliest unmatched send
   at or before it). N sends in the same few seconds need N initializes. A foreign initialize
   (a reviewer, the bridge) can satisfy an expectation it does not belong to; that errs toward
   "attached" (no recycle), never toward a wrong recycle.
   No initialize in the window while the server was healthy for the whole window => the
   conversation is `tools not attached`; the worker recycles it (new conversation, same goal),
   bounded by MAX_TOOLS_RECYCLES, then goes STUCK with the cause `ツール未接続`
   (relay/stuck_cause.py).

Reply-based trigger: `reply_says_no_tools` recognises the model's own statement that it has no
call_tool (or that its only functions are UniversalSearchTool/AskTool/CaptureContextTool).

No credential, no request body and no tool argument is read or kept here.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import namedtuple

LOCAL_HEALTH_URL = os.environ.get("MCP_READY_HEALTH_URL", "http://127.0.0.1:8000/health")
#: The server must have been up this long before a conversation may be created against it.
MIN_UPTIME_S = float(os.environ.get("MCP_READY_MIN_UPTIME_S", "10"))
#: A verdict is reused this long (the recheck pace of a waiting worker).
RECHECK_S = float(os.environ.get("MCP_READY_RECHECK_S", "5"))
#: An initialize must arrive this long after the first send.
INIT_WINDOW_S = float(os.environ.get("MCP_READY_INIT_WINDOW_S", "45"))
#: Fresh conversations a worker may be given for missing tools before it is STUCK.
MAX_TOOLS_RECYCLES = int(os.environ.get("MCP_READY_MAX_TOOLS_RECYCLES", "3"))
#: User-agent fragment of the Copilot Studio connector client.
INIT_UA = os.environ.get("MCP_READY_INIT_UA", "CopilotStudio")
#: An initialize this much BEFORE the send may still belong to it (clock rounding only).
_SEND_SLACK_S = 1.0
#: Events older than this are dropped.
_EVENT_KEEP_S = 900.0
#: Initial look-back when the inbound file is first opened.
_FIRST_READ_BYTES = 512 * 1024

REASON_PREFIX = "waiting for MCP server"

Verdict = namedtuple("Verdict", "ok reason uptime_s server_pid checked_at")


# --- probes ---------------------------------------------------------------------------------
def fetch_json(url, timeout_s=3.0):
    """GET `url` and return the parsed JSON object, or None. Bypasses system proxies (a proxy can
    swallow loopback traffic). Never raises."""
    try:
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout_s) as resp:
            if int(getattr(resp, "status", getattr(resp, "code", 0)) or 0) != 200:
                return None
            doc = json.loads(resp.read(200000).decode("utf-8", "replace"))
        return doc if isinstance(doc, dict) else None
    except Exception:
        return None


def default_health():
    return fetch_json(LOCAL_HEALTH_URL)


def default_tunnel(local_pid):
    """(ok, why) for the path Copilot uses. No tunnel configured => (True, '')."""
    try:
        from relay.relay_fleet import _mcp_tunnel_url
        base = (_mcp_tunnel_url() or "").rstrip("/")
    except Exception:
        base = (os.environ.get("MCP_TUNNEL_URL") or "").strip().rstrip("/")
    if not base:
        return True, ""
    doc = fetch_json(base + "/health")
    if not doc or str(doc.get("status", "")).lower() != "ok":
        return False, "tunnel not reachable"
    pid = doc.get("server_pid")
    if pid is not None and local_pid is not None and pid != local_pid:
        return False, "tunnel reaches another server (pid %s != %s)" % (pid, local_pid)
    return True, ""


# --- the gate -------------------------------------------------------------------------------
class ReadyGate:
    """May a Copilot conversation be created now? Thread-safe; probes are cached RECHECK_S."""

    def __init__(self, health_fn=default_health, tunnel_fn=default_tunnel, now=time.time,
                 min_uptime_s=None, recheck_s=None):
        self._health, self._tunnel, self._now = health_fn, tunnel_fn, now
        self.min_uptime_s = MIN_UPTIME_S if min_uptime_s is None else float(min_uptime_s)
        self.recheck_s = RECHECK_S if recheck_s is None else float(recheck_s)
        self._lock = threading.Lock()
        self._last = None

    def _probe(self, now) -> Verdict:
        try:
            doc = self._health()
        except Exception:
            doc = None
        if not doc or str(doc.get("status", "")).lower() != "ok":
            return Verdict(False, REASON_PREFIX + " (not reachable)", None, None, now)
        pid = doc.get("server_pid")
        up = doc.get("server_uptime_s")
        try:
            up = None if up is None else float(up)
        except (TypeError, ValueError):
            up = None
        # An older server without the field is not blocked on uptime (cannot be measured).
        if up is not None and up < self.min_uptime_s:
            return Verdict(False, "%s (just started: up %ds < %ds, pid %s)" % (
                REASON_PREFIX, int(up), int(self.min_uptime_s), pid), up, pid, now)
        try:
            t_ok, t_why = self._tunnel(pid)
        except Exception:
            t_ok, t_why = False, "tunnel probe failed"
        if not t_ok:
            return Verdict(False, "%s (%s)" % (REASON_PREFIX, t_why or "tunnel not reachable"),
                           up, pid, now)
        return Verdict(True, "", up, pid, now)

    def check(self, force=False) -> Verdict:
        now = float(self._now())
        with self._lock:
            if (not force and self._last is not None
                    and 0 <= now - self._last.checked_at < self.recheck_s):
                return self._last
            self._last = self._probe(now)
            return self._last

    def reason(self) -> str:
        """'' = proceed; else why the conversation must not be created yet."""
        return self.check().reason


# --- inbound tail ---------------------------------------------------------------------------
class InboundTail:
    """Incremental reader of mcp_inbound.jsonl: each call returns only rows appended since the
    previous call. First call looks back _FIRST_READ_BYTES. Never raises."""

    def __init__(self, path_fn=None):
        self._path_fn = path_fn
        self._pos = None
        self._buf = b""

    def _path(self):
        if self._path_fn:
            return self._path_fn()
        from tools import obs_log
        return os.path.join(obs_log._state_dir(), obs_log.INBOUND_NAME)

    def poll(self):
        rows = []
        try:
            path = self._path()
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                if self._pos is None:
                    self._pos = max(0, size - _FIRST_READ_BYTES)
                    fh.seek(self._pos)
                    if self._pos:
                        fh.readline()          # drop a partial first line
                        self._pos = fh.tell()
                elif size < self._pos:         # rotated / trimmed
                    self._pos, self._buf = 0, b""
                fh.seek(self._pos)
                data = self._buf + fh.read()
                self._pos = fh.tell()
            lines = data.split(b"\n")
            self._buf = lines.pop()            # incomplete last line waits for the next call
            for raw in lines:
                try:
                    rows.append(json.loads(raw.decode("utf-8", "replace")))
                except ValueError:
                    continue
        except Exception:
            return rows
        return rows


# --- the detector ---------------------------------------------------------------------------
class InitDetector:
    """Did the conversation a worker just created get its MCP connector bound? See module doc."""

    def __init__(self, source=None, now=time.time, window_s=None, ua=None):
        self._source = source or InboundTail().poll
        self._now = now
        self.window_s = INIT_WINDOW_S if window_s is None else float(window_s)
        self._ua = INIT_UA if ua is None else ua
        self._lock = threading.Lock()
        self._events = []     # [ts, used]
        self._exp = {}        # name -> {"t": send time, "event": ts or None}

    def expect(self, name, t_send=None):
        with self._lock:
            self._exp[name] = {"t": float(self._now() if t_send is None else t_send),
                               "event": None}

    def forget(self, name):
        with self._lock:
            self._exp.pop(name, None)

    def _pull(self, now):
        try:
            rows = self._source() or []
        except Exception:
            rows = []
        for r in rows:
            try:
                if r.get("method") != "initialize":
                    continue
                if self._ua and self._ua not in str(r.get("ua", "")):
                    continue
                self._events.append([float(r["ts"]), False])
            except Exception:
                continue
        self._events = sorted((e for e in self._events if now - e[0] <= _EVENT_KEEP_S),
                              key=lambda e: e[0])

    def _assign(self):
        # Re-derive "used" from the attached expectations, then match the pending ones in send
        # order, each to the earliest unused initialize at/after its send.
        taken = {e["event"] for e in self._exp.values() if e["event"] is not None}
        for ev in self._events:
            ev[1] = ev[0] in taken
        for name, e in sorted(self._exp.items(), key=lambda kv: kv[1]["t"]):
            if e["event"] is not None:
                continue
            for ev in self._events:
                if not ev[1] and ev[0] >= e["t"] - _SEND_SLACK_S:
                    ev[1], e["event"] = True, ev[0]
                    break

    def status(self, name, now=None) -> str:
        """'attached' | 'waiting' | 'missing' | 'untracked'."""
        now = float(self._now() if now is None else now)
        with self._lock:
            e = self._exp.get(name)
            if e is None:
                return "untracked"
            self._pull(now)
            self._assign()
            if e["event"] is not None:
                return "attached"
            return "missing" if now - e["t"] > self.window_s else "waiting"

    def sent_at(self, name):
        with self._lock:
            e = self._exp.get(name)
            return e["t"] if e else 0.0

    def attached(self, name, now=None) -> bool:
        return self.status(name, now) == "attached"


def decide_tools(detector, gate, name, now=None) -> str:
    """One poll of a worker's connector check -> 'untracked' | 'attached' | 'waiting' |
    'recycle' | 'rearmed'.

    'recycle' only when the window has run out with NO initialize AND the server was healthy for
    the whole window (it answers now and has been up since before the send). If it restarted or is
    down, the missing initialize is not the conversation's fault: the expectation is re-armed from
    now and the creation gate keeps new conversations away from a dead server."""
    now = float(time.time() if now is None else now)
    st = detector.status(name, now)
    if st != "missing":
        return st
    v = gate.check()
    sent = detector.sent_at(name)
    if v.ok and (v.uptime_s is None or v.uptime_s >= (now - sent)):
        return "recycle"
    detector.expect(name, now)
    return "rearmed"


# --- the reply-based trigger ----------------------------------------------------------------
_NEG_EN = r"(?:not\s+(?:available|in|present|listed|among|exposed)|isn'?t\s+(?:available|in|listed)|missing|unavailable|do(?:es)?\s*n[o']t\s+(?:have|see|include|exist))"
_NO_TOOLS_RES = (
    re.compile(r"universalsearchtool.{0,200}asktool.{0,200}capturecontexttool", re.I | re.S),
    re.compile(r"\bno\s+`?call_tool`?", re.I),
    re.compile(r"call_tool[^.\n]{0,80}" + _NEG_EN, re.I),
    re.compile(r"(?:do(?:es)?\s*n[o']t|don'?t|cannot|can'?t)\s+(?:have|see|find|access)[^.\n]{0,40}`?call_tool`?", re.I),
    re.compile(r"call_tool[^\n]{0,40}(?:ありません|ないため|ない[。、]|無い|使えません|使用できません|"
               r"見当たりません|含まれていません|存在しません|利用できません)"),
    re.compile(r"(?:関数|機能|ツール)(?:一覧|リスト)[^\n]{0,30}call_tool[^\n]{0,30}"
               r"(?:ありません|ない|含まれ)"),
)


def reply_says_no_tools(text) -> bool:
    """True when the model itself says it has no call_tool / only the three built-in functions."""
    s = str(text or "")
    if not s:
        return False
    return any(rx.search(s) for rx in _NO_TOOLS_RES)


# --- process singletons ---------------------------------------------------------------------
_GATE = None
_DET = None
_SING_LOCK = threading.Lock()


def gate() -> ReadyGate:
    global _GATE
    with _SING_LOCK:
        if _GATE is None:
            _GATE = ReadyGate()
        return _GATE


def detector() -> InitDetector:
    global _DET
    with _SING_LOCK:
        if _DET is None:
            _DET = InitDetector()
        return _DET

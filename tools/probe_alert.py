"""Visible alert for the MCP-handshake probe (goal C of the gateway monitor).

Incident this closes (measured 2026-11-11): the gateway was unusable for hours and
nobody was alerted. The monitoring in place only did GET /health, so a public relay
with no host (devtunnel not logged in -> POST /mcp initialize returned HTTP 404 after
50-60 s, ~25 times) and a hung main.py (handles grew to 1585 in 69 min; /health
timeouts) both went unnoticed. Goal A adds a probe that every 60 s runs the REAL MCP
handshake against the local server and the public URL; goal C -- THIS module -- turns
that probe's failures into a visible, actionable alert.

What makes an alert actionable here (same lesson as tools/auth_stats.py's rejection
log): it must name the FAILING HOP and the LIKELY CAUSE, not just "something is down".
The three outage shapes measured on 2026-11-11 map to three causes a reader can act on
without grepping logs:
  - local hop fails            -> local server down / main.py hung
  - public hop fails, local ok -> public relay has no host (devtunnel not logged in)
  - any hop returns 401/403    -> auth (API key desynced)
  - any hop slower than 30 s    -> slow (relay reaching a dead host; the 50-60 s 404)

Design (mirrors tools/auth_stats.py so the two read the same way):
  - ProbeAlertTracker is a PURE, thread-safe class: record one probe result per hop,
    count CONSECUTIVE failures per hop, and decide when the alert condition is met. No
    I/O, no imports of fastmcp/playwright/anything request-shaped -- fully unit-testable
    in isolation. It NEVER shells out (the probe's "do not call the devtunnel CLI"
    contract lives in goal B; this module simply has no reason to run any subprocess).
  - The alert condition is 2 consecutive failures of the SAME hop within a short window
    (default 120 s). Two, not four: the supervisor's old 4-checks-with-60s-confirmations
    debounce is exactly what left the gateway down for ~5 min after main.py hung; the
    measured basis for "2" is in ALERT_MIN_CONSECUTIVE below.
  - record_probe_result() is the ONLY function that does I/O: it updates the tracker,
    and when the condition fires it (a) appends one durable, bounded row to
    .fleet/probe_alerts.jsonl and (b) best-effort raises a desktop toast. Both are
    swallowed on failure so a disk hiccup or a missing toast backend can never break
    the probe loop that calls this.
  - NO CREDENTIAL IS EVER WRITTEN. The probe resolves the API key to send the handshake,
    but this module is never given it and must not acquire a way to receive it -- a file
    recording failures is the last place a key should land (see auth_stats.py).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

# The two hops the probe checks. Kept as named constants so the tracker, the sidecar
# and the toast all spell them the same way.
HOP_LOCAL = "local"
HOP_PUBLIC = "public"
HOPS = (HOP_LOCAL, HOP_PUBLIC)

# Failure classifications the probe hands us. A successful probe passes ok=True and the
# kind is ignored; on ok=False the kind is one of these (anything else degrades to
# "error" so an unexpected label can never crash the mapping).
KIND_LOCAL_DOWN = "local_down"   # local hop refused / no response -> server down or hung
KIND_NO_HOST = "no_host"         # public hop 404 / relay reached no host
KIND_AUTH = "auth"               # 401 / 403 -> API key desynced
KIND_SLOW = "slow"               # a hop answered, but slower than 30 s
KIND_ERROR = "error"             # anything else
FAIL_KINDS = (KIND_LOCAL_DOWN, KIND_NO_HOST, KIND_AUTH, KIND_SLOW, KIND_ERROR)

# Human-readable likely cause per kind, in the operator's language (ja) with the hop
# in parentheses. This is the sentence the toast and the durable row carry -- it is the
# whole point of the alert, so it names what to go look at.
_CAUSE_JA = {
    KIND_LOCAL_DOWN: "ローカルサーバが応答なし（main.py ハング / プロセス停止の可能性）",
    KIND_NO_HOST: "公開リレーにホストなし（devtunnel 未ログイン / ホスト不在の可能性）",
    KIND_AUTH: "認証失敗（API キー不一致の可能性）",
    KIND_SLOW: "応答が 30 秒超（リレーが死んだホストに到達している可能性）",
    KIND_ERROR: "ハンドシェイク失敗（原因不明）",
}

# ALERT_MIN_CONSECUTIVE = 2, and the window = 120 s.
#
# MEASURED 2026-11-11. The gateway was down twice for reasons a 60 s probe sees on the
# very first failing beat: (1) the public relay answered /mcp initialize with 404 after
# 50-60 s while devtunnel was not logged in, for over an hour; (2) main.py hung and the
# supervisor needed 4 failed checks with 60 s confirmations (~5 min) before restarting.
# A single failed beat is not enough -- one slow beat or a restart-in-progress is normal
# noise -- but waiting for four is exactly the debounce that cost ~5 min of downtime. Two
# consecutive failed REAL handshakes within ~2 min is the smallest signal that is not a
# single blip: at a 60 s cadence that is two beats, ~60-120 s, and it is the same number
# goal D uses to make the supervisor restart main.py. If the probe cadence changes, the
# window is what bounds "consecutive": two failures more than 120 s apart are treated as
# the start of a new streak, not a continued one, so a long gap in the probe loop cannot
# silently satisfy the condition with two unrelated failures.
ALERT_MIN_CONSECUTIVE = 2
ALERT_WINDOW_SECONDS_DEFAULT = 120.0


class ProbeAlertTracker:
    """Thread-safe per-hop consecutive-failure counter. Pure logic, no I/O.

    record() returns an alert payload dict the moment a hop reaches
    ALERT_MIN_CONSECUTIVE consecutive failures within the window, and None otherwise.
    A success for a hop resets that hop's streak. Two failures of the same hop more than
    `window_s` apart do NOT chain -- the later one starts a fresh streak of length 1 --
    so a stalled probe loop cannot satisfy the condition with two unrelated failures.

    Safe to instantiate independently in tests; shares no module state.
    """

    def __init__(self, min_consecutive: int = ALERT_MIN_CONSECUTIVE,
                 window_s: float = ALERT_WINDOW_SECONDS_DEFAULT):
        self.min_consecutive = int(min_consecutive)
        self.window_s = float(window_s)
        self._lock = threading.Lock()
        # per hop: current consecutive-failure streak count and the ts of the last fail
        self._streak: dict[str, int] = {}
        self._last_fail_ts: dict[str, float] = {}
        self._last_kind: dict[str, str] = {}
        # has an alert already fired for the CURRENT streak? so we alert once per streak,
        # not on every beat after the threshold (which would be a toast every 60 s).
        self._alerted: dict[str, bool] = {}

    def record(self, hop: str, ok: bool, kind: str = KIND_ERROR,
               ts: Optional[float] = None) -> Optional[dict]:
        """Record one probe result for one hop. Return an alert payload the instant the
        hop crosses the consecutive-failure threshold for a fresh streak, else None.
        Never raises.
        """
        now = time.time() if ts is None else float(ts)
        hop = str(hop)
        with self._lock:
            if ok:
                # success clears this hop's streak and re-arms the one-shot alert
                self._streak[hop] = 0
                self._alerted[hop] = False
                return None
            k = kind if kind in FAIL_KINDS else KIND_ERROR
            prev_ts = self._last_fail_ts.get(hop)
            if prev_ts is not None and (now - prev_ts) <= self.window_s:
                self._streak[hop] = self._streak.get(hop, 0) + 1
            else:
                # first failure, or too long since the last one: start a fresh streak
                self._streak[hop] = 1
                self._alerted[hop] = False
            self._last_fail_ts[hop] = now
            self._last_kind[hop] = k
            if (self._streak[hop] >= self.min_consecutive
                    and not self._alerted.get(hop, False)):
                self._alerted[hop] = True
                return {
                    "hop": hop,
                    "kind": k,
                    "consecutive": self._streak[hop],
                    "ts": round(now, 3),
                    "cause_ja": _CAUSE_JA.get(k, _CAUSE_JA[KIND_ERROR]),
                }
            return None

    def streak(self, hop: str) -> int:
        """Current consecutive-failure count for a hop (0 if last result was ok)."""
        with self._lock:
            return self._streak.get(hop, 0)


# Module-level singleton the probe loop touches.
_TRACKER = ProbeAlertTracker()

# Durable, bounded record of alerts that actually fired, so an alert that scrolled off
# the toast can still be looked into afterwards (same reasoning as auth_stats.py's
# rejection log: the window is the alarm, the file is the evidence). Redirectable so a
# test never writes into the operator's live .fleet record.
_ALERTS_FILE = Path(os.environ.get("MCP_PROBE_ALERTS_FILE")
                    or (Path(__file__).resolve().parent.parent / ".fleet"
                        / "probe_alerts.jsonl"))

# Bounded: written from a loop on a machine whose disk has been filled to zero once by a
# log nobody bounded (see auth_stats.py). Keep the RECENT half on overflow.
_ALERTS_MAX_BYTES = 256 * 1024


def _append_alert(payload: dict) -> None:
    """Append one fired alert to the durable record. Never raises. Writes nothing that
    was offered as a credential -- the payload carries only hop, kind, cause and ts."""
    try:
        _ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            if _ALERTS_FILE.stat().st_size > _ALERTS_MAX_BYTES:
                keep = _ALERTS_FILE.read_text(encoding="utf-8", errors="replace")
                keep = keep[len(keep) // 2:]
                keep = keep[keep.find("\n") + 1:]   # never start mid-line
                _ALERTS_FILE.write_text(keep, encoding="utf-8")
        except OSError:
            pass
        row = {
            "ts": round(float(payload.get("ts") or 0), 3),
            "hop": str(payload.get("hop") or "")[:32],
            "kind": str(payload.get("kind") or "")[:32],
            "consecutive": int(payload.get("consecutive") or 0),
            "cause_ja": str(payload.get("cause_ja") or "")[:200],
        }
        with open(str(_ALERTS_FILE), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent_alerts(limit: int = 20) -> list:
    """The last `limit` fired alerts, newest last. [] when there is no record."""
    try:
        rows = []
        with open(str(_ALERTS_FILE), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return rows[-int(limit):] if limit else rows
    except OSError:
        return []


def alert_text(payload: dict) -> tuple:
    """(title, body) for the toast. Pure, so a test can assert the hop and cause are
    both named without a toast backend. The title names the hop; the body names the
    likely cause -- an alert you can act on."""
    hop = str(payload.get("hop") or "?")
    cause = str(payload.get("cause_ja") or _CAUSE_JA[KIND_ERROR])
    n = int(payload.get("consecutive") or 0)
    title = "MCP \u30b2\u30fc\u30c8\u30a6\u30a7\u30a4\u969c\u5bb3: %s hop" % hop
    body = "%s\uff08%d \u56de\u9023\u7d9a\u5931\u6557\uff09" % (cause, n)
    return title, body


def _raise_toast(payload: dict) -> None:
    """Best-effort desktop toast. Swallows everything: a missing toast backend, or
    running headless in CI, must never break the probe loop. Imported lazily so this
    module stays stdlib-only at import time (the "cheap to import anywhere" contract).

    notify_desktop is already inert under pytest / MCP_SUPPRESS_GUI by construction
    (see tools/notify_ops.py), so a test that reaches this path raises no real toast.
    The toast's `launch` points at the durable sidecar so a click opens the evidence --
    an alert a click can act on, not just an alarm."""
    try:
        title, body = alert_text(payload)
        from tools.notify_ops import notify_desktop  # lazy; the real toast chokepoint
        launch = ""
        try:
            launch = _ALERTS_FILE.resolve().as_uri()
        except Exception:
            launch = ""
        notify_desktop(title, body, launch=launch)
    except Exception:
        pass


def record_probe_result(hop: str, ok: bool, kind: str = KIND_ERROR,
                        ts: Optional[float] = None) -> Optional[dict]:
    """Record one probe result against the module singleton. When it causes a hop to
    reach the consecutive-failure threshold, durably record the alert and raise a
    desktop toast, then return the alert payload; otherwise return None. Never raises --
    called from the probe loop.
    """
    try:
        payload = _TRACKER.record(hop=hop, ok=ok, kind=kind, ts=ts)
    except Exception:
        return None
    if payload:
        _append_alert(payload)
        _raise_toast(payload)
    return payload

"""Tool-call self-probe tracker for the bridge (self-reporting for the health layer).

Incident this closes (already diagnosed -- see the bridge/copilot_bridge.py docstring
this module is wired into): the user's interactive "desktopfile操作" chat runs through
the BRIDGE (bridge/copilot_bridge.py, Edge profile copilot-bridge-edge on CDP :9223,
HTTP :8765). FleetCockpit's health strip only probes :9222 (the FLEET Edge) and
:8000/tunnel -- none of its dots ever verify that the BRIDGE-side agent can actually
CALL an MCP tool end-to-end. So when the bridge-side MCP connector consent lapses or
its CDP session goes stale, tool calls silently die while every cockpit dot stays
green, and tools/auth_stats.py's 401 counter stays 0 because the request never even
reaches our server (the failure is entirely inside the Copilot web UI / connector,
upstream of our HTTP surface). This module is the smallest piece of state needed to
make "the bridge's agent can currently call a tool" self-report, the same way
tools/auth_stats.py made a burst of 401s self-report.

Design (mirrors tools/auth_stats.py):
  - This module is pure and import-safe: stdlib only, no playwright/pyodbc/pandas/
    fastmcp at module top, so it can be imported cheaply from anywhere (including a
    hermetic test) without paying for the bridge's heavy Playwright import chain.
  - record_probe()/get_summary() are the ONLY functions that do I/O (atomic tmp+replace
    into .fleet/tool_probe.json, same pattern as auth_stats.write_snapshot), and both are
    best-effort: any failure is swallowed so a disk hiccup or a corrupt sidecar file can
    never break the bridge's request handling or a /health read.
  - classify_probe_reply() is a separate PURE function (no I/O) so the classification
    logic itself is trivially unit-testable with canned strings, independent of the
    Playwright-driving code in bridge/copilot_bridge.py that produces the reply text.

Marker lists (CONSENT_MARKERS / NO_CONNECTOR_MARKERS) are a MINIMAL LOCAL COPY of the
lists defined in relay/edge_reconnect.py (see that module's CONSENT_MARKERS /
NO_CONNECTOR_MARKERS, ~line 45). They are duplicated here -- not imported -- because
`import relay.edge_reconnect` pulls in relay.copilot_autopilot_relay's chain (playwright,
fastmcp/authlib, etc.), measured at ~23s wall-clock for the first import; that is
incompatible with this module's "stdlib-only, cheap to import anywhere" contract. If
edge_reconnect's lists change, update the copy below to match.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

# ---------------------------------------------------------------------------------------
# Local copy of relay/edge_reconnect.py's marker lists (see module docstring above for why
# this is a copy, not an import). Keep in sync with relay/edge_reconnect.py by hand.
# ---------------------------------------------------------------------------------------
CONSENT_MARKERS = ("接続マネージャーを開く", "connection manager")
NO_CONNECTOR_MARKERS = ("実行不可", "コネクタ無し", "コネクタがありません", "ツールが使用できません")

# Distinctive marker token the probe instruction asks the agent to emit ONLY when the
# list_directory tool call actually succeeded -- see bridge/copilot_bridge.py's
# TOOL_PROBE_INSTRUCTION for the full prompt text that requests this token.
PROBE_OK_TOKEN = "===TOOLPROBE_OK==="

# The full set of `kind` values record_probe()/classify_probe_reply() may produce.
PROBE_KINDS = ("answer", "consent_card", "canned_fallback", "timeout",
               "agent_unreachable", "error", "starting", "checking")

# Where the cockpit / /health can read the same summary without driving the browser.
_PROBE_FILE = Path(__file__).resolve().parent.parent / ".fleet" / "tool_probe.json"

_LOCK = threading.Lock()


def classify_probe_reply(reply_text: str, agent_loaded: bool) -> Tuple[bool, str]:
    """Pure classifier: given the probe turn's reply text and whether the agent page was
    even loaded, decide (ok, kind). No I/O, no exceptions raised for any input shape --
    reply_text may be None/empty/garbage and this still returns a valid (ok, kind) pair.

    Branch order (first match wins):
      1. not agent_loaded          -> (False, "agent_unreachable")
      2. reply has a CONSENT marker -> (False, "consent_card")
      3. reply has a NO-CONNECTOR/canned marker -> (False, "canned_fallback")
      4. reply contains PROBE_OK_TOKEN -> (True, "answer")
      5. otherwise                 -> (False, "error")

    NOTE: "timeout" is NOT decided here -- it is a bridge-level outcome (DRIVER.wait_for_idle
    returning False, i.e. no exception and no text to classify) and is recorded directly by
    the caller before this function would even be invoked. See bridge/copilot_bridge.py's
    _run_tool_probe."""
    text = reply_text or ""
    if not agent_loaded:
        return False, "agent_unreachable"
    if any(m in text for m in CONSENT_MARKERS):
        return False, "consent_card"
    if any(m in text for m in NO_CONNECTOR_MARKERS):
        return False, "canned_fallback"
    if PROBE_OK_TOKEN in text:
        return True, "answer"
    return False, "error"


def record_probe(ok: bool, kind: str, detail: str = "", ts: Optional[float] = None) -> None:
    """Record the outcome of one tool-call self-probe and best-effort persist it to
    .fleet/tool_probe.json (atomic tmp+os.replace, utf-8, ensure_ascii=False -- same pattern
    as tools/auth_stats.write_snapshot). Never raises.

    `ts` defaults to time.time() but accepts a caller-supplied value so callers (and tests)
    can be deterministic instead of depending on wallclock at call time."""
    try:
        now = time.time() if ts is None else ts
        payload = {"ts": now, "ok": bool(ok), "kind": kind, "detail": detail or ""}
        with _LOCK:
            _PROBE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(_PROBE_FILE) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, str(_PROBE_FILE))
    except Exception:
        pass


def get_summary(now: Optional[float] = None) -> dict:
    """Read the last-recorded probe outcome from .fleet/tool_probe.json and return
    {"tool_ok": bool|None, "tool_kind": str|None, "tool_ts": float|None,
    "tool_age_s": float|None} -- the shape /health's payload.update(...) mirrors from
    tools.auth_stats.get_summary().

    Tolerates a missing or corrupt file (never raises): returns the all-None shape in
    either case, exactly as tools.auth_stats.get_summary() zeroes out on failure. `now`
    is a caller-supplied reference time for computing tool_age_s, defaulting to
    time.time() -- deterministic for tests, real wallclock in production (e.g. /health)."""
    empty = {"tool_ok": None, "tool_kind": None, "tool_ts": None, "tool_age_s": None}
    try:
        with open(_PROBE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        ts = raw.get("ts")
        if not isinstance(ts, (int, float)):
            return dict(empty)
        ref = time.time() if now is None else now
        return {
            "tool_ok": raw.get("ok"),
            "tool_kind": raw.get("kind"),
            "tool_ts": ts,
            "tool_age_s": max(0.0, ref - ts),
        }
    except Exception:
        return dict(empty)

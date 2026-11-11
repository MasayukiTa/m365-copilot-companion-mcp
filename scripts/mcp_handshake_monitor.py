# -*- coding: utf-8 -*-
r"""Synthetic MCP-handshake monitor: the pure decision core.

WHY THIS EXISTS (measured 2026-11-11)
-------------------------------------
The supervisor's liveness check (Test-ServerUp in scripts/supervisor.ps1) does a plain
``GET /health`` against 127.0.0.1:8000 and counts a fast 200 as alive. The owner's chat
client 'muse' reported the real MCP ``initialize`` handshake failing with HTTP 404 after
~60 s, with no tool call ever arriving, WHILE our monitoring only ever did GET /health and
saw 200 everywhere. So the monitoring watched something different from what the client
does: /health can be 200 on the loopback while the PUBLIC MCP route (devtunnel relay ->
this host) answers 404 because the relay has no host connection.

Measured fact: the supervisor log repeatedly showed ``tunnel host connections = 0`` while
the devtunnel host process was alive. Hypothesis this monitor exists to test: while the
relay has no host connection it answers 404 after waiting ~60 s.

WHAT THIS MODULE IS
-------------------
The PURE decision layer, in the same shape as scripts/supervisor_zero_conn_recovery.py and
doctor.ps1's Get-SupervisorServerVerdict: no I/O, no clock, no process calls, stdlib only,
so it runs on the Linux CI runner against stub inputs. The resident runner that actually
sends the handshake and measures latency lives in tools/mcp_handshake_probe.py; it hands
the measured facts (status, latency, which hop, host-connection count) to the functions
here, which decide the words: per-target outcome, which hop failed, whether to alert, and
whether a 404 coincides with host connections = 0.

Pure. A missing or malformed input reads as "cannot tell" and never, by itself, becomes the
reason an alert fires or a 404 is blamed on the host count.
"""
from __future__ import annotations

#: A handshake slower than this many seconds is a failure even if it would eventually answer
#: 200. The muse client gave up around 60 s; 30 s is well under that and above the measured
#: healthy handshake (1.2 s initialize at 18:32 on 2026-11-11), so a genuinely working route
#: is never called slow while the ~60 s 404 wait is always caught.
SLOW_SECONDS = 30.0

#: Consecutive failures (per target) before the monitor raises a visible alert. Two, so a
#: single transient relay hiccup between two 60 s ticks does not page, but a route that is
#: actually down for two ticks (~2 min) does. Matches the supervisor's own "only after it
#: persists" debounce philosophy without copying its larger tunnel-kill debounce (4), because
#: this monitor only RAISES AN ALERT -- it never kills anything -- so a faster strike is safe.
ALERT_AFTER_CONSECUTIVE_FAILURES = 2

# -- hop names (which leg of local-client -> relay -> host -> server failed) ----------------
HOP_NONE = "none"              # the handshake succeeded
HOP_LOCAL_SERVER = "local_server"      # 127.0.0.1:8000/mcp itself did not complete the handshake
HOP_DEVTUNNEL_RELAY = "devtunnel_relay"  # the public relay answered wrong (e.g. 404) or did not answer
HOP_HOST_ZERO_CONN = "host_connections_zero"  # public failed AND devtunnel host connections == 0
HOP_AUTH = "auth"             # 401/403: the API key was rejected
HOP_UNKNOWN = "unknown"       # a failure whose hop cannot be told from the inputs


def _as_float(value):
    """Return value as float, or None when it cannot be read as a number."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value):
    """Return value as int, or None when it cannot be read as an integer."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_handshake(status, latency_seconds, slow_seconds=SLOW_SECONDS):
    """Decide whether one handshake attempt PASSED or FAILED, and why.

    Inputs (all measured by the caller in tools/mcp_handshake_probe.py):
      * ``status`` -- the HTTP status of the ``initialize`` POST: an int (200, 404, 401...),
        or the string ``"ERR"`` / ``None`` when the request never got an HTTP answer
        (timeout, connection refused).
      * ``latency_seconds`` -- how long the ``initialize`` POST took, in seconds.
      * ``slow_seconds`` -- the slowness ceiling (default SLOW_SECONDS).

    Returns a dict: ``{"ok": bool, "reason": str}``.

    Rules (a 404 or a response slower than the ceiling is a failure, not only non-200):
      * no HTTP answer (ERR/None)            -> fail, reason "no_answer"
      * 401 or 403                           -> fail, reason "auth"
      * 404                                  -> fail, reason "not_found_404"
      * any other non-200                    -> fail, reason "http_<status>"
      * 200 but latency > ceiling            -> fail, reason "slow"
      * 200 within the ceiling               -> pass, reason "ok"
      * 200 with an unreadable latency        -> pass, reason "ok_latency_unknown"
        (a 200 is a 200; an unreadable duration must not manufacture a failure)
    """
    code = _as_int(status)
    if code is None:
        return {"ok": False, "reason": "no_answer"}
    if code in (401, 403):
        return {"ok": False, "reason": "auth"}
    if code == 404:
        return {"ok": False, "reason": "not_found_404"}
    if code != 200:
        return {"ok": False, "reason": "http_%d" % code}
    secs = _as_float(latency_seconds)
    if secs is None:
        return {"ok": True, "reason": "ok_latency_unknown"}
    if secs > _coerce_ceiling(slow_seconds):
        return {"ok": False, "reason": "slow"}
    return {"ok": True, "reason": "ok"}


def _coerce_ceiling(slow_seconds):
    s = _as_float(slow_seconds)
    if s is None or s <= 0:
        return SLOW_SECONDS
    return s


def classify_hop(scope, result, host_connections=None):
    """Name which hop failed for one target's handshake result.

    Inputs:
      * ``scope`` -- ``"local"`` (127.0.0.1:8000/mcp) or ``"public"`` (MCP_TUNNEL_URL + /mcp).
      * ``result`` -- the dict returned by ``classify_handshake`` for that target.
      * ``host_connections`` -- the devtunnel ``Host connections`` count at the time of the
        PUBLIC probe (int), or ``None`` when ``devtunnel show`` gave no readable count.
        Only consulted for the public scope.

    Returns one of the HOP_* words.

    The hop logic encodes the hypothesis under test: a PUBLIC 404 (or no answer) while the
    relay reports zero host connections is the "relay has no host connection" failure, named
    HOP_HOST_ZERO_CONN so the alert can say the 404 coincided with host connections = 0. A
    public failure with host connections >= 1 (or an unreadable count) is blamed on the relay
    leg itself, not on a zero host count we could not confirm.
    """
    if result is None or result.get("ok"):
        return HOP_NONE
    reason = result.get("reason", "")
    if reason == "auth":
        return HOP_AUTH
    if scope == "local":
        return HOP_LOCAL_SERVER
    if scope == "public":
        conn = _as_int(host_connections)
        if reason in ("not_found_404", "no_answer", "slow") and conn is not None and conn == 0:
            return HOP_HOST_ZERO_CONN
        return HOP_DEVTUNNEL_RELAY
    return HOP_UNKNOWN


def update_consecutive_failures(previous_count, result):
    """Advance a per-target consecutive-failure counter from the latest result.

    A pass resets the counter to 0; a fail increments it. ``previous_count`` that cannot be
    read as an integer is treated as 0 (a fresh start), so a corrupt saved counter can never
    inflate the streak into a false alert.
    """
    prev = _as_int(previous_count) or 0
    if prev < 0:
        prev = 0
    if result is not None and result.get("ok"):
        return 0
    return prev + 1


def decide_alert(consecutive_failures, threshold=ALERT_AFTER_CONSECUTIVE_FAILURES):
    """Whether a visible alert should be raised, given the consecutive-failure count.

    Returns True only when the count is a readable integer >= threshold. An unreadable count
    never raises an alert (it reads as "cannot tell", which must not page on its own).
    """
    n = _as_int(consecutive_failures)
    if n is None:
        return False
    t = _as_int(threshold)
    if t is None or t < 1:
        t = ALERT_AFTER_CONSECUTIVE_FAILURES
    return n >= t


def correlates_404_with_zero_host_connections(public_result, host_connections):
    """True when the public handshake 404'd AND devtunnel host connections == 0.

    This is the exact sentence the alert needs to state (task item 3): did a 404 coincide
    with host connections = 0. Returns False when the public result is not a 404, or when the
    host-connection count is unreadable (None) or non-zero -- a correlation we cannot confirm
    is not asserted.
    """
    if public_result is None:
        return False
    if public_result.get("reason") != "not_found_404":
        return False
    conn = _as_int(host_connections)
    return conn is not None and conn == 0


def gateway_is_down(local_result, public_result):
    """The verdict the supervisor should act on INSTEAD of a bare /health 200 (task item 4).

    The gateway is the public MCP route the client actually uses. It is DOWN when the public
    handshake failed, regardless of whether the loopback /health is green -- that split is the
    whole reason this monitor exists. The local result is returned alongside so the caller can
    tell "only the public leg is down" (relay/host problem) from "the server itself is down"
    (both legs failed).

    Returns a dict:
      ``{"gateway_down": bool, "local_ok": bool, "public_ok": bool, "scope": str}``
    where ``scope`` is "both", "public_only", "local_only", or "none".
    """
    local_ok = bool(local_result is not None and local_result.get("ok"))
    public_ok = bool(public_result is not None and public_result.get("ok"))
    if not local_ok and not public_ok:
        scope = "both"
    elif not public_ok:
        scope = "public_only"
    elif not local_ok:
        scope = "local_only"
    else:
        scope = "none"
    # The gateway is what the external client reaches: the public route. It is down whenever
    # the public handshake failed (both-down and public-only both mean the client cannot MCP).
    return {
        "gateway_down": not public_ok,
        "local_ok": local_ok,
        "public_ok": public_ok,
        "scope": scope,
    }


#: The supervisor polls every IntervalSeconds (15 s) and the monitor ticks every 60 s, so a
#: fresh status file is at most ~60 s old plus one handshake. A status older than this is
#: STALE -- the monitor is not running or is wedged -- and must NOT be read as "gateway down"
#: (an absent monitor is not evidence the gateway failed). Comfortably above one 60 s tick
#: plus the 75 s request timeout, below the ~7 minute outage gaps this family of fixes closes.
MONITOR_STALE_SECONDS = 180.0


def decide_gateway_from_monitor(status, status_age_seconds,
                                threshold=ALERT_AFTER_CONSECUTIVE_FAILURES,
                                stale_seconds=MONITOR_STALE_SECONDS):
    """The supervisor's gateway verdict, read from THIS monitor's status -- not from /health.

    This is the function the supervisor calls so that it decides the gateway is down from the
    REAL handshake the client does, not from a loopback ``GET /health`` 200 (task item 4). It
    is pure: the PowerShell side reads the status file and its age and hands them here.

    Inputs:
      * ``status`` -- the parsed ``mcp_synthetic_monitor_status.json`` dict (one tick), or
        ``None`` when the file is missing or unparseable.
      * ``status_age_seconds`` -- how old that file is, in seconds (``None`` = cannot tell).
      * ``threshold`` -- consecutive public failures required before "down" (default 2, the
        same 2-strike debounce the alert uses, so the supervisor never acts on a single tick).
      * ``stale_seconds`` -- age beyond which the status is too old to trust.

    Returns a dict ``{"verdict": str, "reason": str}`` where ``verdict`` is one of:
      * ``"down"``    -- the public handshake has failed for >= threshold consecutive ticks in
        a fresh status. THIS is what the supervisor acts on.
      * ``"up"``      -- a fresh status whose public leg is passing.
      * ``"stale"``   -- the status is older than ``stale_seconds`` (monitor not running);
        the supervisor must fall back to its own checks, NOT declare the gateway down.
      * ``"unknown"`` -- no status, unparseable, no public target configured, or the age
        cannot be told. Also a fall-back-to-own-checks verdict, never "down".

    The debounce lives here (not only in the writer) so a single transient failing tick that
    happens to be the latest file never makes the supervisor act.
    """
    if not isinstance(status, dict):
        return {"verdict": "unknown", "reason": "no_status"}
    age = _as_float(status_age_seconds)
    if age is None:
        return {"verdict": "unknown", "reason": "age_unknown"}
    s = _as_float(stale_seconds)
    if s is None or s <= 0:
        s = MONITOR_STALE_SECONDS
    if age > s:
        return {"verdict": "stale", "reason": "status_%.0fs_old" % age}
    public = status.get("public")
    if not isinstance(public, dict) or not public.get("url_configured"):
        return {"verdict": "unknown", "reason": "no_public_target"}
    result = public.get("result")
    if not isinstance(result, dict):
        return {"verdict": "unknown", "reason": "no_public_result"}
    if result.get("ok"):
        return {"verdict": "up", "reason": "public_ok"}
    streak = _as_int(public.get("consecutive_failures"))
    t = _as_int(threshold)
    if t is None or t < 1:
        t = ALERT_AFTER_CONSECUTIVE_FAILURES
    if streak is not None and streak >= t:
        return {"verdict": "down", "reason": "public_failed_%d_consecutive" % streak}
    # A fresh failing tick that has not yet reached the debounce: not yet "down".
    return {"verdict": "up", "reason": "public_failing_below_threshold"}

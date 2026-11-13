"""Provider outage detection and standby for the fleet coordinator.

When the model provider stops answering, turns still get sent and counted as progress,
goals wait for hours, and the per-goal turn budget is spent for nothing. This module:

* counts turns sent and replies received (a reply = a non-empty assistant message),
* classifies the provider as ok / degraded / outage (provider_state, pure),
* keeps workers from sending turns while in outage (standby),
* probes reachability cheaply and allows exactly one turn once the probes recover,
* records every state change as one line in .fleet/provider_health.jsonl.

All thresholds live in the constants below. Each has its reason next to it.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time

# ---------------------------------------------------------------------------
# Thresholds. One place. Each reason names the failure it prevents.
# ---------------------------------------------------------------------------

#: Outage after this many consecutive sent turns with no reply. Reason: during a provider
#: outage, turns kept going out and got no answer; six in a row across the fleet is far
#: beyond what a single slow turn produces, so it marks a real stop, not a slow model.
OUTAGE_CONSECUTIVE_NO_REPLY = 6

#: Outage when token wait exceeds this many seconds while goals are queued. Reason: a
#: token wait of ten minutes with goals waiting means no turn can start at all, so the
#: queue is stalled even when no turn has been sent recently to count against it.
OUTAGE_TOKEN_WAIT_S = 600.0

#: Window size for the degraded check, in sent turns. Reason: ten turns is a sample wide
#: enough that one or two unlucky turns cannot flip the state.
DEGRADED_WINDOW_TURNS = 10

#: Degraded when replies / sent in the window is below this. Reason: at 50 percent, half
#: the turns are lost; the fleet is spending budget on turns that return nothing.
DEGRADED_MIN_REPLY_RATIO = 0.5

#: Seconds between reachability probes while in outage. Reason: cheap enough to run for
#: hours, frequent enough that recovery is noticed within a minute.
PROBE_INTERVAL_S = 60.0

#: Timeout of one TCP connect probe. Reason: a provider that does not accept a TCP
#: connection within five seconds is not usable for a socket turn either.
PROBE_TIMEOUT_S = 5.0

#: Consecutive successful probes (both hosts) before one turn is allowed. Reason: a single
#: success can be a blip; two in a row shows the path is stable enough to spend one turn.
PROBE_PASSES_REQUIRED = 2

#: Host of the provider socket. Same host as relay/chathub.py WS_BASE. A test checks that.
SOCKET_HOST = "substrate.office.com"
SOCKET_PORT = 443

#: Host of the token capture audience (relay/profile_token.py AUDIENCE). Also checked.
TOKEN_HOST = "substrate.office.com"
TOKEN_PORT = 443

#: How many sent-turn outcomes are kept in memory. Reason: only the last window is used;
#: a bound keeps status.json from growing with the run length.
HISTORY_KEEP = 200

HEALTH_LOG_NAME = "provider_health.jsonl"


def _new_counters():
    return {"turns_sent": 0, "replies_received": 0, "last_reply_ts": 0.0,
            "recent": [], "base": 0, "queued": 0}


def provider_state(counters, token_wait_s, socket_fallbacks, now):
    """Return (state, reason) where state is 'ok', 'degraded' or 'outage'.

    counters: dict with 'recent' (list of bools, oldest first, True = replied), 'queued'
    (goals waiting for a worker). socket_fallbacks is reported in the reason only.
    Outage is checked before degraded, so the stronger statement wins.
    """
    recent = list((counters or {}).get("recent") or [])
    queued = int((counters or {}).get("queued") or 0)
    fallbacks = int(socket_fallbacks or 0)

    trailing = 0
    for replied in reversed(recent):
        if replied:
            break
        trailing += 1
    if trailing >= OUTAGE_CONSECUTIVE_NO_REPLY:
        return "outage", ("%d consecutive sent turns with no reply (limit %d); socket fallbacks %d"
                          % (trailing, OUTAGE_CONSECUTIVE_NO_REPLY, fallbacks))

    if token_wait_s is not None and float(token_wait_s) > OUTAGE_TOKEN_WAIT_S and queued > 0:
        return "outage", ("token wait %.0fs (limit %.0fs) with %d goals queued at %.0f"
                          % (float(token_wait_s), OUTAGE_TOKEN_WAIT_S, queued, float(now)))

    if len(recent) >= DEGRADED_WINDOW_TURNS:
        window = recent[-DEGRADED_WINDOW_TURNS:]
        ratio = sum(1 for r in window if r) / float(len(window))
        if ratio < DEGRADED_MIN_REPLY_RATIO:
            return "degraded", ("replies in the last %d sent turns: %.0f%% (minimum %.0f%%)"
                                % (DEGRADED_WINDOW_TURNS, ratio * 100.0,
                                   DEGRADED_MIN_REPLY_RATIO * 100.0))

    return "ok", "replies are arriving"


def tcp_reachable(host, port, timeout=PROBE_TIMEOUT_S):
    """One TCP connect. True when the connection opens within the timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_reachable():
    """Both provider hosts must accept a connection."""
    return tcp_reachable(SOCKET_HOST, SOCKET_PORT) and tcp_reachable(TOKEN_HOST, TOKEN_PORT)


class ProviderMonitor:
    """Holds the counters and the state. Thread-safe; all times come from the caller."""

    def __init__(self, state_dir=None, probe=probe_reachable, clock=time.time):
        self._lock = threading.Lock()
        self._state_dir = state_dir
        self._probe = probe
        self._clock = clock
        self.counters = _new_counters()
        self.state = "ok"
        self.reason = "no turns yet"
        self.since = float(clock())
        self._last_probe = 0.0
        self._passes = 0
        self._probe_turn_allowed = False
        self._probe_turn_out = False

    # -- accounting -------------------------------------------------------
    def note_sent(self):
        """A turn went out. Returns its absolute index for note_reply."""
        with self._lock:
            c = self.counters
            idx = c["turns_sent"]
            c["turns_sent"] += 1
            c["recent"].append(False)
            if len(c["recent"]) > HISTORY_KEEP:
                drop = len(c["recent"]) - HISTORY_KEEP
                del c["recent"][:drop]
                c["base"] += drop
            return idx

    def note_reply(self, idx, ts=None):
        """A non-empty assistant reply arrived for the turn sent with index idx."""
        with self._lock:
            c = self.counters
            pos = idx - c["base"]
            if 0 <= pos < len(c["recent"]) and not c["recent"][pos]:
                c["recent"][pos] = True
                c["replies_received"] += 1
            c["last_reply_ts"] = float(ts if ts is not None else self._clock())
            if self._probe_turn_out:
                # Recovery needs a real reply. A probe success alone never restores ok.
                self._probe_turn_out = False
                self._evaluate_locked(None, None)

    def set_queued(self, n):
        with self._lock:
            self.counters["queued"] = int(n)

    # -- standby ----------------------------------------------------------
    def send_blocked(self):
        """True when a worker must NOT send a turn now (outage standby).

        In outage, exactly one turn is allowed after PROBE_PASSES_REQUIRED passing probes.
        Calling this consumes that allowance.
        """
        with self._lock:
            if self.state != "outage":
                return False
            if self._probe_turn_allowed:
                self._probe_turn_allowed = False
                self._probe_turn_out = True
                return False
            return True

    def tick(self, token_wait_s=None, socket_fallbacks=0, now=None):
        """Re-evaluate the state and run the probe when due. Call about once a second."""
        now = float(self._clock() if now is None else now)
        if self.state == "outage" and now - self._last_probe >= PROBE_INTERVAL_S:
            self._last_probe = now
            ok = False
            try:
                ok = bool(self._probe())
            except Exception:
                ok = False
            with self._lock:
                self._passes = self._passes + 1 if ok else 0
                if self._passes >= PROBE_PASSES_REQUIRED and not self._probe_turn_out:
                    self._probe_turn_allowed = True
        with self._lock:
            self._evaluate_locked(token_wait_s, socket_fallbacks, now)

    def _evaluate_locked(self, token_wait_s, socket_fallbacks, now=None):
        now = float(self._clock() if now is None else now)
        counters = dict(self.counters)
        state, reason = provider_state(counters, token_wait_s, socket_fallbacks or 0, now)
        if state != self.state:
            self._transition_locked(state, reason, now)
        else:
            self.reason = reason

    def _transition_locked(self, state, reason, now):
        frm = self.state
        self.state = state
        self.reason = reason
        self.since = now
        self._passes = 0
        self._probe_turn_allowed = False
        if state != "outage":
            self._probe_turn_out = False
        self._write_change(frm, state, reason, now)

    def _write_change(self, frm, to, reason, now):
        if not self._state_dir:
            return
        row = {"ts": now, "from": frm, "to": to, "reason": reason,
               "counters": {k: self.counters[k] for k in
                            ("turns_sent", "replies_received", "last_reply_ts", "queued")}}
        try:
            os.makedirs(self._state_dir, exist_ok=True)
            with open(os.path.join(self._state_dir, HEALTH_LOG_NAME), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def status_block(self):
        """Fields for the top level of status.json."""
        with self._lock:
            return {"provider_state": self.state, "provider_reason": self.reason,
                    "provider_state_since": self.since,
                    "provider_turns_sent": self.counters["turns_sent"],
                    "provider_replies_received": self.counters["replies_received"]}


_MONITOR = None
_MONITOR_LOCK = threading.Lock()


def get_monitor(state_dir=None):
    """Process-wide monitor. The first caller that passes state_dir sets where it logs."""
    global _MONITOR
    with _MONITOR_LOCK:
        if _MONITOR is None:
            _MONITOR = ProviderMonitor(state_dir=state_dir)
        elif state_dir and not _MONITOR._state_dir:
            _MONITOR._state_dir = state_dir
        return _MONITOR

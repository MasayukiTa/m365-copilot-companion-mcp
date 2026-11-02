# -*- coding: utf-8 -*-
"""The supervisor must never run two Dev Tunnel host readers at once.

WHY THIS EXISTS (measured 2026-11-02)
-------------------------------------
At 07:34 this PC's outbound network dropped for under a minute. The reader started
at 21:23 (``python -m relay.tunnel_host_log``, a supervisor child) lost its relay
connection and was retrying with its OWN backoff (relay.tunnel_host_log.backoff_seconds,
and the devtunnel host's internal reconnect). The supervisor's rule
"tunnel host connections = 0 (4/4)" then fired at 07:34:52 and launched a SECOND reader
(``Start-TunnelHost`` in scripts/supervisor.ps1) WITHOUT stopping the first. Two devtunnel
hosts for the same tunnel took each other's relay session ("Connection lost" in a loop) and
the gateway answered 502/timeouts from ~07:34 to ~07:41, until the older reader was stopped
by hand.

The real fix is to NOT launch a second reader while the first is still alive and plausibly
reconnecting. This module is the PURE decision core, mirroring the shape the other supervisor
gates already use (scripts/stale_server_check.decide_stale_drain, relay/fleet_resume.resume_gate):
no I/O, no clock, no process calls -- the caller measures "is a reader alive" and "for how long
has it been without a relay connection" and acts on the one word this returns. The PowerShell
side (Start-TunnelHost) does the process discovery and the kill/launch; this only decides.

THE 180s THRESHOLD IS MEASURED, NOT GUESSED. The host's own backoff recovered the relay
session within 7 minutes in the 07:34 event and within ~1 minute in short drops. A reader that
has been alive and reconnecting for LESS than this window is still inside its own recovery, and
replacing it there races that recovery -- which is exactly the double-host state that caused the
outage. Only after a reader has been alive but WITHOUT a relay connection for longer than the
threshold is it treated as wedged rather than reconnecting, and only then is it torn down and
replaced by exactly one new reader.
"""
from __future__ import annotations

#: Seconds a reader may be alive-but-without-a-relay-connection before it is treated as wedged
#: rather than reconnecting. Justified by the measured reconnect behaviour (see the module
#: docstring): the host's own backoff recovered within 7 minutes in the 07:34 event and within
#: ~1 minute in short drops, so a shorter window would race its own recovery and re-create the
#: two-hosts-for-one-tunnel state this exists to prevent.
RECONNECT_GRACE_SECONDS = 180


def decide_host_launch(reader_alive, seconds_without_relay,
                       grace=RECONNECT_GRACE_SECONDS):
    """Whether the supervisor may replace the existing reader, or must keep waiting.

    Inputs (both measured by the caller, see Start-TunnelHost):

      * ``reader_alive`` -- is an existing ``relay.tunnel_host_log <tunnel>`` reader process
        (the supervisor child that owns the devtunnel host) alive right now?
      * ``seconds_without_relay`` -- for how long has that reader been alive WITHOUT a relay
        connection (host connections == 0)? ``None`` when it cannot be told, or when the
        reader is not alive.

    Returns one of two words:

      * ``"replace"`` -- no reader is alive (nothing to wait for -> launch exactly one), OR a
        reader is alive but has had no relay connection for longer than ``grace`` seconds
        (wedged, not reconnecting): stop its tree first, then launch exactly one new reader.
      * ``"wait"``    -- a reader is alive and is still within its own reconnect window (less
        than ``grace`` seconds without a relay, or the duration cannot be told): do NOT launch
        a second reader; keep waiting for the host's own backoff to recover the relay session.

    Pure: no I/O, no clock, no process inspection. The caller does the discovery and acts on
    the word. ``seconds_without_relay`` is tolerant of a missing or malformed value: ``None``
    and anything non-numeric both read as "cannot tell how long", which -- while a reader is
    alive -- means WAIT (never race the reader's own recovery on an unreadable duration).
    """
    if not reader_alive:
        # No existing reader (or it just died): there is nothing to race, launch exactly one.
        return "replace"
    try:
        g = float(grace)
    except (TypeError, ValueError):
        g = float(RECONNECT_GRACE_SECONDS)
    if seconds_without_relay is None:
        # A reader is alive but we cannot tell how long it has been without a relay. Err on
        # the side of NOT launching a second host -- an unreadable duration must never be the
        # reason two hosts end up fighting for one tunnel.
        return "wait"
    try:
        secs = float(seconds_without_relay)
    except (TypeError, ValueError):
        return "wait"
    if secs > g:
        # Alive but no relay connection for longer than its own measured recovery window ->
        # wedged. Replace it (the caller stops the reader + its devtunnel child first).
        return "replace"
    # Alive and still inside the reconnect window -> let its own backoff finish; do not race it.
    return "wait"

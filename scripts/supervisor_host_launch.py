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


#: Seconds a reader may stay alive while the relay reports 0 host connections -- i.e. its own
#: devtunnel CHILD is alive but has lost (and is not recovering) its relay session -- before the
#: supervisor stops ONLY that devtunnel child so the reader exits and the existing re-host logic
#: runs. This is the J6 blind spot: decide_host_launch's RECONNECT_GRACE_SECONDS (180s) bounds how
#: long a reader may WAIT before a *replace* is allowed, but a reader whose child is alive-but-
#: disconnected keeps answering "reader alive" and decide_host_launch keeps returning "wait"
#: forever, so nothing ever replaces it (measured 2026-11-04 below).
#:
#: WHY 600s, FROM THE MEASURED RECONNECT TIMES (do not shorten without re-measuring):
#:   * A NORMAL re-host, where the devtunnel child EXITS and relay.tunnel_host_log restarts it,
#:     recovers in well under ~90s: the child's own backoff is capped at 60s
#:     (relay.tunnel_host_log.backoff_seconds cap=60) and Start-TunnelHost waits up to ~50s for the
#:     fresh host to register -- so a child that exits is back within ~1-2 min.
#:   * A reader's OWN in-place recovery (the devtunnel child stays alive and reconnects its relay
#:     session internally) recovered within ~1 minute in short network drops and within 7 minutes
#:     (420s) in the worst case measured -- the 2026-11-02 07:34 event, the same measurement that
#:     justifies RECONNECT_GRACE_SECONDS. 600s sits safely ABOVE that 420s worst case, so this
#:     strike never races a child that is still genuinely reconnecting.
#:   * THE FAILURE THIS FIXES (measured 2026-11-04, %TEMP%\m365-companion-supervisor.log and
#:     .fleet\tunnel_host.json): at 03:46:30 "tunnel host connections = 0 (1/4)"; by 03:49:00 the
#:     reader-wait had reached 4/4 and logged "reader pid 16680 still running, host reconnecting
#:     (waited 0 s)". The old devtunnel child (pid 8304) was alive but had NO relay connection and
#:     nothing replaced it for 7 minutes (03:46-03:53); external /health through the tunnel URL
#:     timed out that whole period. It was stopped by hand at 03:53:19 and the tunnel was back at
#:     03:56:33. 600s > 420s means the supervisor itself strikes the stuck child a few minutes
#:     after its own recovery window has demonstrably closed, instead of waiting forever.
CHILD_RELAY_GRACE_SECONDS = 600


def decide_child_strike(seconds_with_zero_connections, reader_alive, child_pid,
                        grace=CHILD_RELAY_GRACE_SECONDS):
    """Whether to stop ONLY this PC's own devtunnel child so a wedged reader exits and re-hosts.

    This is the companion of :func:`decide_host_launch` for the state that function cannot act on:
    a reader that is ALIVE (so decide_host_launch returns "wait") but whose devtunnel child has
    been alive without any relay connection for so long that it is clearly not coming back.

    Inputs (all measured by the caller, see Invoke-TunnelHostingCheck / Stop-StuckTunnelChild):

      * ``seconds_with_zero_connections`` -- for how long the relay has reported 0 host
        connections while a reader of ours was alive (i.e. the reader-wait duration). ``None``
        when it cannot be told.
      * ``reader_alive`` -- is a ``relay.tunnel_host_log <tunnel>`` reader of ours alive now?
      * ``child_pid`` -- the pid of THIS reader pair's own devtunnel child (the child of our
        reader). ``None`` / 0 when no such child could be identified.
      * ``grace`` -- the ceiling above; a non-numeric value falls back to the default.

    Returns one of two words:

      * ``"strike"`` -- a reader of ours is alive, its own devtunnel child pid is known, and the
        relay has been at 0 connections for longer than ``grace`` seconds: stop ONLY that child
        (never any other devtunnel process), so the reader exits and the existing re-host logic
        brings the tunnel back with exactly one reader.
      * ``"wait"``  -- anything else: no reader alive (decide_host_launch's "replace" path owns
        that), no identifiable child of ours to stop, the duration cannot be told, or it is still
        within ``grace`` (the child may yet reconnect in place -- never race its own recovery).

    Pure: no I/O, no clock, no process inspection. The caller does the discovery (which devtunnel
    process is the child of OUR reader) and performs the single, scoped Stop-Process; this only
    decides. A missing or malformed duration reads as "cannot tell" -> WAIT, so an unreadable gap
    is never the reason a child is struck.
    """
    if not reader_alive:
        # No reader of ours is alive: there is no child of ours to strike, and launching/replacing
        # is decide_host_launch's job, not this one.
        return "wait"
    try:
        pid = int(child_pid)
    except (TypeError, ValueError):
        return "wait"
    if pid <= 0:
        # No identifiable devtunnel child of our reader -> nothing scoped to stop.
        return "wait"
    try:
        g = float(grace)
    except (TypeError, ValueError):
        g = float(CHILD_RELAY_GRACE_SECONDS)
    if seconds_with_zero_connections is None:
        # A reader is alive but we cannot tell how long the relay has been at 0 -> never strike on
        # an unreadable duration.
        return "wait"
    try:
        secs = float(seconds_with_zero_connections)
    except (TypeError, ValueError):
        return "wait"
    if secs > g:
        # Alive, its child is known, and 0 connections for longer than the measured worst-case
        # in-place recovery window -> the child is wedged, not reconnecting. Strike only it.
        return "strike"
    return "wait"

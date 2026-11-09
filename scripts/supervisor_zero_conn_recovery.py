# -*- coding: utf-8 -*-
r"""When a reader's host stays at zero relay connections, end only its host child.

WHY THIS EXISTS (measured 2026-11-04, %TEMP%\m365-companion-supervisor.log)
---------------------------------------------------------------------------
The host process of this PC stayed alive with ZERO relay connections and nothing
replaced it for ~7 minutes (the GOAL names 03:46-03:53 UTC; the same shape is in the
log this machine kept). The reason is the duplicate-launch guard in Start-TunnelHost
(scripts/supervisor.ps1): while a reader (``python -m relay.tunnel_host_log``) is alive
and still inside its own reconnect window it logs ``reader pid N still running, host
reconnecting`` and WAITS -- it must never start a second reader (that was the 2026-11-02
two-hosts-for-one-tunnel outage). So a reader whose devtunnel host child has silently
stopped carrying traffic, but whose own backoff is not recovering the relay, is left in
place with zero connections and nobody takes over.

THE MEASURED NUMBERS (%TEMP%\m365-companion-supervisor.log, 2026-11-04)
----------------------------------------------------------------------
  * 09:10:34 -> 09:13:03  ``tunnel host connections = 0 (1/4)``..``(4/4)`` -- the count
    sat at 0 for ~149 s across four ticks while a reader was alive.
  * 09:13:04  ``reader pid 39412 still running, host reconnecting (waited 157 s)`` --
    the longest the reader was observed alive WITHOUT a relay before this guard still
    said WAIT; its own backoff had not recovered the relay.
  * 09:57:23  ``devtunnel host established (after ~8s)`` -- a fresh host re-registers
    with the relay in ~8 s, so ending the stuck host child and letting the reader's own
    serve_forever() restart it costs ~8 s of downtime, far less than the 7-minute gap.

So a reader that has been at zero connections for longer than GRACE is treated as having
a WEDGED host child rather than a reconnecting one, and the fix ends ONLY that host child
(the devtunnel.exe this reader spawned). The reader stays alive; its serve_forever() loop
(relay/tunnel_host_log.py) sees the host exit and starts a new one after its backoff --
exactly one host, exactly one reader, no second reader ever started.

THE GRACE IS MEASURED, NOT GUESSED. 240 s sits above the longest observed
alive-without-relay reconnect (157 s) with margin, so a host child that is genuinely
reconnecting on its own backoff is never cut short, and above the four-tick
``connections = 0`` debounce (~149 s) so the decision is only reached after the existing
counter has already given up. It is well under the 7-minute gap this exists to close.
This is deliberately >= the reader-replacement window in
scripts/supervisor_host_launch.py (180 s): ending just the host child is the gentler
first move and is given more room than tearing the whole reader tree down.

Pure: no I/O, no clock, no process calls. The PowerShell side (Start-TunnelHost) measures
the inputs and performs the kill; this only decides the word.
"""
from __future__ import annotations

#: Seconds a reader may sit at zero relay connections before its host child is treated as
#: wedged (not reconnecting) and ended. Justified by the measured numbers in the module
#: docstring: above the 157 s longest observed reconnect and the ~149 s four-tick debounce,
#: well under the ~7 minute gap, and >= supervisor_host_launch.RECONNECT_GRACE_SECONDS (180).
ZERO_CONN_GRACE_SECONDS = 240


def decide_zero_conn_child_kill(seconds_at_zero, reader_alive, child_pid,
                                grace=ZERO_CONN_GRACE_SECONDS):
    """Whether to end this reader's host child so the reader restarts it, or keep waiting.

    Inputs (all measured by the caller, see Start-TunnelHost in scripts/supervisor.ps1):

      * ``seconds_at_zero`` -- for how long the reader has been alive with the relay at
        zero connections (host connections == 0). ``None`` when it cannot be told.
      * ``reader_alive`` -- is the ``relay.tunnel_host_log <tunnel>`` reader alive now?
      * ``child_pid`` -- the pid of the devtunnel host child this reader spawned, or
        ``None``/0 when no such child could be found.
      * ``grace`` -- seconds at zero before the child is treated as wedged (default
        ZERO_CONN_GRACE_SECONDS).

    Returns one of two words:

      * ``"kill_child"`` -- a reader is alive, it owns a known host child, and it has been
        at zero connections for longer than ``grace``: end ONLY that host child. The
        reader stays alive and its serve_forever() loop starts a new host; no second
        reader is ever started.
      * ``"wait"``       -- anything else: no reader, no known child pid, the duration
        cannot be told, or still inside ``grace``. Do nothing; let the reader's own
        backoff recover the relay.

    Pure: no I/O, no clock, no process inspection. A missing or malformed
    ``seconds_at_zero`` reads as "cannot tell how long" and therefore WAIT -- an
    unreadable duration must never be the reason a host child is killed.
    """
    if not reader_alive:
        # No reader to recover around; the reader-level guard (decide_host_launch) owns
        # the "no reader" case by launching one.
        return "wait"
    if child_pid is None:
        return "wait"
    try:
        pid = int(child_pid)
    except (TypeError, ValueError):
        return "wait"
    if pid <= 0:
        return "wait"
    if seconds_at_zero is None:
        # Alive, has a child, but we cannot tell how long it has been at zero -> wait.
        return "wait"
    try:
        secs = float(seconds_at_zero)
    except (TypeError, ValueError):
        return "wait"
    try:
        g = float(grace)
    except (TypeError, ValueError):
        g = float(ZERO_CONN_GRACE_SECONDS)
    if secs > g:
        # At zero past its reconnect window with a live reader and a known host child ->
        # end just that child; the reader restarts it.
        return "kill_child"
    return "wait"

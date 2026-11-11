# -*- coding: utf-8 -*-
r"""When devtunnel login returns, re-host the tunnel at once -- skip the debounce.

WHY THIS EXISTS (measured 2026-11-11, the GOAL's incident window)
-----------------------------------------------------------------
The Dev Tunnel 'resonac-mcp' was unhosted from 16:39:30 to 17:42:26 (63 min). The
devtunnel CLI login had expired ('Not logged in'), so scripts/supervisor.ps1 PAUSED
tunnel management (it must not touch devtunnel while logged out -- a `devtunnel host`
launched then fails and can reap an interactive `devtunnel login` the owner is running).
The owner signed in at 17:35:44, but the supervisor only re-hosted at 17:42:26 -- about
7 minutes later. The gap is the debounce: once login returns, the tunnel-hosting tick
sees state 'none' (nobody hosts it) and waits FailuresBeforeAction consecutive
'tunnel host connections = 0' checks (4 ticks, ~80 s at this loop's pace) before it calls
Start-TunnelHost -- the same debounce that is correct for a transient blip mid-run.

WHY THE DEBOUNCE IS WRONG ON THE LOGIN-RESUME TICK
--------------------------------------------------
The debounce exists so a single transient 'devtunnel show' glitch on a tunnel that WAS
working does not trigger a needless re-host. But the tick where login has just returned
is not that case: management was PAUSED, so nothing of this PC was hosting the tunnel the
whole time it was logged out. A 'none' reading here is a known, settled fact, not a blip.
There is nothing a fast re-host could knock over -- exactly the reasoning the supervisor
already uses for its server VANISHED fast path (Get-VanishedServerVerdict) and its tunnel
STARTUP fast path ('tunnel host connections = 0 at startup -> hosting ... without the
debounce'). This applies the same 'nothing to protect' rule to the login-resume tick.

Narrowly scoped: ONLY the first tick after login flips from down to up, and ONLY when the
state is 'none'. 'foreign'/'shared' (another PC serves this PC's tunnel) is never fought,
here or anywhere; 'ours'/'stale' keep their existing handling. The ps1 side still makes
its live, uncached 'logged in' re-check before Start-TunnelHost touches devtunnel, so this
cannot re-host on a cached login answer.

Pure: no I/O, no clock, no process calls. The PowerShell side (Invoke-TunnelHostingCheck)
observes whether login just resumed and the current state; this only decides the word.
"""
from __future__ import annotations


def decide_login_resume_rehost(login_just_resumed, tunnel_state):
    """Whether the login-resume tick may skip the debounce and re-host now.

    Inputs (both measured by the caller, see Invoke-TunnelHostingCheck in
    scripts/supervisor.ps1):

      * ``login_just_resumed`` -- did the devtunnel CLI login flip from DOWN to UP on this
        very tick? True only on the single tick where ``$loggedIn`` was ``$false`` and the
        live re-check now says logged in. A steady-state logged-in tick is False.
      * ``tunnel_state`` -- the resolved hosting state this tick: ``"none"``, ``"ours"``,
        ``"stale"``, ``"foreign"`` or ``"shared"`` (see Resolve-TunnelHostingState).

    Returns one of two words:

      * ``"rehost_now"`` -- login just came back AND nobody hosts the tunnel
        (``tunnel_state == "none"``): re-host on this tick without waiting out the
        FailuresBeforeAction debounce. The caller still makes its live, uncached
        'logged in' re-check before touching devtunnel.
      * ``"debounce"``   -- anything else: not the login-resume tick, or the state is not
        'none'. Keep the existing per-state handling (the debounce, 'ours', 'stale',
        'foreign'/'shared' are all unchanged).

    Pure: no I/O, no clock, no process inspection. A non-string or unexpected state reads
    as 'not none' and therefore ``"debounce"`` -- an unreadable state must never be the
    reason the debounce is skipped.
    """
    if login_just_resumed is not True:
        return "debounce"
    # Must be a real string. ``str(None)`` is the literal "none", which would otherwise be
    # mistaken for the no-host state and wrongly skip the debounce -- an unreadable state must
    # never be the reason the fast path fires.
    if not isinstance(tunnel_state, str):
        return "debounce"
    if tunnel_state.strip().lower() == "none":
        return "rehost_now"
    return "debounce"

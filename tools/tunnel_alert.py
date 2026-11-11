# -*- coding: utf-8 -*-
"""Turn a Dev Tunnel probe result into a VISIBLE alert that names WHICH cause.

The availability review (docs/availability/06_incident_timeline_and_alerting.md and
04_devtunnel_recovery.md) settled on one existing, repo-standard alert surface:
``tools.notify_ops.notify_desktop`` -- a native Windows toast whose body may be multi-line,
whose click target is ``launch``, and which is inert under pytest by construction. This module
is the CONNECTING FACE between a tunnel probe and that surface: it does not poll anything
itself (an always-on probe is a separate concern/worktree). It takes a probe observation,
decides which of the four failure causes it is, and raises ONE toast that states the cause and
carries the operator's next step in its own body.

The incident showed that tunnel failures need an actionable notification.
supervisor merely wrote one log line when login expired. The defect this module closes is the
NOTIFICATION FACE of that: a failure must become a visible alert within 60 s and must say WHICH
cause it is, because the four causes need four different human actions:

  - not_logged_in     -> ``devtunnel user login`` must be run IN A VISIBLE CONSOLE (this
                         tenant's Entra forbids device-code; a hidden window silently falls
                         back to -d and is blocked). Tunnel management is PAUSED until then.
  - no_host           -> Host connections = 0. The supervisor re-hosts; if it does not,
                         restart via start_all.bat. The local server and login are both fine.
  - local_server_down -> main.py is not answering /health locally. Restart main.py; the tunnel
                         itself may still be hosted.
  - public_path_down  -> local /health is fine and we are hosted, but the PUBLIC URL does not
                         answer. The relay path (devtunnels.ms) is the suspect, not login.

An alert that cannot be acted on teaches the reader to clear it. So every cause writes its own
remedy into the toast body, and ``launch`` points at the logs folder so a click lands somewhere.

NO CREDENTIAL IS EVER PUT IN AN ALERT. This module is given booleans/counts/status strings, not
tokens. A file or a toast is the last place a key should land.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Dict, Optional

# The four causes, as machine-stable strings. Ordered by how they are decided below.
CAUSE_NOT_LOGGED_IN = "not_logged_in"
CAUSE_NO_HOST = "no_host"
CAUSE_LOCAL_SERVER_DOWN = "local_server_down"
CAUSE_PUBLIC_PATH_DOWN = "public_path_down"

ALL_CAUSES = (
    CAUSE_NOT_LOGGED_IN,
    CAUSE_NO_HOST,
    CAUSE_LOCAL_SERVER_DOWN,
    CAUSE_PUBLIC_PATH_DOWN,
)


@dataclass(frozen=True)
class ProbeObservation:
    """One reading from the always-on probe. All fields are plain facts, never a credential.

    The probe (separate concern) fills these in; this module only interprets them.

    Attributes:
        logged_in: ``devtunnel user show`` reports a signed-in user. False == 'Not logged in'.
        host_connections: Host-connection count from ``devtunnel show <name>``. None means the
            CLI could not be reached (treated as unknown, NOT as zero -- conflating the two is
            the 2026-11-04 class of defect). 0 means genuinely no host.
        local_health_ok: the local MCP handshake succeeded.
        public_health_ok: the PUBLIC MCP handshake succeeded
            (https://<id>-8000.jpe1.devtunnels.ms/health).
    """

    logged_in: bool
    host_connections: Optional[int]
    local_health_ok: bool
    public_health_ok: bool

    def healthy(self) -> bool:
        """True iff nothing is wrong: logged in, at least one host, local AND public both up."""
        return (
            self.logged_in
            and (self.host_connections or 0) >= 1
            and self.local_health_ok
            and self.public_health_ok
        )


@dataclass(frozen=True)
class Alert:
    """What a cause turns into before it is shown. Pure data, so a test can read it directly."""

    cause: str
    title: str
    body: str
    # Whether the remedy requires opening an interactive login in a VISIBLE console. Only the
    # not_logged_in cause sets this; the alert text then says so (goal (c)'s notification half).
    needs_visible_console_login: bool = False
    launch: str = ""


def classify_probe(obs: ProbeObservation) -> Optional[str]:
    """Which of the four causes is it, or None when the observation is healthy.

    DECISION ORDER MATTERS, and it mirrors the dependency chain. Login gates everything: when
    the tunnel is not logged in, the supervisor PAUSES tunnel management, so a later 'no host'
    is a SYMPTOM of the login, not an independent cause. Reporting 'no host' there would send
    the operator to restart a tunnel that cannot be hosted until they log in. So:

      1. not logged in            -> CAUSE_NOT_LOGGED_IN   (the root; everything else waits)
      2. local /health is down    -> CAUSE_LOCAL_SERVER_DOWN (nothing downstream can be up)
      3. no host connection       -> CAUSE_NO_HOST
      4. public path is down      -> CAUSE_PUBLIC_PATH_DOWN (logged in, hosted, local up)

    host_connections is None (CLI unreachable) is NOT treated as zero: it cannot by itself
    prove 'no host'. If login and local health are fine and the public path is up, an unknown
    count is not an alertable failure; if the public path is ALSO down, that is reported as
    public_path_down, which is the actionable fact.
    """
    if obs.healthy():
        return None
    if not obs.logged_in:
        return CAUSE_NOT_LOGGED_IN
    if not obs.local_health_ok:
        return CAUSE_LOCAL_SERVER_DOWN
    if (obs.host_connections is not None) and obs.host_connections <= 0:
        return CAUSE_NO_HOST
    if not obs.public_health_ok:
        return CAUSE_PUBLIC_PATH_DOWN
    return None


def _logs_launch() -> str:
    """A file:/// URI to the repo's logs folder, so a clicked toast lands somewhere real.

    Best effort: computed from this file's location. Never raises.
    """
    try:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        logs = os.path.join(repo, "logs")
        return "file:///" + logs.replace("\\", "/")
    except Exception:
        return ""


#: Per-cause toast text. Each body is self-contained: it names the cause and the remedy, so the
#: operator can act from the body alone even if the click target does nothing (unregistered
#: AppId). PUBLIC_URL is filled in when known.
_PUBLIC_HEALTH = "the public MCP handshake"


def build_alert(cause: str, *, public_url: str = "", logs_launch: str = "") -> Alert:
    """Turn a cause string into the Alert that will be shown. Pure; no I/O, no toast.

    Separated from raising so a test can assert the body names the cause and the remedy without
    a desktop. ``public_url`` is woven into the body when provided; it is a URL, never a secret.
    """
    if cause not in ALL_CAUSES:
        raise ValueError("unknown cause: %r" % (cause,))
    launch = logs_launch or _logs_launch()
    pub = public_url.strip() or _PUBLIC_HEALTH

    if cause == CAUSE_NOT_LOGGED_IN:
        return Alert(
            cause=cause,
            title="Dev Tunnel: NOT LOGGED IN -- tunnel management PAUSED",
            body=(
                "Cause: devtunnel user show reports 'Not logged in'.\n"
                "Effect: the supervisor PAUSES tunnel hosting until login returns; the public "
                "URL will not come back on its own.\n"
                "Fix: run 'devtunnel user login' IN A VISIBLE CONSOLE WINDOW. Do NOT use a "
                "hidden window and do NOT use device-code (-d): this tenant's Entra blocks "
                "device-code, and a hidden login silently falls back to it and fails.\n"
                "After login, hosting should resume within ~60 s."
            ),
            needs_visible_console_login=True,
            launch=launch,
        )
    if cause == CAUSE_LOCAL_SERVER_DOWN:
        return Alert(
            cause=cause,
            title="Dev Tunnel: LOCAL SERVER DOWN (local MCP handshake failing)",
            body=(
                "Cause: the local MCP handshake failed. The tunnel may "
                "still be hosted, but there is nothing behind it.\n"
                "Fix: restart main.py (start_all.bat). Login and the tunnel host are not the "
                "problem here.\n"
                "Check the logs folder for main.py's exit."
            ),
            launch=launch,
        )
    if cause == CAUSE_NO_HOST:
        return Alert(
            cause=cause,
            title="Dev Tunnel: NO HOST (host connections = 0)",
            body=(
                "Cause: the tunnel has 0 host connections -- the public URL cannot reach this "
                "PC. Login is fine and main.py is up locally.\n"
                "Fix: the supervisor should re-host within ~60 s. If it does not, restart via "
                "start_all.bat. Do NOT kill devtunnel processes by image name (that can kill "
                "an interactive login)."
            ),
            launch=launch,
        )
    # CAUSE_PUBLIC_PATH_DOWN
    return Alert(
        cause=cause,
        title="Dev Tunnel: PUBLIC PATH DOWN (local up, public URL failing)",
        body=(
            "Cause: the local MCP handshake is fine, but %s is not "
            "answering. The relay path (devtunnels.ms) is the suspect, not login or main.py.\n"
            "Fix: check the tunnel host log for relay 'Connection lost' churn; the supervisor "
            "re-hosts stale/ghost connections. If it persists, re-run start_all.bat." % pub
        ),
        launch=launch,
    )


# Type of the notify sink, so tests can inject a fake and this module need not import the GUI
# path at module load. The real default is bound lazily inside raise_tunnel_alert.
NotifyFn = Callable[..., str]


def raise_tunnel_alert(
    obs: ProbeObservation,
    *,
    public_url: str = "",
    notify: Optional[NotifyFn] = None,
) -> Optional[Dict[str, str]]:
    """Classify the observation and, if it is a failure, raise ONE visible toast naming the cause.

    Returns None when the observation is healthy (no alert raised). Otherwise returns a small
    dict describing what was shown: {'cause', 'title', 'needs_visible_console_login',
    'notify_result'}. The dict NEVER contains a credential.

    ``notify`` defaults to tools.notify_ops.notify_desktop, which is inert under pytest by
    construction -- so calling this in a test does not pop a real toast. Tests that want to see
    the arguments pass their own callable.
    """
    cause = classify_probe(obs)
    if cause is None:
        return None
    alert = build_alert(cause, public_url=public_url)

    if notify is None:
        # Bind lazily so importing this module costs nothing and needs no GUI stack.
        from tools.notify_ops import notify_desktop as notify  # type: ignore

    result = notify(alert.title, alert.body, launch=alert.launch)
    return {
        "cause": alert.cause,
        "title": alert.title,
        "needs_visible_console_login": "1" if alert.needs_visible_console_login else "0",
        "notify_result": str(result),
    }

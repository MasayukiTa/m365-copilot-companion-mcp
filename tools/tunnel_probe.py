"""Pure classification for an always-on probe of the public Dev Tunnel path.

This module is the DECISION CORE of the public-URL / tunnel-state watcher. It takes
already-measured observations (HTTP results, `devtunnel user show` text, host
connection count) and returns a single verdict that names WHICH cause is at fault:

  - ok                : public path answers with this checkout's main.py /health body
  - not_logged_in     : `devtunnel user show` reports no signed-in user
  - no_host           : signed in, but Host connections == 0 (nobody hosting the tunnel)
  - local_server_down : local 127.0.0.1 /health does not answer the main.py body
  - public_path_down  : local server is up and a host exists, but the PUBLIC url does not
                        return the main.py body (relay 404/504, sign-in/anti-phishing page,
                        or a body from a different process)

WHY A PURE MODULE (not just PowerShell): CI on this repo is Linux/pytest only -- no
Pester, no Windows. Putting the verdict logic here lets it be covered by hermetic
tests that actually run in CI, while the PowerShell watcher (scripts/public_url_probe.ps1)
stays a thin shell that measures and then calls `classify`.

NO NETWORK, NO SUBPROCESS HERE. Every input is passed in. The caller does the I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

# Machine-stable verdict codes. The alert text keys off these, so they are part of the
# contract and must not drift.
OK = "ok"
NOT_LOGGED_IN = "not_logged_in"
NO_HOST = "no_host"
LOCAL_SERVER_DOWN = "local_server_down"
PUBLIC_PATH_DOWN = "public_path_down"


def is_healthy_mainpy_body(body: Optional[str]) -> bool:
    """True only when `body` is main.py's /health answer: JSON with status == "ok"
    AND an integer server_pid > 0.

    SAME RULE AS supervisor.ps1 Get-HealthServerPid and measure_public_url_ready.ps1
    Test-IsHealthyMainPyBody. A 200 that is a relay sign-in / anti-phishing page, or any
    other body, is NOT healthy -- that is exactly the false-positive an always-on probe
    must avoid. Kept byte-for-byte equivalent to the PowerShell rule so the watcher and
    this module never disagree about what 'ready' means.
    """
    if not body:
        return False
    try:
        j = json.loads(body)
    except (ValueError, TypeError):
        return False
    if not isinstance(j, dict) or j.get("status") != "ok":
        return False
    sp = j.get("server_pid", None)
    try:
        return int(sp) > 0
    except (ValueError, TypeError):
        return False


def is_logged_in(user_show_text: Optional[str]) -> bool:
    """Parse `devtunnel user show` stdout for a signed-in user.

    The CLI prints 'Not logged in' (or 'Not logged in.') when the token cache is empty
    or expired, and a line naming the account when signed in. Treat an empty/whitespace
    output, or any text containing 'not logged in' (case-insensitive), as NOT signed in.
    Everything else -- a line with an account name / 'Logged in as ...' -- counts as
    signed in. This is deliberately conservative: an unreadable result is treated as not
    logged in so the watcher raises the login alert rather than silently assuming auth.
    """
    if not user_show_text or not user_show_text.strip():
        return False
    low = user_show_text.lower()
    if "not logged in" in low:
        return False
    return True


@dataclass(frozen=True)
class Observation:
    """Everything the watcher measured on one tick. All I/O is done by the caller."""

    user_show_text: Optional[str]      # stdout of `devtunnel user show` (None if CLI unreachable)
    host_connections: Optional[int]    # Host connections count from `devtunnel show <name>` (None if unknown)
    local_ok: bool                     # local 127.0.0.1 /health returned the main.py body
    public_ok: bool                    # PUBLIC url /health returned the main.py body


@dataclass(frozen=True)
class Verdict:
    code: str          # one of the module constants above
    healthy: bool      # True only for OK
    detail: str        # short human-facing clause for the alert body


def classify(obs: Observation) -> Verdict:
    """Return the single most-upstream cause. Order matters: a failure high in the chain
    makes the ones below it meaningless, so it is reported alone rather than as a pile.

    Chain (most upstream first):
      1. not signed in  -> nothing downstream can be hosted; this is cause #1 from the task.
      2. local server    -> if 127.0.0.1 /health is not main.py, the public path cannot be
                            either; naming 'public down' here would mislead (the 2026-11-04
                            'slow /health read as dead server' class of mistake).
      3. no host         -> signed in and local up, but Host connections == 0: nobody is
                            hosting, so the public URL is unreachable by construction.
      4. public path     -> signed in, local up, a host exists, yet the PUBLIC body is wrong
                            (relay 404/504, sign-in page, or a foreign pid). This is the
                            relay/path problem, distinct from the three above.
      5. ok              -> public path returns this checkout's main.py body.
    """
    if not is_logged_in(obs.user_show_text):
        return Verdict(NOT_LOGGED_IN, False,
                       "devtunnel is not signed in (token cache empty or expired)")
    if not obs.local_ok:
        return Verdict(LOCAL_SERVER_DOWN, False,
                       "local 127.0.0.1 /health is not answering main.py's body")
    # host_connections is None when the CLI could not be read; treat unknown as 'no host'
    # only when we also cannot see the public body, otherwise a reachable public path is
    # itself proof a host exists.
    if (obs.host_connections is not None and obs.host_connections <= 0) and not obs.public_ok:
        return Verdict(NO_HOST, False,
                       "signed in and local server up, but Host connections == 0")
    if not obs.public_ok:
        return Verdict(PUBLIC_PATH_DOWN, False,
                       "public URL /health is not returning this checkout's main.py body "
                       "(relay 404/504, sign-in page, or a different process)")
    return Verdict(OK, True, "public path returns this checkout's main.py /health body")

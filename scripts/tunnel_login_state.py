"""Decide Dev Tunnel LOGIN state and the failing HOP without ever calling the devtunnel CLI.

WHY THIS EXISTS (measured 2026-11-11)
-------------------------------------
The gateway was unusable for hours and monitoring never noticed, because:

  * the only liveness check was a GET /health, which stays 200 while the PUBLIC relay has no
    host -- so a logged-out devtunnel (the relay answering POST /mcp initialize with HTTP 404
    after 50-60 s, measured ~25 times) was invisible; and
  * the monitor that DID watch the tunnel ran ``devtunnel user show`` / ``devtunnel show``
    every 6-8 s, and twice a fresh login was lost within about a minute of such a burst. The
    login at 20:40 survived 4+ hours when NOTHING called the devtunnel CLI during or after
    sign-in and only the token file (size + mtime) and the supervisor log were read.
    Hypothesis (one supportive case, not proven): concurrent devtunnel CLI invocations
    corrupt the token cache.

So the probe must decide login state WITHOUT the CLI. It reads two facts the caller gathers
with no subprocess at all:

  * the devtunnel token file -- %LOCALAPPDATA%\\DevTunnels\\devtunnels-tokens-microsoft --
    whose SIZE is 4182 bytes (non-zero) when a login is valid and 0 bytes when it is not
    (measured 2026-11-11); and
  * the result of the REAL public MCP handshake (status + elapsed) and the supervisor log.

EVERYTHING HERE IS PURE (no I/O, no clock, no process calls, stdlib only), exactly like
``scripts/supervisor_hang_detect.decide_hang_restart`` and
``scripts/supervisor_host_launch.decide_child_strike``, so it runs in the Linux CI test job on
any OS. The caller owns the filesystem read, the HTTP probe and the clock, and hands the facts
in. It must NOT import or shell out to devtunnel; test_probe_never_shells_out_to_devtunnel.py
fails if this module (or the reference probe) does.
"""

from __future__ import annotations

#: The token file is non-empty when a login is valid and exactly 0 bytes when it is not
#: (measured 2026-11-11: 4182 bytes valid, 0 bytes logged-out). A file that does not exist is
#: treated the same as 0 bytes: no login.
LOGIN_VALID = "logged_in"
LOGIN_NONE = "logged_out"
LOGIN_UNKNOWN = "unknown"

#: How old the token file's mtime may be before a non-zero file is no longer trusted on its
#: size alone. The token cache is rewritten on sign-in and on refresh; a non-empty file whose
#: mtime is wildly in the future (a clock fault) is reported UNKNOWN rather than VALID so a
#: corrupt timestamp cannot read as a healthy login. There is no lower bound: an old but
#: non-empty token is still a token, and whether the relay actually has a host is answered by
#: the public probe, not by this file.
_MAX_FUTURE_SKEW_SECONDS = 300


def decide_login_state(token_size, token_mtime=None, now=None,
                       max_future_skew_s=_MAX_FUTURE_SKEW_SECONDS):
    """Login state from the token file's size and mtime alone -- never the devtunnel CLI.

    Inputs (all gathered by the caller with os.stat; no subprocess):

      * ``token_size``  -- size in bytes of the devtunnel token file, or ``None`` when the
                           file does not exist. 0 or None -> logged out. A read error the
                           caller cannot turn into a size is passed as ``None`` too: absence
                           and no-login are the same operational fact here.
      * ``token_mtime`` -- the file's mtime (epoch seconds), or ``None`` when unavailable.
                           Used only to reject a timestamp impossibly far in the FUTURE; a
                           past mtime never downgrades a non-empty token (see the constant).
      * ``now``         -- current epoch seconds, supplied by the caller's clock. ``None``
                           means skip the skew check (size alone decides).
      * ``max_future_skew_s`` -- how far ahead of ``now`` an mtime may sit before a non-empty
                           token is reported UNKNOWN. Non-numeric / non-positive disables the
                           check (fail OPEN to the size verdict; this module never kills).

    Returns one of ``LOGIN_VALID`` / ``LOGIN_NONE`` / ``LOGIN_UNKNOWN``. This function judges
    ONLY whether a token exists; whether the relay has a live host is decided by
    ``classify_public_hop`` from the public probe, not here.
    """
    # No file, or an empty file: no login. This is the measured logged-out shape (0 bytes).
    if token_size is None:
        return LOGIN_NONE
    try:
        size = int(token_size)
    except (TypeError, ValueError):
        return LOGIN_UNKNOWN  # unreadable size: do not claim a login we cannot see
    if size <= 0:
        return LOGIN_NONE
    # Non-empty token. Guard only against an mtime impossibly in the future, which would mean
    # the timestamp is corrupt and the file cannot be trusted on size alone.
    if now is not None and token_mtime is not None:
        try:
            skew = float(max_future_skew_s)
        except (TypeError, ValueError):
            skew = 0.0
        if skew > 0:
            try:
                ahead = float(token_mtime) - float(now)
            except (TypeError, ValueError):
                return LOGIN_VALID  # unreadable clock: size already proved a token exists
            if ahead > skew:
                return LOGIN_UNKNOWN
    return LOGIN_VALID


#: Hop names the probe reports so an alert can say WHICH hop failed and WHY.
HOP_OK = "ok"
HOP_LOCAL_DOWN = "local_server_down"
HOP_PUBLIC_NO_HOST = "public_relay_no_host"
HOP_AUTH = "auth"
HOP_PUBLIC_SLOW = "public_slow"
HOP_LOCAL_SLOW = "local_slow"
HOP_PUBLIC_OTHER = "public_other"

#: A handshake slower than this is a failure even if it eventually returns 200: the measured
#: logged-out relay answered its 404 only after 50-60 s, and a live gateway must answer the
#: real MCP initialize far faster. 30 s is the ceiling the operator set.
SLOW_SECONDS = 30


def classify_public_hop(status, elapsed_s, login_state, slow_s=SLOW_SECONDS):
    """Classify the PUBLIC MCP handshake result into a failing hop and a likely cause.

    The caller performs the real handshake (initialize / notifications/initialized /
    tools/list) against ``MCP_TUNNEL_URL`` + ``/mcp`` and hands in the first response's
    ``status`` and the elapsed seconds, plus the ``login_state`` decided from the token file.
    This function names the hop; it does not touch the network or the CLI.

    Inputs:

      * ``status``      -- HTTP status of the public initialize, or the string ``"ERR"`` /
                           ``None`` when the request never completed (timeout / refused).
      * ``elapsed_s``   -- seconds the request took. Compared against ``slow_s``.
      * ``login_state`` -- the ``decide_login_state`` verdict, so a 404 can be attributed to
                           a missing login (``LOGIN_NONE``) rather than guessed at.
      * ``slow_s``      -- the slowness ceiling (default 30 s). Non-numeric / non-positive
                           disables the slow check.

    Returns ``(hop, cause)``: ``hop`` is one of the ``HOP_*`` names and ``cause`` is a short
    fixed phrase for the alert. ``HOP_OK`` means a fast 200 -- the only healthy shape.
    """
    # Did it even complete?
    completed = status not in (None, "ERR")
    code = None
    if completed:
        try:
            code = int(status)
        except (TypeError, ValueError):
            code = None

    # A 404 from the relay is the measured no-host shape: the public endpoint answers but the
    # tunnel has no host behind it. If the token file also says logged-out, name that as the
    # cause, because that is the one measured root cause of a 404 here.
    if code == 404:
        if login_state == LOGIN_NONE:
            return HOP_PUBLIC_NO_HOST, "devtunnel is not logged in, so the relay has no host"
        return HOP_PUBLIC_NO_HOST, "public relay has no host behind it (login looks present; host down or reconnecting)"

    # 401/403: the handshake reached a server that rejected the bearer token.
    if code in (401, 403):
        return HOP_AUTH, "public endpoint rejected the bearer token (auth)"

    # Never completed: timeout or connection refused at the public hop. If logged out, that is
    # the most likely reason; otherwise the relay/host is unreachable.
    if not completed:
        if login_state == LOGIN_NONE:
            return HOP_PUBLIC_NO_HOST, "public handshake did not complete and devtunnel is not logged in"
        return HOP_PUBLIC_NO_HOST, "public handshake did not complete (timeout/refused) -- relay or host unreachable"

    # Completed with some non-200, non-404, non-auth status: a real but unhealthy relay answer.
    if code != 200:
        return HOP_PUBLIC_OTHER, "public endpoint returned HTTP %s" % code

    # A 200 that was too slow is still a failure (the logged-out relay's 404 took 50-60 s; a
    # live gateway must answer far faster).
    try:
        ceiling = float(slow_s)
    except (TypeError, ValueError):
        ceiling = 0.0
    if ceiling > 0 and elapsed_s is not None:
        try:
            if float(elapsed_s) > ceiling:
                return HOP_PUBLIC_SLOW, "public handshake returned 200 but took longer than %gs" % ceiling
        except (TypeError, ValueError):
            pass
    return HOP_OK, ""


def classify_local_hop(status, elapsed_s, slow_s=SLOW_SECONDS):
    """Classify the LOCAL (127.0.0.1:8000) MCP handshake result.

    The local hop has no tunnel and no login in front of it, so there are only three shapes:
    a fast 200 (ok), a reject (auth), or anything else -- which means main.py is down, wedged
    or answering too slowly. Returns ``(hop, cause)`` from the ``HOP_*`` names.
    """
    completed = status not in (None, "ERR")
    code = None
    if completed:
        try:
            code = int(status)
        except (TypeError, ValueError):
            code = None
    if code in (401, 403):
        return HOP_AUTH, "local server rejected the bearer token (auth)"
    if not completed:
        return HOP_LOCAL_DOWN, "local server did not answer (down or wedged)"
    if code != 200:
        return HOP_LOCAL_DOWN, "local server returned HTTP %s (down or wedged)" % code
    try:
        ceiling = float(slow_s)
    except (TypeError, ValueError):
        ceiling = 0.0
    if ceiling > 0 and elapsed_s is not None:
        try:
            if float(elapsed_s) > ceiling:
                return HOP_LOCAL_SLOW, "local server returned 200 but took longer than %gs" % ceiling
        except (TypeError, ValueError):
            pass
    return HOP_OK, ""


def main(argv=None):
    """Self-check the pure deciders on the MEASURED facts and print the verdicts, one JSON
    line per case. No network, no filesystem, no clock, no devtunnel CLI -- it feeds the
    functions the 2026-11-11 measurements (4182-byte valid token / 0-byte logged-out token;
    the public relay answering 404 after 50-60 s while logged out; a fast healthy 200) and
    prints what decide_login_state / classify_public_hop / classify_local_hop return, so the
    contract can be eyeballed from the command line without standing anything up.

    Returns 0 when the self-check matches the measured expectations, 1 otherwise, so it can
    double as a smoke test in a shell.
    """
    import json
    import sys

    login_out = decide_login_state(0)                 # 0 bytes -> logged out
    login_in = decide_login_state(4182)               # 4182 bytes -> logged in
    pub_no_host = classify_public_hop(404, 55.0, login_out)   # measured no-host shape
    pub_ok = classify_public_hop(200, 1.2, login_in)          # fast healthy 200
    local_down = classify_local_hop("ERR", 90.0)              # local main.py not answering
    local_ok = classify_local_hop(200, 0.5)                   # fast healthy 200

    cases = [
        ("login_logged_out", login_out, LOGIN_NONE),
        ("login_logged_in", login_in, LOGIN_VALID),
        ("public_no_host", pub_no_host[0], HOP_PUBLIC_NO_HOST),
        ("public_ok", pub_ok[0], HOP_OK),
        ("local_down", local_down[0], HOP_LOCAL_DOWN),
        ("local_ok", local_ok[0], HOP_OK),
    ]
    ok = True
    for name, got, want in cases:
        passed = got == want
        ok = ok and passed
        print(json.dumps({"case": name, "got": got, "want": want, "pass": passed},
                         ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())

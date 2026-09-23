"""bridge_auth.py -- the per-start token that every bridge request has to carry.

WHY THIS EXISTS. The bridge (bridge/copilot_bridge.py) listens on 127.0.0.1 and drives a
signed-in Copilot session. Until 2026-09-24 every endpoint -- /stream, /goal, /send, /delete,
/forget, /upload, ... -- was a plain GET with no check of any kind. Loopback is not a trust
boundary: any web page open in any browser on this machine could make the browser issue
`GET http://127.0.0.1:8765/goal?text=...` (an <img> tag is enough), and so could any other
process, whatever it was. That is a signed-in Copilot session steered by whoever asks.

THE MODEL, stated once:

  * The bridge mints a random token at every start (``install_token``) and writes it to a file
    only the current Windows user can read (explicit owner-only ACL, verified by reading
    ``icacls`` back; mode 0600 elsewhere). Whoever can read that file is the user.
  * Every request that changes state or reads the page must carry it in ``X-Bridge-Token``. A
    custom header is not a "simple" header, so a browser cannot attach it cross-origin without a
    CORS preflight -- and the bridge never answers a preflight positively.
  * State changes are POST only. The request body is ``application/x-www-form-urlencoded`` with
    the same fields the old query strings carried, so the handlers did not change.
  * Anything carrying ``Origin``, ``Referer`` or a ``Sec-Fetch-Site`` other than
    none/same-origin is refused outright: native clients send none of those.

A BRIDGE RESTART ROTATES THE TOKEN. Clients therefore re-read the file once on a 401 before
giving up (``request`` below; ui/BridgeClient.cs does the same), so a long-running window is not
stranded by a restart.

STDLIB ONLY, because bridge/session_cli.py imports this on the ubuntu CI box.
"""
from __future__ import annotations

import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

#: The header. Not a "simple" request header, which is the point: see the module docstring.
TOKEN_HEADER = "X-Bridge-Token"

#: Moves the token directory. Tests point it at a temp directory (conftest does so for every
#: run) so no test can ever overwrite the live bridge's token.
TOKEN_DIR_ENV = "MCP_BRIDGE_TOKEN_DIR"

#: How much of a refused reply to quote in an error message.
_QUOTE = 300


class BridgeAuthError(RuntimeError):
    """The bridge could not be asked, for a reason the person has to act on (no token file,
    token refused, a bridge older than this client). The message says which, and what to do."""


class TokenFileError(RuntimeError):
    """The token file could not be written owner-only. The bridge refuses to serve rather than
    publish a token anybody on the machine could read."""


# ── where the token lives ──────────────────────────────────────────────────────────────────────

def token_dir() -> str:
    """Per-user, outside any checkout: two checkouts of this repository talk to the same bridge
    on the same port, so the token cannot live in either one's .fleet."""
    override = os.environ.get(TOKEN_DIR_ENV, "").strip()
    if override:
        return override
    root = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(root, "m365-copilot-companion", "bridge")


def token_path(port: int) -> str:
    """One file per port: start_bridge.ps1 can run a second bridge on another port."""
    return os.path.join(token_dir(), "token-%d" % int(port))


def port_of(base_url: str) -> int:
    parsed = urllib.parse.urlparse(base_url)
    return parsed.port or (443 if parsed.scheme == "https" else 80)


def read_token(port: int) -> "str | None":
    """The current token for ``port``, or None when there is no readable, non-empty file."""
    try:
        with open(token_path(port), "r", encoding="ascii") as fh:
            tok = fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return tok or None


# ── writing it (the bridge, at startup) ───────────────────────────────────────────────────────

def _current_user():
    """(DOMAIN\\name, SID) of this process's user, from whoami. Windows only."""
    from tools import childproc
    r = childproc.run(["whoami", "/user", "/fo", "csv", "/nh"], timeout=30,
                      creationflags=childproc.headless_creationflags())
    line = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not line:
        raise TokenFileError("whoami /user failed (rc=%s): %s" % (r.returncode, r.stderr.strip()))
    parts = [p.strip().strip('"') for p in line[0].split('","')]
    if len(parts) != 2 or not parts[1].startswith("S-1-"):
        raise TokenFileError("could not read the current user's SID from whoami: %r" % line[0])
    return parts[0], parts[1]


def _icacls(args):
    from tools import childproc
    return childproc.run(["icacls"] + list(args), timeout=60,
                         creationflags=childproc.headless_creationflags())


def acl_entries(path: str):
    """The principals icacls lists for ``path``, as (principal, rights) pairs. Windows only.

    icacls prints the path, then one ACE per line (continuation lines indented to the path's
    width), then a blank line and a "Successfully processed" summary.
    """
    r = _icacls([path])
    if r.returncode != 0:
        raise TokenFileError("icacls could not read %s (rc=%s): %s%s"
                             % (path, r.returncode, r.stdout, r.stderr))
    entries = []
    for i, raw in enumerate((r.stdout or "").splitlines()):
        line = raw.strip()
        if not line or line.lower().startswith("successfully processed"):
            continue
        if i == 0 and line.lower().startswith(path.lower()):
            line = line[len(path):].strip()
        # An ACE is "principal:(rights)". The summary line is localized (Japanese here), so it
        # is recognised by NOT having that shape rather than by its wording.
        principal, sep, rights = line.partition(":(")
        if not sep or not principal.strip():
            continue
        entries.append((principal.strip(), "(" + rights.strip()))
    return entries, r.stdout


def _restrict_to_owner(path: str) -> str:
    """Give ``path`` an explicit ACL naming the current user alone, then read it back.
    Returns icacls' own listing, which is the evidence. Raises TokenFileError otherwise."""
    if os.name != "nt":
        os.chmod(path, 0o600)
        mode = os.stat(path).st_mode & 0o777
        if mode != 0o600:
            raise TokenFileError("%s is mode %o, not 600" % (path, mode))
        return "mode %o" % mode
    name, sid = _current_user()
    r = _icacls([path, "/inheritance:r", "/grant:r", "*%s:(F)" % sid])
    if r.returncode != 0:
        raise TokenFileError("icacls could not restrict %s (rc=%s): %s%s"
                             % (path, r.returncode, r.stdout, r.stderr))
    entries, listing = acl_entries(path)
    others = [p for p, _ in entries if p.lower() not in (name.lower(), "*" + sid.lower(),
                                                           sid.lower())]
    if not entries or others:
        raise TokenFileError("%s is not owner-only after icacls; it lists %r"
                             % (path, [p for p, _ in entries]))
    return listing


def install_token(port: int, token: "str | None" = None) -> "tuple[str, str]":
    """Mint (or take) a token, write it owner-only for ``port``, and return (token, evidence).

    The ACL is set on an EMPTY temporary file before the secret is written into it, and the file
    is then renamed over the old one -- so there is no moment at which the token sits in a file
    with the directory's inherited ACL.
    """
    token = token or secrets.token_urlsafe(32)
    d = token_dir()
    os.makedirs(d, exist_ok=True)
    final = token_path(port)
    tmp = "%s.%d.tmp" % (final, os.getpid())
    try:
        os.remove(tmp)
    except OSError:
        pass
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        evidence = _restrict_to_owner(tmp)
        with open(tmp, "w", encoding="ascii", newline="") as fh:
            fh.write(token)
        os.replace(tmp, final)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    if read_token(port) != token:
        raise TokenFileError("the token file %s did not read back" % final)
    return token, evidence


# ── asking the bridge (every Python client) ───────────────────────────────────────────────────

def _no_token_message(base_url: str) -> str:
    return ("no bridge token at %s. Either the bridge on %s is not running, or it is older than "
            "this client (an old bridge writes no token and accepts only GET). Restart the "
            "bridge: stop the python process running bridge/copilot_bridge.py and let the "
            "supervisor bring it back, or run start_all.bat."
            % (token_path(port_of(base_url)), base_url))


def describe_refusal(base_url: str, path: str, code: int, reason: str, body: str) -> str:
    """One sentence a person can act on, for a refused bridge request."""
    head = "bridge %s refused %s (HTTP %s %s)" % (base_url, path, code, reason or "")
    if code == 501 or code == 405:
        return (head + ": the bridge is older than this client -- it does not accept "
                "authenticated POST requests. Restart the bridge (stop the python process "
                "running bridge/copilot_bridge.py; the supervisor restarts it with the current "
                "code).")
    if code == 401:
        return (head + ": the token in %s was not accepted even after re-reading it. The bridge "
                "may have restarted and failed to write its token -- check .setup/logs/"
                "bridge.log." % token_path(port_of(base_url)))
    return head + (": " + body[:_QUOTE] if body else "")


def request(base_url: str, path: str, params: "dict | None" = None, *, timeout=None,
            method: str = "POST"):
    """Open one authenticated request to the bridge and return the response (a context manager,
    as urllib.request.urlopen returns). ``params`` travel as a form body for POST and as the
    query string for GET.

    Re-reads the token file once on a 401, so a bridge restart does not strand a long-running
    caller. Raises BridgeAuthError for a missing token or a refusal a person has to act on;
    connection failures propagate unchanged, so "is it up?" callers can still tell.
    """
    port = port_of(base_url)
    data = urllib.parse.urlencode(params or {}, doseq=True)
    url = base_url + path
    body = None
    if method == "GET":
        if data:
            url += "?" + data
    else:
        body = data.encode("ascii")
    # NO PROXY. A corporate HTTP_PROXY would otherwise receive loopback requests -- and with
    # them this token.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for attempt in (0, 1):
        tok = read_token(port)
        if not tok:
            raise BridgeAuthError(_no_token_message(base_url))
        if attempt == 1 and tok == last:
            break            # nothing new to try; report the 401 below
        last = tok
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header(TOKEN_HEADER, tok)
        if body is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            return opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            text = ""
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            if exc.code == 401 and attempt == 0:
                continue
            raise BridgeAuthError(describe_refusal(base_url, path, exc.code, exc.reason, text))
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            # AN OLD BRIDGE OFTEN ANSWERS WITH A RESET, NOT A 501. It never reads a POST body,
            # and closing a socket with unread bytes makes Windows send RST -- so the client sees
            # "connection reset" and the 501 that would have explained it is lost (measured:
            # intermittent in this repository's own test run). Ask /status, which every bridge
            # serves to a GET: one that does not report `authenticated` predates the token.
            if is_old_bridge(base_url):
                raise BridgeAuthError(describe_refusal(base_url, path, 501, "(connection reset)",
                                                       "")) from exc
            raise
    raise BridgeAuthError(describe_refusal(base_url, path, 401, "Unauthorized", ""))


def is_old_bridge(base_url: str) -> bool:
    """True when something answers GET /status as a bridge that predates the token (its JSON
    has no `authenticated` field). False when nothing answers, or a current bridge does."""
    import json
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base_url + "/status", timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return False
    return isinstance(body, dict) and "ok" in body and "authenticated" not in body


def main(argv=None):
    """``python -m bridge.bridge_auth <port>`` prints the token file and its ACL -- evidence for
    a person checking the file is owner-only. It never prints the token."""
    argv = list(sys.argv[1:] if argv is None else argv)
    port = int(argv[0]) if argv else int(os.environ.get("MCP_BRIDGE_PORT", "8765"))
    p = token_path(port)
    print("token file:", p, "(present)" if os.path.isfile(p) else "(absent)")
    if os.path.isfile(p) and os.name == "nt":
        print(acl_entries(p)[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())

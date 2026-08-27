"""What the last capture established, written where a window can read it.

WHY THIS FILE EXISTS. The cockpit's health strip judged sign-in and agent binding from the
BROWSER TAB LIST, because when it was written every worker drove a tab. Work now runs over a
websocket and a page is opened only to read a token -- about once per token lifetime, for four
seconds, and not at all in between. So the tab list is empty during normal operation, and both
dots read the emptiness:

    sign-in   `needsSignin = onLoginWall && !hasUsableM365Chat`  -- with no tabs at all this is
              false, so the dot is GREEN. It is green because there is nothing there, not
              because sign-in works, and it would be equally green with an expired sign-in.
              A check that reports health from an absence is a check that fails open.

    agent     `if (RunIsLive() && !hasUsableM365Chat) RED` -- during a socket run there is never
              a chat tab, so this is a guaranteed false red for the whole run.

THE RULE THIS RESTORES. Grey means there is no evidence and none is expected. Green means fresh
POSITIVE evidence. Evidence that is expected and missing is neither -- it is amber, then red.

WHAT IS WRITTEN, AND WHAT IS NOT. The token's expiry and audience, the agent the template names,
when the capture happened, and whether it succeeded. Never the token. An expiry timestamp and
an audience string are metadata: alone they grant nothing.

WHY NOT IN profile_token.py, WHERE THE CAPTURE IS. That module's docstring promises in as many
words that the credential never leaves the process and that nothing there logs, writes or
records it. Writing derived scalars beside that promise invites a later reader to widen it. The
capture_floor seam already wraps every capture implementation -- it was built as the one point
every capture passes through without touching the frozen socket_route -- so the fact is
recorded there, and what may be recorded is enumerated here rather than left to judgement.
"""
from __future__ import annotations

import base64
import json
import os
import time

STATUS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           ".fleet", "capture_status.json")

#: The ONLY fields this module will ever write. A credential is not among them, and a list is
#: kept rather than a habit so that adding one is a visible edit.
FIELDS = ("ok", "at", "expires_at", "audience", "gpt_id", "agent_url_hash", "reason", "kind")

#: Why a capture failed, in the only terms the strip needs: a person must log in, or something
#: else went wrong. The distinction decides red against amber, so it is named rather than
#: inferred from a message at display time.
SIGNIN = "signin"
OTHER = "other"

#: Text that means the browser is sitting on a login wall rather than on the application. Kept
#: narrow: a capture fails for many reasons and only this one is answered by a human logging in.
_SIGNIN_MARKS = ("login.microsoftonline.com", "sign in", "signin", "サインイン",
                 "not authenticated", "unauthor", "401", "aadsts")


def _claims(token):
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(body))
    except Exception:
        return {}


def classify_failure(exc) -> str:
    """SIGNIN when a person has to log in, OTHER for everything else.

    Deliberately conservative: a capture that failed for an unclear reason is OTHER, which the
    strip shows as amber. Calling an unknown failure a sign-in problem sends somebody to
    re-authenticate for a network blip, and after the second time they stop believing the dot.
    """
    text = ("%s: %s" % (type(exc).__name__, exc)).lower()
    return SIGNIN if any(m in text for m in _SIGNIN_MARKS) else OTHER


def record_success(token, template, agent_url="", path=None):
    """Write what a successful capture establishes. Never raises, never writes the token."""
    claims = _claims(token or "")
    _write({"ok": True,
            "at": time.time(),
            "expires_at": float(claims.get("exp") or 0),
            "audience": str(claims.get("aud") or ""),
            "gpt_id": str(getattr(template, "gpt_id", "") or ""),
            "agent_url_hash": _hash(agent_url),
            "reason": "",
            "kind": ""}, path)


def record_failure(exc, agent_url="", path=None):
    """Write that a capture failed, and whether a person has to do something about it."""
    _write({"ok": False,
            "at": time.time(),
            "expires_at": 0.0,
            "audience": "",
            "gpt_id": "",
            "agent_url_hash": _hash(agent_url),
            "reason": ("%s: %s" % (type(exc).__name__, exc))[:200],
            "kind": classify_failure(exc)}, path)


def _hash(agent_url):
    import hashlib
    return hashlib.sha256((agent_url or "").encode("utf-8")).hexdigest()[:16]


def _write(record, path=None):
    target = path or STATUS_PATH
    payload = {k: record.get(k) for k in FIELDS}
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, target)          # a half-written status must never be readable
    except Exception:
        # EXCEPTION, NOT OSError. The line below promises a status write can never take
        # down a capture, and OSError does not cover everything a path can raise -- a
        # null byte in one gives ValueError, and the promise was not kept. A capture is
        # the route's only way to exist; a file for a window is not worth risking it.
        pass


def read(path=None):
    """The last recorded capture, or None. A missing file is not a failure."""
    try:
        with open(path or STATUS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None

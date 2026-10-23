"""A token that outlives the coordinator that captured it.

WHY THIS EXISTS. Every coordinator (relay/fleet_runner.py) is a new process, and everything the
route holds -- the token, the capture floor, profile_token's `_MEMO` -- is process memory. The
capture itself runs in a helper process, so `_MEMO` was never shared even inside one run. So a
coordinator that starts on an idle fleet has no token, opens a page to get one, and its goals
wait for that ("waiting for token") although the token the previous coordinator captured is
good for 60-85 minutes. Measured 2026-10-23 18:19-20:00 (soak2, 23 coordinators, one per few
minutes): the page took 12-24 s each time, and the three that failed with NoLightToken kept the
goals waiting 181 s, 84 s and 36 s.

WHAT IS KEPT. The access token, encrypted with Windows DPAPI under the current user
(tools/secret_store.py, the same protection the .env secrets use), plus plain METADATA: the
agent surface it was captured for (hash), the audience, the expiry, and a fingerprint of the
account it was issued to (hash of tenant + object id). The token is never written in clear text:
on a platform without DPAPI `save` refuses and nothing is written.

WHEN IT IS REUSED. Only when every one of these holds, otherwise it is discarded and the
ordinary capture runs:

  * the file is for THIS agent surface (the name is a hash of the surface, and the hash is
    stored inside and compared);
  * DPAPI decrypts it, which means this Windows user on this machine wrote it;
  * the token's own `aud` is the sydney audience, and its own `exp` has at least
    profile_token.MIN_LIFE_S left -- the bar a freshly captured token has to clear too;
  * the account fingerprint stored beside it matches the one computed from the token itself.

NEVER WORSE THAN A CAPTURE. A reused token is installed in the route like a captured one, so
the route still sees `needs_refresh` as soon as less than its refresh margin is left and goes
for a fresh one in the background. A token the backend refuses is dropped (`discard`), and so is
everything after a browser reset (relay_fleet.reset_socket_route).

THE ACCOUNT. The browser can be signed in as someone else between two runs. The stored
fingerprint cannot detect that on its own -- only a capture can see it -- so a capture whose
account differs REPLACES the file whatever its expiry, and the log says so. What remains is the
window between the switch and the next capture, bounded by the token's own life; the in-process
route has always had the same window.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time

#: One file per agent surface, beside the request templates. `.fleet/` is gitignored.
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         ".fleet", "token_cache")

VERSION = 1


def _claims(token: str) -> dict:
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(body))
    except Exception:
        return {}


def who_of(token: str) -> str:
    """A fingerprint of the account a token was issued to: a hash of tenant and object id.

    "" when the token names neither, which makes it unusable here -- a token that cannot be
    attributed to an account is not one to hand to the next process."""
    c = _claims(token or "")
    tid = str(c.get("tid") or "")
    oid = str(c.get("oid") or c.get("sub") or "")
    if not tid or not oid:
        return ""
    return hashlib.sha256(("%s|%s" % (tid, oid)).encode("utf-8")).hexdigest()[:16]


def _agent_hash(agent_url: str) -> str:
    return hashlib.sha256((agent_url or "").encode("utf-8")).hexdigest()[:16]


def file_for(agent_url: str, directory=None) -> str:
    return os.path.join(directory or CACHE_DIR, "token_%s.json" % _agent_hash(agent_url))


def _default_protect(value: str) -> str:
    from tools.secret_store import protect_secret
    return protect_secret(value)


def _default_unprotect(value: str) -> str:
    from tools.secret_store import unprotect_secret
    return unprotect_secret(value)


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def discard(agent_url: str, directory=None) -> None:
    try:
        os.remove(file_for(agent_url, directory))
    except OSError:
        pass


def discard_all(directory=None) -> int:
    """Forget every cached token. For a browser that was reset, and for a backend that refused
    the token: both mean the cached value cannot be trusted any more."""
    gone = 0
    d = directory or CACHE_DIR
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    for name in names:
        if name.startswith("token_") and name.endswith(".json"):
            try:
                os.remove(os.path.join(d, name))
                gone += 1
            except OSError:
                pass
    return gone


def save(token: str, agent_url: str, *, directory=None, protect=None, now=None,
         audience=None, min_life_s=None, log=None) -> bool:
    """Persist a captured token for the next process. Never raises; False means nothing written.

    Refuses a token for the wrong audience or with too little life (it would not be reused),
    one that names no account, and any platform where it cannot be encrypted. Keeps the file it
    finds when that holds a LATER expiry for the same account, so two coordinators finishing
    close together cannot make the cache go backwards."""
    from relay import profile_token as PT
    say = log or (lambda m: None)
    now = time.time() if now is None else float(now)
    audience = PT.AUDIENCE if audience is None else audience
    min_life = PT.MIN_LIFE_S if min_life_s is None else float(min_life_s)
    try:
        if not PT.token_is_usable(token, audience=audience, now=now, min_life_s=min_life):
            return False
        who = who_of(token)
        if not who:
            return False
        claims = _claims(token)
        exp = float(claims.get("exp") or 0)
        path = file_for(agent_url, directory)
        old = _read(path)
        if old and old.get("who") == who and float(old.get("exp") or 0) >= exp:
            return True
        if old and old.get("who") and old.get("who") != who:
            say("[token_cache] the account behind the browser changed since the cached token "
                "was captured: replacing it")
        blob = (protect or _default_protect)(token)
        if not isinstance(blob, str) or not blob or token in blob:
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        record = {"v": VERSION, "agent_hash": _agent_hash(agent_url), "aud": audience,
                  "exp": exp, "who": who, "saved_at": now, "blob": blob}
        tmp = path + ".tmp.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as fh:
            # `blob` is DPAPI ciphertext (protect_secret), never the token; save() returned
            # above unless the ciphertext differs from it.
            # codeql[py/clear-text-storage-sensitive-data]
            json.dump(record, fh)
        os.replace(tmp, path)          # a half-written cache must never be readable
        return True
    except Exception:
        return False


def load(agent_url: str, *, directory=None, unprotect=None, now=None, audience=None,
         min_life_s=None, log=None) -> str:
    """The cached token for this agent surface, or "" when there is nothing safe to reuse.

    Every failed check discards the file: expiry only moves one way, and a file that does not
    decrypt, names another agent or another account can never become valid by waiting."""
    from relay import profile_token as PT
    say = log or (lambda m: None)
    now = time.time() if now is None else float(now)
    audience = PT.AUDIENCE if audience is None else audience
    min_life = PT.MIN_LIFE_S if min_life_s is None else float(min_life_s)
    path = file_for(agent_url, directory)
    rec = _read(path)
    if rec is None:
        return ""
    why = ""
    token = ""
    try:
        if rec.get("v") != VERSION or rec.get("agent_hash") != _agent_hash(agent_url):
            why = "it is for another agent surface or an unknown format"
        else:
            token = (unprotect or _default_unprotect)(str(rec.get("blob") or ""))
            who = who_of(token)
            if not who or who != rec.get("who"):
                why = "the account fingerprint does not match the token"
            elif not PT.token_is_usable(token, audience=audience, now=now, min_life_s=min_life):
                why = "the token has expired or has too little life left"
            elif abs(float(_claims(token).get("exp") or 0) - float(rec.get("exp") or 0)) > 1.0:
                why = "the stored expiry does not match the token"
    except Exception as exc:
        why = "it could not be decrypted (%s)" % type(exc).__name__
    if why:
        say("[token_cache] discarded the cached token: %s" % why)
        discard(agent_url, directory)
        return ""
    return token


def _main(argv=None) -> int:
    """`python -m relay.token_cache clear` forgets every cached token."""
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["clear"]:
        print("removed %d cached token file(s)" % discard_all())
        return 0
    print("usage: python -m relay.token_cache clear")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())

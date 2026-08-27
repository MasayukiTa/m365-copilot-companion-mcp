"""Get the token without running a turn, and stop re-deriving a template that has not changed.

WHAT THE CAPTURE PAGE IS FOR, AND WHY IT COSTS WHAT IT COSTS. The socket route is built from a
tab: a page is opened on the agent surface, ONE REAL TURN is run on it, and two things are read
out of the websocket the client opens -- the access token from the URL, and the request
template from the chat frame. Sampled every second throughout, the browser rises about 610 MB
while that page is open and stays there for some thirty-five seconds.

THE TURN IS FOR THE TEMPLATE, NOT FOR THE TOKEN. Measured 2026-08-27, on the same agent
surface: a token for substrate.office.com/sydney appears in an outgoing Authorization header
4.3 seconds after navigation, with no message sent and no answer awaited. The other thirty
seconds are the turn, and the turn exists only because the frame it sends is where the
template comes from.

SO TWO LIFETIMES WERE BOUND TOGETHER THAT SHOULD NOT HAVE BEEN. `_refresh_locked` asks
capture_fn for a token AND a template and stores both, so every token refresh re-derives a
template that had not changed. The token lives 15 to 79 minutes. The template -- the flag list
and the frame shape -- changes when Microsoft ships a feature flag, which is not hourly.

    tier 1   no page at all      the route still holds a live token and a cached template
    tier 2   a page, no turn     navigate, read the header, close             about 5s
    tier 3   a page and a turn   what happens today, and the only way        about 35s
                                 to derive a template in the first place

WHAT THIS DOES NOT DO. It does not read MSAL's cache. That cache is on the origin in
localStorage and its key names are readable from any document there -- a favicon reads 23
entries in under a second -- but the VALUES are encrypted at rest: `data` and `nonce`, AES-GCM.
Decrypting them would mean reimplementing Microsoft's key derivation, which is not reading what
the browser stored, it is defeating the protection the browser applied to it. A token observed
in flight, leaving our own browser, is what the capture has always read; a token prised out of
encrypted storage is a different act, and this module does not perform it.

Nor does it present a client id to Entra. No device code, no borrowed application identity, no
first-party client asserted by us. The page signs in exactly as it always did, and this reads
what the page then sends.

THE CREDENTIAL NEVER LEAVES THIS PROCESS. It is handed to the route, which holds it in memory
exactly as the tab capture's token was held. Nothing here logs it, writes it, or records it,
and the cached template is a request SHAPE: RequestTemplate strips access_token on the way in,
and the per-turn keys are dropped again before anything reaches disk.
"""
from __future__ import annotations

import base64
import json
import os
import time

#: The resource the socket route needs. Read from a real capture's own `aud` claim, so it is
#: observed rather than chosen.
AUDIENCE = os.environ.get("MCP_PROFILE_TOKEN_AUDIENCE", "https://substrate.office.com/sydney")

#: How long to wait for the page to present a token before giving up and letting the caller
#: fall through to a full capture. Measured at 4.3s on the agent surface; 30 is seven times
#: that and still well under the 35 seconds a turn costs.
LIGHT_TIMEOUT_S = float(os.environ.get("MCP_LIGHT_CAPTURE_TIMEOUT_S", "30"))

#: A token with less than this left is not worth returning: the route's own refresh margin is
#: ten minutes, so anything shorter would send it straight back for another one.
MIN_LIFE_S = float(os.environ.get("MCP_PROFILE_TOKEN_MIN_LIFE_S", "660"))


class NoLightToken(RuntimeError):
    """The light path produced no usable token. The caller runs a full capture, as it always did."""


def _claims(token: str) -> dict:
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(body))
    except Exception:
        return {}


def token_is_usable(token: str, *, audience=AUDIENCE, now=None, min_life_s=MIN_LIFE_S) -> bool:
    """Right resource, and enough life left to be worth having.

    THE AUDIENCE IS CHECKED, NOT ASSUMED. A single page makes Authorization-bearing calls to
    several resources -- graph, loki, titles, arc were all observed on this one -- and taking
    the first Bearer it emits would hand the route a token for the wrong backend, which fails a
    real turn before anybody finds out.
    """
    claims = _claims(token)
    if audience.lower() not in str(claims.get("aud") or "").lower():
        return False
    try:
        left = float(claims.get("exp") or 0) - (time.time() if now is None else now)
    except (TypeError, ValueError):
        return False
    return left >= min_life_s


def token_life_s(token: str, now=None) -> float:
    """Seconds of life left, or 0 for anything unreadable."""
    try:
        return float(_claims(token).get("exp") or 0) - (time.time() if now is None else now)
    except (TypeError, ValueError):
        return 0.0


def token_via_light_page(context, url, *, audience=AUDIENCE, timeout_s=None,
                         min_life_s=MIN_LIFE_S, log=None):
    """Navigate, watch our own outgoing requests for a token, close. No message is sent.

    Raises NoLightToken rather than returning something empty: the caller's fallback is the
    full capture, which is a working path, and a route handed a wrong or expiring token fails
    a real turn before it finds out.
    """
    say = log or _say
    budget = LIGHT_TIMEOUT_S if timeout_s is None else float(timeout_s)
    found = {"token": ""}

    def on_request(req):
        if found["token"]:
            return
        try:
            auth = (req.headers or {}).get("authorization", "")
        except Exception:
            return
        if not auth.lower().startswith("bearer "):
            return
        candidate = auth.split(None, 1)[1]
        if token_is_usable(candidate, audience=audience, min_life_s=min_life_s):
            found["token"] = candidate

    page = context.new_page()
    _claim(page, "light token")
    started = time.time()
    try:
        page.on("request", on_request)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            # A slow navigation is not a failure here. The token rides on requests the page
            # makes, and those begin well before the load event settles.
            pass
        while not found["token"] and time.time() - started < budget:
            page.wait_for_timeout(250)
    finally:
        _release(page)
    if not found["token"]:
        raise NoLightToken(
            "no %s token with %.0fs of life seen in %.0fs of page traffic"
            % (audience, min_life_s, time.time() - started))
    say("[profile_token] token in %.1fs with no turn sent (%.0f min of life)"
        % (time.time() - started, token_life_s(found["token"]) / 60.0))
    return found["token"]


def _claim(page, note):
    """Record ownership before anything else can fail, so this page is never an orphan."""
    try:
        from relay.relay_fleet import _claim_page
        _claim_page(page, note=note)
    except Exception:
        pass


def _release(page):
    """Release and close on every exit path -- success, timeout, cancellation, crash."""
    try:
        from relay.ownership import release
        from relay.relay_fleet import _page_target_id
        tid = _page_target_id(page)
        if tid:
            release("page", tid, run_id=str(os.getpid()))
    except Exception:
        pass
    try:
        page.close()
    except Exception:
        pass


# ---- the template, whose lifetime is not the token's ---------------------------------------
#
# A cached template is not a frozen one. It is written from a real capture, reused while it
# works, and discarded the moment the backend refuses a request built from it -- which is the
# only evidence that matters about whether it is still right.

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            ".fleet", "templates")

#: How old a cached template may be before it is re-derived anyway. A backstop, not the
#: mechanism: the real signal is the backend refusing a request, and this only covers a drift
#: that changes an answer without ever producing an error.
TEMPLATE_MAX_AGE_S = float(os.environ.get("MCP_TEMPLATE_MAX_AGE_S", str(24 * 3600)))


def template_path(agent_url: str, directory=None) -> str:
    """One file per agent surface. PER AGENT, because the template names WHICH agent -- gptId
    is in both the query and the frame -- so one cache shared across surfaces would send every
    goal to whichever agent happened to be captured last."""
    import hashlib
    key = hashlib.sha256((agent_url or "").encode("utf-8")).hexdigest()[:16]
    return os.path.join(directory or TEMPLATE_DIR, "template_%s.json" % key)


def save_template(template, agent_url: str, directory=None) -> bool:
    """Persist a captured template. NEVER persists a credential: RequestTemplate strips
    access_token on the way in, and the per-turn keys are dropped here as well, so what lands
    on disk is a request shape and an agent id."""
    path = template_path(agent_url, directory)
    try:
        from relay.chathub import RequestTemplate
        query = {k: v for k, v in (template.query or {}).items()
                 if k not in set(RequestTemplate.VOLATILE_QUERY)}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ts": time.time(),
                       "query": query, "frame": template.frame}, fh, ensure_ascii=False)
        os.replace(tmp, path)               # a half-written cache must never be readable
        return True
    except Exception:
        return False


def load_template(agent_url: str, directory=None, now=None, max_age_s=None):
    """The cached template for this agent, or None. A cache that cannot be read is not an
    error -- it means the next capture is an ordinary one, which is where this started."""
    path = template_path(agent_url, directory)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    age_cap = TEMPLATE_MAX_AGE_S if max_age_s is None else float(max_age_s)
    if age_cap > 0:
        age = (time.time() if now is None else now) - float(data.get("ts") or 0)
        if age > age_cap:
            return None
    try:
        from relay.chathub import RequestTemplate
        template = RequestTemplate(data.get("query") or {}, data.get("frame") or {})
    except Exception:
        return None
    # A TEMPLATE WITHOUT AN AGENT IS NOT A CACHE HIT. It is the one thing the tab capture
    # refuses to return, because a request naming no agent reaches the default Copilot -- no
    # connectors, no tenant grounding -- and answers fluently while the route reports success.
    return template if template.gpt_id else None


def discard_template(agent_url: str, directory=None) -> None:
    """Forget this agent's cached template so the next capture re-derives it from a real turn."""
    try:
        os.remove(template_path(agent_url, directory))
    except OSError:
        pass


# ---- tier 1: answer without touching the browser at all --------------------------------------
#
# THE ROUTE CAN ASK FOR EVER, AND HAS. Its refresh margin is 1500 seconds, set to cover a turn
# that may run 600 -- sound reasoning, and it silently assumes the token outlives the margin.
# The tokens this tenant hands out do not: measured across twelve runs they range from 9 to 81
# minutes, and whenever one comes back under 25 minutes `needs_refresh` is true the instant the
# capture that was meant to satisfy it finishes. The route then captures again. And again.
#
#     run                captures   minutes   token min
#     20260827_171924          16       4.2      10-81     3.8 captures a minute
#     20260826_210007          22      16.6       9-25     1.3 captures a minute
#
# At 3.8 a minute, with each capture holding a 610 MB page for thirty-five seconds, the browser
# is never WITHOUT one. That is the memory pressure -- not "once per token lifetime", which was
# a measurement of how long a token lasts read as though it were a measurement of how often a
# page opens. The two are equal only while the margin is smaller than the token.
#
# Capturing faster does not lengthen a token, so the spin buys nothing at all. Serving the one
# already in hand costs nothing and changes nothing about what the route holds.

#: How soon after a capture the same answer may be served again without opening anything. Not a
#: cache of convenience: it is the floor that stops an unsatisfiable margin from becoming an
#: unbounded loop, whatever the tenant does to token lifetimes tomorrow.
MIN_CAPTURE_INTERVAL_S = float(os.environ.get("MCP_MIN_CAPTURE_INTERVAL_S", "120"))

#: Below this there is nothing worth serving -- the token would expire during the turn it was
#: handed to, and a fresh capture, however expensive, is the honest answer.
SERVE_FLOOR_S = float(os.environ.get("MCP_TOKEN_SERVE_FLOOR_S", "90"))

_MEMO = {}
_MEMO_LOCK = __import__("threading").Lock()
_WARNED = set()


def _memo_get(agent_url, now=None):
    """The last answer for this surface, if it is recent enough and still alive."""
    now = time.time() if now is None else now
    with _MEMO_LOCK:
        entry = _MEMO.get(agent_url)
    if not entry:
        return None
    if now - entry["at"] > MIN_CAPTURE_INTERVAL_S:
        return None
    if token_life_s(entry["token"], now=now) < SERVE_FLOOR_S:
        return None
    return entry


def _memo_put(agent_url, token, template, now=None):
    with _MEMO_LOCK:
        _MEMO[agent_url] = {"token": token, "template": template,
                            "at": time.time() if now is None else now}


def forget_memo():
    """Drop the in-memory answers. For tests, and for a browser that was reset underneath us."""
    with _MEMO_LOCK:
        _MEMO.clear()
    _WARNED.clear()


def _warn_once(key, message, say):
    if key in _WARNED:
        return
    _WARNED.add(key)
    say(message)


# ---- the capture the route actually calls ---------------------------------------------------

def _say(message):
    """The default voice. NOT silence.

    The route calls capture_fn with two positional arguments and no logger, so a `log`
    defaulting to a no-op meant the tiering left no trace at all: the first real run under
    this could not be told from the old path by reading the log, which is the one question
    worth asking of a live run. socket_route prints its own captures; so does this.
    """
    try:
        print(message, flush=True)
    except Exception:
        pass


def capture_via_profile(context, agent_url, *, log=None, directory=None):
    """A drop-in for socket_route.capture_via_tab that does not send a turn when it need not.

    SAME CONTRACT, INCLUDING THE FAILURES. Returns (token, template) or raises, so the route's
    circuit breaker sees exactly the events it always saw.

    The tiers, in the order they are tried:

      2. a cached template for THIS agent, plus a token read from a page that is navigated and
         closed without sending anything. About five seconds.
      3. no cached template, or the light path produced nothing: a full capture, which runs a
         real turn, and whose template is then saved so tier 2 can serve the next refresh.

    Tier 1 is not here because it is not a capture. It is the route not calling this at all,
    which is what happens for the fifty-odd minutes a token remains valid.
    """
    say = log or _say

    recent = _memo_get(agent_url)
    if recent is not None:
        _warn_once(
            "spin:" + agent_url,
            "[profile_token] tier 1: serving the token captured %.0fs ago without opening "
            "anything. The route asked again immediately, which means its refresh margin is "
            "longer than the token this tenant issues -- capturing faster cannot lengthen a "
            "token, so the loop is served from memory instead of run."
            % (time.time() - recent["at"]), say)
        return recent["token"], recent["template"]

    template = load_template(agent_url, directory)
    if template is not None:
        try:
            token = token_via_light_page(context, agent_url, log=say)
            say("[profile_token] tier 2: cached template + a page with no turn")
            _memo_put(agent_url, token, template)
            return token, template
        except Exception as exc:
            say("[profile_token] light path declined (%s: %s); running a full capture"
                % (type(exc).__name__, str(exc)[:120]))
    say("[profile_token] tier 3: a full capture, with a real turn")
    from relay.socket_route import capture_via_tab
    token, template = capture_via_tab(context, agent_url)
    if save_template(template, agent_url, directory):
        say("[profile_token] template cached for %s; the next refresh needs no turn"
            % (template.gpt_id or "(no agent)")[:28])
    _memo_put(agent_url, token, template)
    return token, template


def capture_fn():
    """The capture the route should use, chosen at CALL time rather than at import, so an
    operator's change to the environment takes effect on the next run instead of the next
    deployment."""
    raw = os.environ.get("MCP_CAPTURE_LIGHT", "0").strip().lower()
    if raw not in ("0", "false", "no", "off", ""):
        return capture_via_profile
    from relay.socket_route import capture_via_tab
    return capture_via_tab

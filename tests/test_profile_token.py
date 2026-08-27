"""The capture must not run a turn it does not need, and must never guess when it is unsure.

The socket route's capture opens a page, sends a real message and waits for the answer -- about
thirty-five seconds during which the browser sits some 610 MB above where it started. It does
that because the chat frame is where the request template comes from. The token, measured on
the same surface, appears in an outgoing Authorization header 4.3 seconds after navigation with
nothing sent at all.

So the turn buys the template, the template does not change hourly, and the two were being
fetched together anyway. These tests pin the tiering that separates them, and -- more
importantly -- pin every way it must decline rather than hand the route something wrong.
"""
import json
import os
import time

import pytest

from relay import profile_token as PT


@pytest.fixture(autouse=True)
def _no_memo_between_tests():
    """The memo is a module global, which is right for a run and wrong for a test suite.

    Without this, a test that captures leaves its answer sitting there and the next test
    gets served from memory instead of exercising the path it was written for -- three did,
    and each failed for a reason that had nothing to do with what it was testing.
    """
    PT.forget_memo()
    yield
    PT.forget_memo()

def _jwt(aud="https://substrate.office.com/sydney", exp_in=3600, now=None):
    """A token-shaped string with the claims this module reads. Not signed: nothing here
    verifies a signature, and a test that needed a real one would be testing Entra."""
    import base64

    now = time.time() if now is None else now
    body = {"aud": aud, "exp": int(now + exp_in)}
    raw = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return "header." + raw + ".signature"


# ---- which token is worth having ------------------------------------------------------------

def test_a_token_for_the_right_resource_with_life_left_is_usable():
    assert PT.token_is_usable(_jwt(exp_in=3600))


def test_a_token_for_another_resource_is_refused():
    """A page makes Authorization-bearing calls to several backends -- graph, loki, titles and
    arc were all seen on this one. Taking the first Bearer it emits hands the route a token for
    the wrong backend, which fails a real turn before anybody finds out."""
    for other in ("https://graph.microsoft.com", "https://loki.delve.office.com",
                  "https://substrate.office.com/search", "https://arc.msn.com"):
        assert not PT.token_is_usable(_jwt(aud=other)), other


def test_a_token_about_to_expire_is_refused():
    """The route's own refresh margin is ten minutes, so a shorter token would send it straight
    back for another one -- and a worker holding it could outlive it mid-turn."""
    assert not PT.token_is_usable(_jwt(exp_in=120))
    assert not PT.token_is_usable(_jwt(exp_in=int(PT.MIN_LIFE_S) - 60))
    assert PT.token_is_usable(_jwt(exp_in=int(PT.MIN_LIFE_S) + 60))


def test_rubbish_is_refused_rather_than_crashing():
    for junk in ("", "not-a-jwt", "a.b", "a.!!!.c", None):
        assert not PT.token_is_usable(junk or "")


def test_the_audience_is_matched_on_the_claim_not_on_a_substring_of_the_token():
    """The resource has to come from `aud`. A token whose payload merely mentions the string
    somewhere is not a token for it."""
    import base64
    body = {"aud": "https://graph.microsoft.com",
            "scp": "https://substrate.office.com/sydney/sydney.readwrite"}
    raw = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    assert not PT.token_is_usable("h." + raw + ".s")


# ---- the template cache ---------------------------------------------------------------------

class _Template:
    """A RequestTemplate as this module handles one."""

    VOLATILE = ("access_token", "ConversationId", "X-SessionId")

    def __init__(self, gpt_id="T_agent.abc", query=None, frame=None):
        self.query = query if query is not None else {"gptId": gpt_id, "variants": "a,b",
                                                      "ConversationId": "per-turn"}
        self.frame = frame if frame is not None else {"threadLevelGptId": {"id": gpt_id}}
        self.gpt_id = gpt_id


def test_a_saved_template_comes_back(tmp_path):
    assert PT.save_template(_Template(), "agent-1", str(tmp_path))
    got = PT.load_template("agent-1", str(tmp_path))
    assert got is not None and got.gpt_id


def test_each_agent_surface_gets_its_own_cache(tmp_path):
    """The template names WHICH agent -- gptId is in both the query and the frame -- so one
    cache shared across surfaces would send every goal to whichever agent was captured last."""
    PT.save_template(_Template(gpt_id="T_first.x"), "agent-1", str(tmp_path))
    PT.save_template(_Template(gpt_id="T_second.y"), "agent-2", str(tmp_path))
    first = PT.load_template("agent-1", str(tmp_path))
    second = PT.load_template("agent-2", str(tmp_path))
    assert first.gpt_id != second.gpt_id
    assert PT.template_path("agent-1", str(tmp_path)) != PT.template_path("agent-2", str(tmp_path))


def test_the_per_turn_keys_are_not_persisted(tmp_path):
    """A conversation id belongs to one turn. Writing it into a cache and replaying it makes
    every later turn claim to be that conversation."""
    PT.save_template(_Template(), "agent-1", str(tmp_path))
    raw = json.load(open(PT.template_path("agent-1", str(tmp_path)), encoding="utf-8"))
    assert "ConversationId" not in raw["query"]
    assert "gptId" in raw["query"]


def test_nothing_credential_shaped_reaches_disk(tmp_path):
    """RequestTemplate strips access_token on the way in and this drops the per-turn keys
    again, so what lands on disk is a request shape and an agent id."""
    tmpl = _Template(query={"gptId": "T_a.b", "access_token": "SECRET-VALUE",
                            "variants": "x,y"})
    PT.save_template(tmpl, "agent-1", str(tmp_path))
    text = open(PT.template_path("agent-1", str(tmp_path)), encoding="utf-8").read()
    assert "SECRET-VALUE" not in text


def test_a_template_naming_no_agent_is_not_a_cache_hit(tmp_path):
    """The one thing the tab capture refuses to return. A request naming no agent reaches the
    default Copilot -- no connectors, no tenant grounding -- and answers fluently while the
    route reports success."""
    PT.save_template(_Template(gpt_id=""), "agent-1", str(tmp_path))
    assert PT.load_template("agent-1", str(tmp_path)) is None


def test_a_missing_or_corrupt_cache_is_not_an_error(tmp_path):
    assert PT.load_template("never-saved", str(tmp_path)) is None
    path = PT.template_path("broken", str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write("{not json")
    assert PT.load_template("broken", str(tmp_path)) is None


def test_a_stale_cache_is_ignored(tmp_path):
    """A backstop, not the mechanism -- the real signal is the backend refusing a request. This
    only covers a drift that changes an answer without ever producing an error."""
    PT.save_template(_Template(), "agent-1", str(tmp_path))
    assert PT.load_template("agent-1", str(tmp_path), max_age_s=3600) is not None
    assert PT.load_template("agent-1", str(tmp_path),
                            now=time.time() + 7200, max_age_s=3600) is None


def test_a_half_written_cache_is_never_readable(tmp_path):
    """Written to a temporary name and renamed, so a process killed mid-write leaves either the
    old cache or the new one and never half of either."""
    import inspect
    src = inspect.getsource(PT.save_template)
    assert "os.replace" in src


def test_discard_forgets_only_that_agent(tmp_path):
    PT.save_template(_Template(), "agent-1", str(tmp_path))
    PT.save_template(_Template(), "agent-2", str(tmp_path))
    PT.discard_template("agent-1", str(tmp_path))
    assert PT.load_template("agent-1", str(tmp_path)) is None
    assert PT.load_template("agent-2", str(tmp_path)) is not None


def test_discarding_something_absent_is_not_an_error(tmp_path):
    PT.discard_template("never-saved", str(tmp_path))


# ---- the tiering -----------------------------------------------------------------------------

def test_with_a_cached_template_the_light_path_is_used(tmp_path, monkeypatch):
    PT.save_template(_Template(), "agent-1", str(tmp_path))
    calls = []
    monkeypatch.setattr(PT, "token_via_light_page",
                        lambda ctx, url, **kw: calls.append(url) or _jwt())
    token, template = PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path))
    assert calls == ["agent-1"], "the light path was not tried"
    assert token and template.gpt_id


def test_without_a_cached_template_a_full_capture_runs_and_the_template_is_saved(tmp_path,
                                                                                monkeypatch):
    """Tier 3 is not a failure mode, it is where a template comes from. The next refresh is
    tier 2 precisely because this one ran."""
    monkeypatch.setattr(PT, "token_via_light_page",
                        lambda *a, **k: pytest.fail("the light path cannot work without a template"))
    import relay.socket_route as SR
    monkeypatch.setattr(SR, "capture_via_tab", lambda ctx, url: (_jwt(), _Template()))
    token, template = PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path))
    assert token and template.gpt_id
    assert PT.load_template("agent-1", str(tmp_path)) is not None, "the template was not cached"


def test_a_failing_light_path_falls_through_to_the_full_capture(tmp_path, monkeypatch):
    """THE WHOLE FALLBACK STRUCTURE. Nothing here may fail a capture that the ordinary path
    would have completed: a light path that cannot produce a token costs five seconds and then
    the run proceeds exactly as it always did."""
    PT.save_template(_Template(), "agent-1", str(tmp_path))

    def refuse(*a, **k):
        raise PT.NoLightToken("nothing seen")

    monkeypatch.setattr(PT, "token_via_light_page", refuse)
    import relay.socket_route as SR
    monkeypatch.setattr(SR, "capture_via_tab", lambda ctx, url: (_jwt(), _Template()))
    token, template = PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path))
    assert token and template.gpt_id


def test_an_unexpected_error_in_the_light_path_also_falls_through(tmp_path, monkeypatch):
    """Not just NoLightToken. A browser that went away mid-probe raises something else entirely,
    and the answer is the same: run the capture that has always worked."""
    PT.save_template(_Template(), "agent-1", str(tmp_path))

    def explode(*a, **k):
        raise RuntimeError("target closed")

    monkeypatch.setattr(PT, "token_via_light_page", explode)
    import relay.socket_route as SR
    monkeypatch.setattr(SR, "capture_via_tab", lambda ctx, url: (_jwt(), _Template()))
    token, _template = PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path))
    assert token


def test_a_failing_full_capture_still_raises(tmp_path, monkeypatch):
    """The route's circuit breaker has to see the same events it always saw. Swallowing a
    capture failure here would leave it counting successes that did not happen."""
    import relay.socket_route as SR

    def boom(ctx, url):
        raise RuntimeError("capture failed")

    monkeypatch.setattr(SR, "capture_via_tab", boom)
    with pytest.raises(RuntimeError):
        PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path))


# ---- the switch --------------------------------------------------------------------------------

def test_it_is_off_unless_the_flag_says_otherwise(monkeypatch):
    monkeypatch.delenv("MCP_CAPTURE_LIGHT", raising=False)
    assert PT.capture_fn().__name__ == "capture_via_tab"
    for off in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("MCP_CAPTURE_LIGHT", off)
        assert PT.capture_fn().__name__ == "capture_via_tab", off
    monkeypatch.setenv("MCP_CAPTURE_LIGHT", "1")
    assert PT.capture_fn().__name__ == "capture_via_profile"


def test_the_route_asks_which_capture_rather_than_naming_one():
    """socket_route.py is frozen, so a policy about cost is applied by its caller."""
    from _srcprobe import executable_source

    from relay import relay_fleet
    code = executable_source(relay_fleet._socket_route)
    assert "_choose_capture" in code
    assert "capture_via_tab" not in code


# ---- the drift signal ---------------------------------------------------------------------------

def test_a_refused_request_shape_discards_the_cached_templates(tmp_path, monkeypatch):
    """A cached template is reused until something says it is wrong, and this is that something.
    InvalidRequest is the backend refusing the request it was sent -- not a dropped socket, not
    a consent card, but the shape itself."""
    from relay import relay_fleet

    monkeypatch.setattr(PT, "TEMPLATE_DIR", str(tmp_path))
    PT.save_template(_Template(), "agent-1", str(tmp_path))
    assert relay_fleet._forget_cached_templates(
        "ChatHubError: the backend declined the request: InvalidRequest") is True
    assert PT.load_template("agent-1", str(tmp_path)) is None


def test_a_transport_fault_leaves_the_templates_alone(tmp_path, monkeypatch):
    """A dropped connection says nothing about the request shape, and throwing the cache away
    on one would put a thirty-five second capture behind every network blip."""
    from relay import relay_fleet

    monkeypatch.setattr(PT, "TEMPLATE_DIR", str(tmp_path))
    PT.save_template(_Template(), "agent-1", str(tmp_path))
    for reason in ("ConnectionClosedError: no close frame received or sent",
                   "could not open the socket: InvalidProxyStatus: HTTP 502",
                   "the turn completed but carried no text",
                   "consent card"):
        assert relay_fleet._forget_cached_templates(reason) is False, reason
    assert PT.load_template("agent-1", str(tmp_path)) is not None


# ---- tier 1: the answer that opens nothing ---------------------------------------------------

def test_a_recent_answer_is_served_without_touching_the_browser(tmp_path, monkeypatch):
    """THE SPIN THIS EXISTS TO STOP. The route's refresh margin is 1500 seconds and the tokens
    this tenant issues run 9 to 81 minutes, so whenever a short one comes back `needs_refresh`
    is true the instant the capture meant to satisfy it finishes -- 3.8 captures a minute on the
    run that exposed it, each holding a 610 MB page for thirty-five seconds. Capturing faster
    cannot lengthen a token, so the second ask is answered from memory."""
    PT.forget_memo()
    PT.save_template(_Template(), "agent-1", str(tmp_path))
    calls = []
    monkeypatch.setattr(PT, "token_via_light_page",
                        lambda ctx, url, **kw: calls.append(url) or _jwt())
    PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path))
    PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path))
    PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path))
    assert len(calls) == 1, "the browser was opened %d times for one answer" % len(calls)


def test_the_memo_expires(tmp_path, monkeypatch):
    """A floor, not a cache of convenience. It stops an unsatisfiable margin becoming an
    unbounded loop; it must not stop an ordinary refresh from ever happening."""
    PT.forget_memo()
    PT.save_template(_Template(), "agent-1", str(tmp_path))
    calls = []
    monkeypatch.setattr(PT, "token_via_light_page",
                        lambda ctx, url, **kw: calls.append(url) or _jwt())
    PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path))
    # NEGATIVE, not zero: the comparison is `elapsed > interval`, and a test fast
    # enough to make elapsed exactly 0.0 fails against a 0.0 interval.
    monkeypatch.setattr(PT, "MIN_CAPTURE_INTERVAL_S", -1.0)
    PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path))
    assert len(calls) == 2


def test_an_expiring_memo_is_not_served(tmp_path, monkeypatch):
    """Serving a token that dies during the turn it was handed to is worse than an expensive
    capture: the turn fails, and the route learns nothing about why."""
    PT.forget_memo()
    PT._memo_put("agent-1", _jwt(exp_in=30), _Template())
    assert PT._memo_get("agent-1") is None
    PT.forget_memo()
    PT._memo_put("agent-1", _jwt(exp_in=3600), _Template())
    assert PT._memo_get("agent-1") is not None


def test_each_surface_has_its_own_memo(tmp_path, monkeypatch):
    """Serving one agent's token for another would send the goal to the wrong agent, which is
    the same defect as sharing the template cache and just as silent."""
    PT.forget_memo()
    PT._memo_put("agent-1", _jwt(), _Template(gpt_id="T_first.x"))
    assert PT._memo_get("agent-2") is None
    assert PT._memo_get("agent-1") is not None


def test_the_memo_holds_the_template_that_came_with_the_token(tmp_path, monkeypatch):
    PT.forget_memo()
    PT.save_template(_Template(gpt_id="T_only.z"), "agent-1", str(tmp_path))
    monkeypatch.setattr(PT, "token_via_light_page", lambda ctx, url, **kw: _jwt())
    _t1, first = PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path))
    _t2, second = PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path))
    assert first.gpt_id == second.gpt_id == "T_only.z"


def test_a_full_capture_is_memoised_too(tmp_path, monkeypatch):
    """Tier 3 is the expensive one, so it is the one that most needs not to be repeated when the
    route asks again a second later."""
    PT.forget_memo()
    import relay.socket_route as SR
    calls = []
    monkeypatch.setattr(SR, "capture_via_tab",
                        lambda ctx, url: calls.append(url) or (_jwt(), _Template()))
    PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path))
    PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path))
    assert len(calls) == 1


def test_the_spin_is_explained_once_not_every_time(tmp_path, monkeypatch, capsys):
    """A line per capture at 3.8 a minute is a log nobody reads. The condition is structural, so
    it is worth saying clearly and worth saying once."""
    PT.forget_memo()
    PT.save_template(_Template(), "agent-1", str(tmp_path))
    monkeypatch.setattr(PT, "token_via_light_page", lambda ctx, url, **kw: _jwt())
    said = []
    for _ in range(4):
        PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path),
                               log=said.append)
    tier1 = [m for m in said if "tier 1" in m]
    assert len(tier1) == 1, "said %d times" % len(tier1)
"""The refresh margin has to be satisfiable by the tokens the tenant actually issues.

A margin longer than the token makes `needs_refresh` true the instant the capture meant to
satisfy it finishes, and the route captures again, and again -- 3.8 times a minute on the run
that exposed it, each holding a page for thirty-five seconds. Capturing faster cannot lengthen
a token, so the loop buys nothing at all.
"""
import os

from relay import profile_token as PT


def _observed_tokens_minutes():
    """The first capture of each recorded run, which is what a token looks like asked fresh.

    ONE PER RUN, because a short token gets re-captured every few seconds by the very spin
    being measured: counting all 137 captures says 74.5% are under 25 minutes, and counting one
    per run says 3.8%. The defect inflates the statistic that would justify it.
    """
    import glob
    import re

    firsts = []
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in glob.glob(os.path.join(root, ".fleet", "coordinator_*.log")):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        found = re.findall(r"captured: ([0-9]+) min", text)
        if found:
            firsts.append(int(found[0]))
    return sorted(firsts)


def test_the_margin_still_covers_the_longest_turn_a_worker_can_take():
    """THE MARGIN WAS LOWERED TO 600 AND PUT BACK, and this is why.

    The tenant issues tokens with a median of 52 minutes and a minimum of 14, so the margin
    is sometimes unsatisfiable and the route spins. Shrinking it to 600 looked like the fix
    and was not: a turn may run SOCKET_TURN_TIMEOUT_S (1200 s) and the header is read once,
    at connect, so a margin of 600 lets a turn start with 601 seconds of token and expire
    with the answer half-written. relay/test_socket_route.py has asserted against exactly
    that since a run where it happened, and it caught this change.

    The spin's answer is the memo below, not a shorter margin: it covers the 3.8% of runs
    where the margin cannot be met without breaking the 96% where it can."""
    import inspect

    from relay import relay_fleet
    from relay.socket_route import SocketRoute

    longest = max(relay_fleet.SOCKET_TURN_TIMEOUT_S,
                  inspect.signature(SocketRoute.driver_for)
                  .parameters["turn_timeout_s"].default)
    assert relay_fleet.SOCKET_REFRESH_MARGIN_S > longest


def test_the_token_distribution_is_recorded_beside_the_margin():
    """A margin defended by measurement is only defensible while the measurement is next to
    it -- including the population note, because counting all 137 captures says 74.5% of
    tokens are short and counting one per run says 3.8%. The spin inflates the statistic
    that would justify shrinking the margin."""
    import inspect

    from relay import relay_fleet
    src = inspect.getsource(relay_fleet)
    i = src.index("SOCKET_REFRESH_MARGIN_S = float(")
    note = src[max(0, i - 2000):i]
    for fact in ("median of 52 minutes", "3.8%", "74.5%"):
        assert fact in note, "the note beside the margin does not carry %r" % fact

def test_a_short_token_no_longer_loops_even_if_the_margin_is_unsatisfiable(tmp_path,
                                                                          monkeypatch):
    """THE BACKSTOP, WHICH THE MARGIN CHANGE DOES NOT REPLACE. A tenant can shorten token
    lifetimes tomorrow; the memo means the route is served rather than spun whatever it does."""
    import json
    import time

    # THE FILE'S OWN FAKE, not a fresh one. The first version of this test defined a
    # template with an EMPTY frame, so the reconstructed RequestTemplate had no gpt_id, so
    # load_template correctly refused it and the whole test measured tier 3 while claiming
    # to measure tier 1.

    import base64
    body = base64.urlsafe_b64encode(
        json.dumps({"aud": PT.AUDIENCE, "exp": int(time.time() + 3600)}).encode()
    ).decode().rstrip("=")
    token = "h." + body + ".s"

    PT.forget_memo()
    PT.save_template(_Template(), "agent-1", str(tmp_path))
    calls = []
    monkeypatch.setattr(PT, "token_via_light_page",
                        lambda ctx, url, **kw: calls.append(1) or token)
    for _ in range(20):
        PT.capture_via_profile(object(), "agent-1", directory=str(tmp_path))
    assert len(calls) == 1, "twenty asks opened %d pages" % len(calls)

"""The refuter / research side agents get a socket the way a worker does, or HOLD -- never UNCLEAR.

Live 2026-10-15: every goal ended "refuter#N: UNCLEAR: harness: opening the side page failed:
CopilotTabForbidden". The side session tried a socket once, demanding a token with more than the
600 s refresh margin left (else a synchronous capture), and any False fell through to a page the
no-tab rule forbids. These tests use the REAL SocketRoute and a fake socket driver.
"""
import base64
import json
import time
import types

import pytest

from relay import mcp_ready, refuter as RF
from relay import relay_fleet as rf
from relay import side_socket as SS
from relay import socket_route as _SR
from relay.socket_route import SocketRoute


def _token(seconds):
    body = json.dumps({"oid": "o", "tid": "t", "exp": time.time() + seconds}).encode()
    return "x." + base64.urlsafe_b64encode(body).decode().rstrip("=") + ".y"


class _Tpl:
    def __init__(self, gpt="T_agent.impl"):
        self.gpt_id = gpt

    def with_deep_research_model(self, _m):
        return self


class _Gate:
    def __init__(self, why=""):
        self.why = why

    def reason(self):
        return self.why


class _FakeSocketDriver:
    """Stands in for CopilotSocketDriver: records sends, answers with a canned reply."""
    instances = []

    def __init__(self, conv, connect=None):
        self.conv, self.sent, self._text, self.failed = conv, [], "", ""
        self.closed = False
        type(self).instances.append(self)

    reply = "UPHELD"

    def send(self, text, **_kw):
        self.sent.append(text)
        self._text = type(self).reply

    def _answers(self):
        return types.SimpleNamespace(count=lambda: len(self.sent))

    def read_last_response(self):
        return self._text

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(_SR, "DEFAULT_LOG", str(tmp_path / "isolated.jsonl"))
    monkeypatch.setattr(mcp_ready, "gate", lambda: _Gate(""))
    import relay.socket_driver as SD
    monkeypatch.setattr(SD, "CopilotSocketDriver", _FakeSocketDriver)
    _FakeSocketDriver.instances = []
    _FakeSocketDriver.reply = "UPHELD"
    from relay import token_provider
    monkeypatch.setattr(token_provider, "_HOLD", token_provider.TokenHold())

    def boom(*_a, **_k):
        raise AssertionError("a synchronous capture / a page on the sweep thread")
    monkeypatch.setattr(RF.RefuterSession, "_do_open", lambda self: boom())


AGENT = "https://m365.example/chat/agent/impl"
REVIEWER = "https://m365.example/chat/agent/reviewer"


def _route(token_s=3600, agent=AGENT, tpl=None, **kw):
    kw.setdefault("refresh_margin_s", 600)
    r = SocketRoute(enabled=True, connect_fn=object(),
                    capture_fn=lambda *_a: (_ for _ in ()).throw(
                        AssertionError("synchronous capture")), **kw)
    if token_s is not None:
        r.install_capture(_token(token_s), tpl or _Tpl(), agent)
    return r


def _kicks():
    got = []
    return got, (lambda route, url: got.append(url) or True)


def _acquire(route, url=AGENT, **kw):
    kw.setdefault("hold_name", "refuter-t")
    kw.setdefault("turn_timeout_s", 600.0)
    kw.setdefault("frame_timeout_s", 120.0)
    return SS.acquire_side_driver("refuter", url, route=route, **kw)


# ---- the live defect ------------------------------------------------------------------------

def test_a_token_inside_the_refresh_margin_is_used_not_refreshed_synchronously():
    """THE ROOT CAUSE. 300 s of life < the 600 s margin: needs_refresh() is True, a worker
    happily takes the socket, and the side session used to run route.refresh() (a synchronous
    capture) and give up."""
    route = _route(token_s=300)
    assert route.needs_refresh(AGENT) and route.ready(AGENT)
    kicked, kick = _kicks()
    drv, why = _acquire(route, kick=kick)
    assert drv is not None and why == ""
    assert isinstance(drv, _FakeSocketDriver)
    assert kicked == [AGENT], "the renewal is asked of the off-thread capture manager"


def test_the_review_runs_to_a_real_verdict_with_the_margin_short_and_no_page(monkeypatch):
    route = _route(token_s=300)
    monkeypatch.setattr(rf, "_socket_route", lambda: route)
    monkeypatch.setattr(rf, "_consider_socket_refresh", lambda r, u: True)
    _FakeSocketDriver.reply = "REFUTED: no test was added"
    s = RF.RefuterSession(object(), AGENT, "goal", "I fixed it. DONE", dwell_s=0.0,
                          max_nudges=0).start()
    verdict = None
    for _ in range(30):
        verdict = s.poll()
        if verdict is not None:
            break
    assert verdict is not None and verdict[0] == "REFUTED", verdict
    assert not RF.unclear_is_harness_fault(verdict[1])
    [drv] = _FakeSocketDriver.instances
    assert drv.sent and "goal" in drv.sent[0], "the reviewer was actually asked"
    assert drv.closed


def test_the_reviewer_conversation_is_its_own_not_the_workers():
    route = _route()
    d1, _ = _acquire(route)
    d2, _ = _acquire(route)
    assert d1 is not d2 and d1.conv is not d2.conv, "a fresh conversation per review"


# ---- holds, with a reason, never a verdict --------------------------------------------------

def test_no_token_yet_holds_with_a_reason():
    route = _route(token_s=None)
    route.default_agent_url = AGENT
    # a template on disk exists but nothing is captured in memory yet
    kicked, kick = _kicks()
    drv, why = _acquire(route, kick=kick, disk_template=lambda u: _Tpl())
    assert drv is None and why.startswith("waiting for token")
    assert kicked == [AGENT], "the missing token is requested off-thread"


def test_an_expired_token_holds_and_requests_a_new_one():
    route = _route(token_s=-5)
    kicked, kick = _kicks()
    drv, why = _acquire(route, kick=kick)
    assert drv is None and "waiting for token" in why and kicked == [AGENT]


def test_the_mcp_gate_holds_a_new_conversation():
    route = _route()
    drv, why = _acquire(route, gate=_Gate("waiting for MCP server (not reachable)"))
    assert drv is None and why.startswith("waiting for MCP server")


def test_a_reviewer_agent_with_no_cached_template_says_so():
    """A different agent surface that was never captured: a capture would only raise
    NoTemplateWithoutTurn, so the hold names it instead of launching helpers or ending UNCLEAR."""
    route = _route()                       # the worker's agent is ready; the reviewer's is not
    kicked, kick = _kicks()
    drv, why = _acquire(route, url=REVIEWER, kick=kick, disk_template=lambda u: None)
    assert drv is None
    assert "refuter agent has no cached template" in why
    assert kicked == [], "no capture helper is launched for an agent that cannot be captured"


def test_a_reviewer_agent_with_a_cached_template_is_captured_off_thread():
    route = _route()
    kicked, kick = _kicks()
    drv, why = _acquire(route, url=REVIEWER, kick=kick, disk_template=lambda u: _Tpl("T_rev"))
    assert drv is None and why.startswith("waiting for token") and kicked == [REVIEWER]
    route.install_capture(_token(3600), _Tpl("T_rev"), REVIEWER)      # the capture lands
    drv, why = _acquire(route, url=REVIEWER, kick=kick)
    assert drv is not None and drv.conv.template.gpt_id == "T_rev", "the reviewer's own template"


def test_the_refuter_is_the_workers_agent_when_its_url_has_no_entry():
    route = _route(agent=AGENT)
    route.default_agent_url = AGENT
    drv, why = _acquire(route, url="", same_agent_as_default=True)
    assert drv is not None and drv.conv.template.gpt_id == "T_agent.impl"
    drv2, why2 = _acquire(route, url=REVIEWER, same_agent_as_default=False,
                          disk_template=lambda u: None)
    assert drv2 is None, "a side agent that is NOT the worker's must never borrow its template"


def test_a_shut_route_holds_and_names_it():
    route = _route()
    route.close_route("3 consecutive failures")
    drv, why = _acquire(route)
    assert drv is None and why.startswith("socket route unavailable")


def test_a_held_review_registers_in_token_wait_and_releases_on_start(monkeypatch):
    from relay import token_provider
    route = _route(token_s=None)
    _acquire(route, disk_template=lambda u: _Tpl())
    assert token_provider.hold().summary().get("waiting") == 1
    route.install_capture(_token(3600), _Tpl(), AGENT)
    drv, _ = _acquire(route)
    assert drv is not None and not token_provider.hold().summary()


def test_the_session_holds_then_reviews_when_the_route_comes_up(monkeypatch):
    route = _route(token_s=None)
    monkeypatch.setattr(rf, "_socket_route", lambda: route)
    monkeypatch.setattr(rf, "_consider_socket_refresh", lambda r, u: True)
    import relay.profile_token as PT
    monkeypatch.setattr(PT, "load_template", lambda u, *a, **k: _Tpl())
    s = RF.RefuterSession(object(), AGENT, "goal", "DONE", dwell_s=0.0, max_nudges=0).start()
    for _ in range(3):
        s._next_try_at = 0.0
        assert s.poll() is None
    assert s._done is None and s.hold_reason.startswith("waiting for token")
    route.install_capture(_token(3600), _Tpl(), AGENT)
    _FakeSocketDriver.reply = "UPHELD"
    s._next_try_at = 0.0
    verdict = None
    for _ in range(30):
        verdict = s.poll()
        if verdict is not None:
            break
    assert verdict is not None and verdict[0] == "UPHELD"


def test_unclear_is_still_returned_when_the_reviewer_truly_cannot_judge(monkeypatch):
    route = _route()
    monkeypatch.setattr(rf, "_socket_route", lambda: route)
    monkeypatch.setattr(rf, "_consider_socket_refresh", lambda r, u: True)
    _FakeSocketDriver.reply = "I looked at the files but cannot decide."
    s = RF.RefuterSession(object(), AGENT, "goal", "DONE", dwell_s=0.0, max_nudges=0).start()
    verdict = None
    for _ in range(60):
        verdict = s.poll()
        if verdict is not None:
            break
    assert verdict is not None and verdict[0] == "UNCLEAR"
    assert not RF.unclear_is_harness_fault(verdict[1]), "a reviewer outcome, not a harness hole"


# ---- the research side agent ----------------------------------------------------------------

def _research(monkeypatch, route, **kw):
    from relay import agent_profiles as AP
    monkeypatch.setattr(rf, "_socket_route", lambda: route)
    monkeypatch.setattr(rf, "_consider_socket_refresh", lambda r, u: True)
    prof = types.SimpleNamespace(name="researcher", url=REVIEWER, model_picker=None)
    return AP.ResearchSession(object(), "調べて", profile=prof, **kw)


def test_research_takes_its_own_agents_socket_inside_the_margin(monkeypatch):
    route = _route(token_s=300, agent=REVIEWER, tpl=_Tpl("P_research.dr_work"))
    s = _research(monkeypatch, route)
    assert s._try_socket() is True
    assert s.socket and s.transport == "socket" and s.page is None
    assert s.drv.conv.template.gpt_id == "P_research.dr_work"
    assert s.drv.sent == ["調べて"]


def test_research_without_a_cached_template_holds_and_does_not_end_empty(monkeypatch):
    route = _route(token_s=3600, agent=AGENT)        # only the implementation agent is known
    import relay.profile_token as PT
    monkeypatch.setattr(PT, "load_template", lambda u, *a, **k: None)
    s = _research(monkeypatch, route)
    s.start()
    s._t_send -= 10_000                               # far past timeout_s: still no verdict
    s._next_try_at = 0.0
    assert s.poll() is None
    assert s._done is None
    assert "research agent has no cached template" in s.hold_reason


def test_research_holds_for_the_mcp_gate_then_runs(monkeypatch):
    route = _route(token_s=3600, agent=REVIEWER, tpl=_Tpl("P_r"))
    state = {"why": "waiting for MCP server (not reachable)"}
    monkeypatch.setattr(mcp_ready, "gate", lambda: _Gate(state["why"]))
    s = _research(monkeypatch, route)
    s.start()
    s._next_try_at = 0.0
    assert s.poll() is None and s.hold_reason.startswith("waiting for MCP server")
    state["why"] = ""
    s._next_try_at = 0.0
    assert s.poll() is None and s.socket and s.drv.sent == ["調べて"]


def test_the_page_open_stays_refused(monkeypatch):
    """Nothing in this change reopens a page: the legacy page opener still raises."""
    from relay.no_tab import CopilotTabForbidden
    from relay import agent_profiles as AP
    prof = types.SimpleNamespace(name="r", url=REVIEWER, model_picker=None)
    s = AP.ResearchSession(object(), "q", profile=prof)
    s._do_open()
    assert s._done == "" and "open failed: CopilotTabForbidden" in s.error
    assert CopilotTabForbidden

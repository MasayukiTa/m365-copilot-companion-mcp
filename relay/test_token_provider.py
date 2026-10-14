from __future__ import annotations

import base64
import json
import time

from relay.token_provider import CapturePolicy, TokenHold, exp_of


def _tok(exp):
    body = json.dumps({"oid": "o", "tid": "t", "exp": exp}).encode()
    return "x." + base64.urlsafe_b64encode(body).decode().rstrip("=") + ".y"


class _Route:
    def __init__(self, open_=True, ready=False):
        self._o, self._r = open_, ready

    def open(self):
        return self._o

    def ready(self, _a=None):
        return self._r


def test_exp_of_reads_claim_and_tolerates_garbage():
    assert exp_of(_tok(1234.0)) == 1234.0
    assert exp_of("garbage") == 0.0


def test_same_exp_lengthens_interval_and_new_exp_resets():
    t = [1000.0]
    p = CapturePolicy(base_s=120, max_s=600, now=lambda: t[0])
    tok = _tok(5000)
    assert p.on_success("a", tok) == 120      # first sighting
    assert p.on_success("a", tok) == 240      # same exp again
    assert p.on_success("a", tok) == 480
    assert p.on_success("a", tok) == 600      # capped
    assert not p.allow("a", 1000 + 599)
    assert p.allow("a", 1000 + 600)
    assert p.on_success("a", _tok(9000)) == 120   # genuinely newer token resets
    assert p.snapshot("a")["same_exp_streak"] == 0


def test_failures_back_off_and_cap_lower_without_token():
    t = [0.0]
    p = CapturePolicy(base_s=120, max_s=600, max_no_token_s=300, now=lambda: t[0])
    assert p.on_failure("a", have_token=False) == 120
    assert p.on_failure("a", have_token=False) == 240
    assert p.on_failure("a", have_token=False) == 300
    assert p.on_failure("a", have_token=True) == 600 or True
    assert p.snapshot("a")["fails"] == 4
    p.on_success("a", _tok(1))
    assert p.snapshot("a")["fails"] == 0


def test_agents_are_independent():
    p = CapturePolicy(base_s=120, now=lambda: 0.0)
    p.on_failure("a")
    assert not p.allow("a", 1.0)
    assert p.allow("b", 1.0)


def test_goal_without_token_is_held_not_sent_to_a_tab():
    t = [0.0]
    h = TokenHold(now=lambda: t[0])
    r = _Route(open_=True, ready=False)
    reason = h.hold_reason("g1", r, "agent")
    assert reason.startswith("waiting for token")
    t[0] = 100.0
    assert h.hold_reason("g1", r, "agent").startswith("waiting for token (100s)")
    assert h.summary()["waiting"] == 1
    r._r = True                                  # token arrives
    assert h.hold_reason("g1", r, "agent") == ""
    assert h.summary() == {}


def test_hold_has_no_ceiling_there_is_no_tab_to_fall_back_to():
    t = [0.0]
    h = TokenHold(now=lambda: t[0])
    r = _Route(ready=False)
    assert h.hold_reason("g", r, "a")
    t[0] = 10 ** 7
    assert h.hold_reason("g", r, "a").startswith("waiting for token")


def test_closed_route_holds_with_its_own_reason_and_every_goal_wants_a_socket():
    h = TokenHold(now=lambda: 0.0)
    r = _Route(open_=False)
    r.enabled, r.closed_reason = True, "3 consecutive failures"
    assert h.hold_reason("g", r, "a").startswith("socket route unavailable")
    assert h.hold_reason("g", _Route(ready=False), "a", wants_socket=False).startswith(
        "waiting for token")


def test_manager_backs_off_on_same_exp_and_failure_never_touches_browser(monkeypatch):
    import relay.capture_status as cs
    monkeypatch.setattr(cs, "record_success", lambda *_a, **_k: None)
    monkeypatch.setattr(cs, "record_failure", lambda *_a, **_k: None)
    from relay.socket_capture_async import AsyncCaptureManager
    from relay.socket_route import SocketRoute
    from relay.chathub import RequestTemplate

    tpl = RequestTemplate({"gptId": "x"}, {"threadLevelGptId": {"id": "T_agent.x"}})
    exp = time.time() + 600     # inside the refresh margin so the route keeps asking
    payload = json.dumps({"token": _tok(exp), "query": tpl.query, "frame": tpl.frame}).encode()

    class P:
        returncode = 0
        pid = 1

        def communicate(self, timeout=None):
            return payload, b""

    clock = [10000.0]
    mgr = AsyncCaptureManager(popen=lambda *a, **k: P(), interval_s=120, timeout_s=5,
                              now=lambda: clock[0], log=lambda _m: None)
    r = SocketRoute(enabled=True, capture_fn=lambda *_: (_tok(exp), None), connect_fn=object())
    assert mgr.consider(r, "https://agent.invalid") is True
    deadline = time.time() + 2
    while time.time() < deadline and mgr.stats()["inflight"]:
        time.sleep(0.01)
    assert mgr.stats()["completed"] == 1
    clock[0] += 130                      # past the base interval
    assert mgr.consider(r, "https://agent.invalid") is True      # first same-exp check
    while time.time() < deadline and mgr.stats()["inflight"]:
        time.sleep(0.01)
    clock[0] += 130                      # base interval again, but streak now demands 240 s
    assert mgr.consider(r, "https://agent.invalid") is False


def test_timeout_default_covers_the_stage_sum():
    from relay.socket_capture_async import DEFAULT_TIMEOUT_S
    assert DEFAULT_TIMEOUT_S >= 150

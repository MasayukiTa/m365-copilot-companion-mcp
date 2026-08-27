"""A capture must be unaskable more often than the floor, whatever capture is plugged in.

The route decides it needs a refresh by comparing the token against a 1500-second margin. The
tenant issues tokens with a median life of 52 minutes and a minimum of 14, so when a short one
comes back `needs_refresh` is true the instant the capture meant to satisfy it finishes -- and
the route captures again. Measured at 3.8 a minute, each holding a page for thirty-five
seconds, which is to say the browser was never without one. It ran for weeks, and every
individual capture was correct.

The floor sits between the loop and the capture, so it covers implementations that have their
own floor and implementations that do not.
"""
import time

import pytest

from relay import capture_floor as CF


def _jwt(exp_in=3600, now=None):
    import base64
    import json

    now = time.time() if now is None else now
    body = {"aud": "https://substrate.office.com/sydney", "exp": int(now + exp_in)}
    raw = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return "h." + raw + ".s"


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _wrapped(inner=None, **kw):
    calls = []

    def default(context, url):
        calls.append(url)
        return _jwt(), "template-for-" + (url or "")

    return CF.CaptureFloor(inner or default, log=lambda _m: None, **kw), calls


def test_a_second_ask_inside_the_floor_does_not_reach_the_capture():
    clock = _Clock()
    floor, calls = _wrapped(now=clock, interval_s=120)
    floor(object(), "agent-1")
    clock.t += 5
    floor(object(), "agent-1")
    clock.t += 5
    floor(object(), "agent-1")
    assert calls == ["agent-1"], "the capture ran %d times for one floor window" % len(calls)


def test_the_floored_call_returns_the_same_answer_rather_than_raising():
    """A raise would reach note_failure and count against the circuit breaker for something
    that is not a failure: the route asked twice quickly and the true answer did not change."""
    clock = _Clock()
    floor, _calls = _wrapped(now=clock, interval_s=120)
    first = floor(object(), "agent-1")
    clock.t += 1
    second = floor(object(), "agent-1")
    assert first == second


def test_the_floor_lifts_once_the_interval_passes():
    """It is a floor, not a cache. An ordinary refresh must still happen."""
    clock = _Clock()
    floor, calls = _wrapped(now=clock, interval_s=120)
    floor(object(), "agent-1")
    clock.t += 121
    floor(object(), "agent-1")
    assert len(calls) == 2


def test_an_expiring_token_is_not_served_even_inside_the_floor():
    """Handing back a token that dies during the turn it was given to is worse than an
    expensive capture: the turn fails and nobody learns why."""
    clock = _Clock()
    short = [_jwt(exp_in=30, now=clock.t)]

    def inner(context, url):
        return short[0], "t"

    floor = CF.CaptureFloor(inner, now=clock, interval_s=120, serve_floor_s=90,
                            log=lambda _m: None)
    floor(object(), "agent-1")
    clock.t += 5
    floor(object(), "agent-1")
    assert floor.stats()["captured"] == 2, "an expiring token was served from the floor"


def test_each_surface_has_its_own_floor():
    """Two agent surfaces are two independent tokens. Throttling one on the other's account
    would starve it -- the same defect as sharing a template cache, and just as quiet."""
    clock = _Clock()
    floor, calls = _wrapped(now=clock, interval_s=120)
    floor(object(), "agent-1")
    clock.t += 5
    floor(object(), "agent-2")
    assert calls == ["agent-1", "agent-2"]


def test_a_failing_capture_still_raises():
    """Nothing here may swallow a capture failure: the breaker has to see what it always saw."""
    def boom(context, url):
        raise RuntimeError("capture failed")

    floor = CF.CaptureFloor(boom, log=lambda _m: None)
    with pytest.raises(RuntimeError):
        floor(object(), "agent-1")


def test_a_failure_does_not_arm_the_floor():
    """A capture that raised produced no answer, so there is nothing to serve and the next ask
    must be allowed through. Arming on failure would turn one bad capture into two minutes of
    no route at all."""
    calls = []

    def flaky(context, url):
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("first one failed")
        return _jwt(), "t"

    clock = _Clock()
    floor = CF.CaptureFloor(flaky, now=clock, interval_s=120, log=lambda _m: None)
    with pytest.raises(RuntimeError):
        floor(object(), "agent-1")
    clock.t += 1
    token, _t = floor(object(), "agent-1")
    assert token and len(calls) == 2


def test_the_condition_is_explained_once_per_surface():
    """A line per suppressed capture at 3.8 a minute is a log nobody reads -- which is how the
    original defect survived: it was all in the log, and the log was too long to see a rate in."""
    clock = _Clock()
    said = []
    floor = CF.CaptureFloor(lambda c, u: (_jwt(), "t"), now=clock, interval_s=120,
                            log=said.append)
    floor(object(), "agent-1")
    for _ in range(10):
        clock.t += 1
        floor(object(), "agent-1")
    assert len([m for m in said if "capture_floor" in m]) == 1


def test_it_counts_what_it_suppressed_so_the_gate_can_check_it():
    clock = _Clock()
    floor, _calls = _wrapped(now=clock, interval_s=120)
    floor(object(), "agent-1")
    for _ in range(4):
        clock.t += 1
        floor(object(), "agent-1")
    assert floor.stats() == {"captured": 1, "served": 4, "surfaces": 1, "interval_s": 120.0}


def test_the_floor_can_be_switched_off_but_is_on_by_default(monkeypatch):
    inner = lambda c, u: (_jwt(), "t")
    monkeypatch.delenv("MCP_CAPTURE_FLOOR", raising=False)
    assert isinstance(CF.floored(inner), CF.CaptureFloor)
    monkeypatch.setenv("MCP_CAPTURE_FLOOR", "0")
    assert CF.floored(inner) is inner


def test_the_route_gets_a_floored_capture_whichever_capture_it_gets():
    """THE POINT OF PUTTING IT AT THE SEAM. relay/profile_token.py already refused to open a
    page it had just opened, and that protected exactly one implementation -- with the light
    path switched off the route gets capture_via_tab, which has no floor of its own."""
    from _srcprobe import executable_source

    from relay import relay_fleet
    code = executable_source(relay_fleet._socket_route)
    assert "floored" in code
    assert "capture_fn=floored(" in code

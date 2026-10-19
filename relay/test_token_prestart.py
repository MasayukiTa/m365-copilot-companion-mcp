# -*- coding: utf-8 -*-
"""The token fetch is launched at the top of a run, in parallel with the run's own set-up.

Measured 2026-10-19: the fetch is a helper process taking 17.8-28.2 s (subprocess, n=6), and it
used to be launched only from the first sweep. These tests pin that it is launched at the top,
exactly once (the sweep's own call is a no-op while it is in flight), never for an empty run,
and that it never raises. No browser and no process: the manager gets a fake Popen.
"""
from __future__ import annotations

import inspect
import time

from relay import relay_fleet as RF
from relay.socket_capture_async import AsyncCaptureManager
from relay.socket_route import SocketRoute

import relay.test_socket_capture_async as _T


def _route_with_manager(fac):
    route = SocketRoute(enabled=True, capture_fn=lambda *_: (_T._token(), None), connect_fn=object())
    route._async_capture_manager = AsyncCaptureManager(
        popen=fac, interval_s=120, timeout_s=2, log=lambda _m: None)
    return route


def _use(monkeypatch, route):
    monkeypatch.setattr(RF, "_socket_route", lambda: route)


def test_a_run_with_goals_launches_one_helper(monkeypatch):
    fac = _T._Factory(delay=0.2)
    route = _route_with_manager(fac)
    _use(monkeypatch, route)
    assert RF._prestart_socket_token(["goal"], "A") is True
    assert len(fac.calls) == 1
    _T._wait_done(route._async_capture_manager)


def test_the_sweeps_own_call_afterwards_does_not_launch_a_second(monkeypatch):
    fac = _T._Factory(delay=0.3)
    route = _route_with_manager(fac)
    _use(monkeypatch, route)
    assert RF._prestart_socket_token(["goal"], "A") is True
    assert RF._consider_socket_refresh(route, "A") is False      # what the sweep does
    assert len(fac.calls) == 1
    _T._wait_done(route._async_capture_manager)


def test_an_empty_run_fetches_nothing(monkeypatch):
    fac = _T._Factory()
    _use(monkeypatch, _route_with_manager(fac))
    assert RF._prestart_socket_token([], "A") is False
    assert fac.calls == []


def test_a_route_that_is_shut_fetches_nothing(monkeypatch):
    fac = _T._Factory()
    route = _route_with_manager(fac)
    route.enabled = False
    _use(monkeypatch, route)
    assert RF._prestart_socket_token(["g"], "A") is False
    assert fac.calls == []


def test_it_does_not_block_the_caller(monkeypatch):
    fac = _T._Factory(delay=0.5)
    route = _route_with_manager(fac)
    _use(monkeypatch, route)
    t0 = time.perf_counter()
    RF._prestart_socket_token(["g"], "A")
    assert time.perf_counter() - t0 < 0.15
    _T._wait_done(route._async_capture_manager, timeout=3.0)


def test_a_failure_to_build_the_route_never_propagates(monkeypatch):
    def boom():
        raise RuntimeError("no route")
    monkeypatch.setattr(RF, "_socket_route", boom)
    assert RF._prestart_socket_token(["g"], "A") is False


def test_run_relay_fleet_calls_it_before_the_sweep_loop():
    src = inspect.getsource(RF.run_relay_fleet)
    first = src.index("_prestart_socket_token(goals, agent_url)")
    assert first < src.index("workers = [_worker_for(")
    assert first < src.index("while (any(w.status not in TERMINAL for w in workers)")

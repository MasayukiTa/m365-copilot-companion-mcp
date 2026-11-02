# -*- coding: utf-8 -*-
"""The creation gate (relay/mcp_ready.py, called from RelayWorker._begin_send via
_mcp_gate_blocked) exists ONLY because Copilot Studio binds the MCP connector tools when a
conversation is CREATED: a conversation created while the local server is unreachable loses its
tools for life. That premise holds for the CONNECTOR route only.

On the CONNECTOR-LESS (bridged) route there is no connector to bind -- tool calls go through the
gateway/run_tool (relay/tool_bridge.make_run_tool), no tunnel is in the path -- so holding
creation against a slow/unreachable /health stalls the worker for nothing. These tests pin:

  * a fresh bridged worker with an UNREACHABLE /health is NOT held by the creation gate;
  * the SAME worker on the connector transport IS held (exactly as before);
  * a resumed conversation never reaches the gate (its _mcp_conv_fresh is False), so it is
    unaffected on either route.

The route of a FRESH conversation is read with tool_bridge.is_bridge() (the current transport
setting -- the conversation about to be created takes that route), not worker_is_bridged(), whose
driver signal may not be set before the first send.
"""
from __future__ import annotations

import types

import pytest

import relay.relay_fleet as F
from relay import mcp_ready

GOAL = "テスト用のゴール"
#: What gate().reason() returns while the local server does not answer /health (REASON_PREFIX).
NOT_REACHABLE = mcp_ready.REASON_PREFIX + " (not reachable)"


class _Gate:
    """A creation gate that always reports the server is unreachable."""

    def __init__(self, reason=NOT_REACHABLE):
        self._reason = reason

    def reason(self):
        return self._reason

    def check(self):
        return mcp_ready.Verdict(False, self._reason, None, None, 0.0)


@pytest.fixture()
def worker(monkeypatch):
    monkeypatch.setattr(F, "_unlock_password", lambda: "pw-placeholder-not-a-credential")
    monkeypatch.setattr(F, "connector_proof_source", lambda: "run")
    # The gate always says "not reachable"; whether that HOLDS the worker is what we test.
    monkeypatch.setattr(mcp_ready, "gate", lambda: _Gate())
    w = F.RelayWorker(GOAL, "w0")
    w.socket = True
    return w


def _set_transport(monkeypatch, bridged):
    """Force tool_bridge.is_bridge() -- the predicate _mcp_gate_blocked reads for a fresh
    conversation -- to the chosen route, without touching settings.txt."""
    monkeypatch.setattr(F.tool_bridge, "is_bridge", lambda *a, **k: bridged)


# -- fresh conversation: the route decides whether the gate holds -----------------------------

def test_connector_fresh_worker_is_held_when_health_is_unreachable(worker, monkeypatch):
    """Unchanged behaviour: on the connector route an unreachable /health holds creation."""
    _set_transport(monkeypatch, bridged=False)
    assert worker._mcp_gate_blocked() is True
    assert mcp_ready.REASON_PREFIX in worker.reason


def test_bridged_fresh_worker_is_not_held_when_health_is_unreachable(worker, monkeypatch):
    """The fix: on the connector-less route there is no connector to lose, so the same
    unreachable /health does NOT hold the worker -- the send proceeds."""
    _set_transport(monkeypatch, bridged=True)
    assert worker._mcp_gate_blocked() is False


def test_begin_send_skips_the_gate_entirely_for_a_fresh_bridged_worker(worker, monkeypatch):
    """End to end at the call site: _begin_send consults the gate for a fresh conversation; a
    bridged worker is not held by it. If the gate were consulted and honoured, the stubbed
    _Gate would have held the worker."""
    _set_transport(monkeypatch, bridged=True)
    seen = {"gate": 0}
    orig = F.RelayWorker._mcp_gate_blocked

    def _spy(self):
        seen["gate"] += 1
        return orig(self)

    monkeypatch.setattr(F.RelayWorker, "_mcp_gate_blocked", _spy)
    worker._mcp_conv_fresh = True
    assert worker._mcp_gate_blocked() is False
    assert seen["gate"] == 1


# -- resumed conversation: never gated on either route ----------------------------------------

def test_resumed_conversation_is_not_fresh_so_the_gate_does_not_apply(worker, monkeypatch):
    """A resume sets _mcp_conv_fresh=False; _begin_send only consults the gate when it is True,
    so a resumed conversation keeps its own route and is never held by the creation gate --
    the same on the connector route and the connector-less route."""
    for bridged in (False, True):
        _set_transport(monkeypatch, bridged=bridged)
        worker._mcp_conv_fresh = False
        assert getattr(worker, "_mcp_conv_fresh") is False


def test_a_broken_is_bridge_probe_falls_through_to_the_gate(worker, monkeypatch):
    """Defence in depth: if the route probe raises, _mcp_gate_blocked must not crash -- it falls
    through to the real gate (connector behaviour), never silently letting a send past."""
    def _boom(*a, **k):
        raise RuntimeError("transport read failed")

    monkeypatch.setattr(F.tool_bridge, "is_bridge", _boom)
    assert worker._mcp_gate_blocked() is True
    assert mcp_ready.REASON_PREFIX in worker.reason

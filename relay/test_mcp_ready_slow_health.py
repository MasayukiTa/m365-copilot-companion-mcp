# -*- coding: utf-8 -*-
"""The creation gate must tell a SLOW-but-answering MCP server from a DOWN one.

Measured 2026-11-04: under ~100% CPU the local /health answered 200 in 7-26 s, yet the gate's
3 s probe read every one of those as "not reachable" and held 19 goals (including the gateway
fixes meant to relieve the load). The fix: a short probe is the fast path; a short TIMEOUT (not
a refusal) triggers one long confirmation probe; a refusal or a non-200 body still fails at once.

These are hermetic tests of the pure pieces -- no network, no process, no .fleet. The decision
lives in `reachable_from_probes` (first probe kind, second probe kind, seconds) and the exception
mapping in `_classify_probe_exception`; the gate wiring that runs the long probe without holding
its lock is exercised in relay/test_mcp_ready.py against a fake kind-aware probe_fn.
"""
from __future__ import annotations

import errno
import socket

import pytest

from relay import mcp_ready as mr


# --- reachable_from_probes: the pure decision -----------------------------------------------
# id, first_kind, second_kind, seconds, want_reachable, want_slow, reason_fragment_or_None
REACH_CASES = [
    # Fast path: a 200 inside the short timeout is up at once, no long probe.
    ("short_ok", mr.PROBE_OK, None, 0.0, True, False, None),
    # A refusal is a truly absent server: fail at once, do NOT wait for the long probe.
    ("short_refused", mr.PROBE_REFUSED, None, 0.0, False, False, "not reachable"),
    # Reached but not healthy (non-200 / non-JSON): not reachable, at once.
    ("short_bad_body", mr.PROBE_BAD, None, 0.0, False, False, "not reachable"),
    # The slow-server case this change exists for: short timed out, long got a 200.
    ("short_timeout_then_ok", mr.PROBE_TIMEOUT, mr.PROBE_OK, 12.0, True, True, None),
    # Short timed out and the long probe was refused: the server went away -> not reachable.
    ("short_timeout_then_refused", mr.PROBE_TIMEOUT, mr.PROBE_REFUSED, 4.0, False, False,
     "not reachable"),
    # Short timed out and the long probe answered a bad body: reached, unhealthy.
    ("short_timeout_then_bad", mr.PROBE_TIMEOUT, mr.PROBE_BAD, 5.0, False, False, "not reachable"),
    # Both probes timed out: a wedged or absent server. The tail names the waited seconds.
    ("short_timeout_then_timeout", mr.PROBE_TIMEOUT, mr.PROBE_TIMEOUT, 60.0, False, False,
     "no answer in 60s"),
]


@pytest.mark.parametrize(
    "cid,first,second,seconds,want_reach,want_slow,frag",
    REACH_CASES, ids=[c[0] for c in REACH_CASES])
def test_reachable_from_probes(cid, first, second, seconds, want_reach, want_slow, frag):
    reachable, slow, tail = mr.reachable_from_probes(first, second, seconds)
    assert reachable is want_reach
    assert slow is want_slow
    if want_reach:
        assert tail == ""
    else:
        assert frag in tail


def test_a_short_ok_never_needs_a_long_probe():
    """The fast path must not depend on the second probe at all: an ok short result is reachable
    no matter what is passed as the (never-run) second probe."""
    for second in (None, mr.PROBE_TIMEOUT, mr.PROBE_REFUSED, mr.PROBE_BAD, mr.PROBE_OK):
        assert mr.reachable_from_probes(mr.PROBE_OK, second, 999.0) == (True, False, "")


def test_slow_is_true_only_when_the_long_probe_rescued_a_timeout():
    """`confirmed_slow` is the one signal that separates this change's new behaviour (a live but
    slow server) from every old verdict. It is true in exactly one row and nowhere else."""
    slow_rows = [c for c in REACH_CASES if c[5]]
    assert [c[0] for c in slow_rows] == ["short_timeout_then_ok"]


def test_an_unknown_first_kind_is_treated_as_a_timeout_not_a_refusal():
    """Erring toward one more probe is cheaper than calling a live server dead, so anything that
    is not ok/refused/bad is handled on the timeout branch (it consults the second probe)."""
    # Unknown short kind + ok long probe -> reachable (same as a timeout that was rescued).
    assert mr.reachable_from_probes("weird", mr.PROBE_OK, 8.0) == (True, True, "")
    # Unknown short kind + no long answer -> not reachable.
    reachable, slow, tail = mr.reachable_from_probes("weird", mr.PROBE_TIMEOUT, 60.0)
    assert reachable is False and slow is False and "not reachable" in tail


# --- _classify_probe_exception: timeout vs refused ------------------------------------------
class _ReasonErr(Exception):
    def __init__(self, reason):
        super().__init__(str(reason))
        self.reason = reason


def _oserr(errno_val):
    e = OSError()
    e.errno = errno_val
    return e


CLASSIFY_CASES = [
    ("socket_timeout", socket.timeout("timed out"), mr.PROBE_TIMEOUT),
    ("builtin_timeouterror", TimeoutError("timed out"), mr.PROBE_TIMEOUT),
    ("connection_refused_err", ConnectionRefusedError("refused"), mr.PROBE_REFUSED),
    ("connection_reset_err", ConnectionResetError("reset"), mr.PROBE_REFUSED),
    # urllib wraps the real cause in URLError-like .reason; both branches must be read.
    ("urlerror_wrapping_timeout", _ReasonErr(socket.timeout("timed out")), mr.PROBE_TIMEOUT),
    ("urlerror_wrapping_refused", _ReasonErr(ConnectionRefusedError("refused")),
     mr.PROBE_REFUSED),
    ("urlerror_wrapping_econnrefused", _ReasonErr(_oserr(errno.ECONNREFUSED)), mr.PROBE_REFUSED),
    ("urlerror_wrapping_ehostunreach", _ReasonErr(_oserr(errno.EHOSTUNREACH)), mr.PROBE_REFUSED),
    ("urlerror_wrapping_enetunreach", _ReasonErr(_oserr(errno.ENETUNREACH)), mr.PROBE_REFUSED),
    # An OSError with an errno we do not place errs toward timeout (one more probe, not "dead").
    ("urlerror_wrapping_other_oserror", _ReasonErr(_oserr(errno.EPIPE)), mr.PROBE_TIMEOUT),
    # Last resort: read the message text.
    ("text_refused", RuntimeError("Connection refused"), mr.PROBE_REFUSED),
    ("text_unreachable", RuntimeError("No route to host, network is unreachable"),
     mr.PROBE_REFUSED),
    ("text_unknown_defaults_to_timeout", RuntimeError("something odd happened"),
     mr.PROBE_TIMEOUT),
]


@pytest.mark.parametrize("cid,exc,want", CLASSIFY_CASES, ids=[c[0] for c in CLASSIFY_CASES])
def test_classify_probe_exception(cid, exc, want):
    assert mr._classify_probe_exception(exc) == want


# --- probe_health_kind: HTTP result -> (kind, doc), never raises -----------------------------
class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, _n=0):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_opener(monkeypatch, resp=None, exc=None):
    class _Opener:
        def open(self, url, timeout=None):
            if exc is not None:
                raise exc
            return resp
    import urllib.request
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: _Opener())


def test_probe_health_kind_ok(monkeypatch):
    _patch_opener(monkeypatch, resp=_Resp(200, b'{"status": "ok", "server_pid": 7}'))
    kind, doc = mr.probe_health_kind("http://x/health", 3.0)
    assert kind == mr.PROBE_OK and doc == {"status": "ok", "server_pid": 7}


def test_probe_health_kind_non_200_is_bad(monkeypatch):
    _patch_opener(monkeypatch, resp=_Resp(503, b'nope'))
    assert mr.probe_health_kind("http://x/health", 3.0) == (mr.PROBE_BAD, None)


def test_probe_health_kind_non_object_json_is_bad(monkeypatch):
    _patch_opener(monkeypatch, resp=_Resp(200, b'[1, 2, 3]'))
    assert mr.probe_health_kind("http://x/health", 3.0) == (mr.PROBE_BAD, None)


def test_probe_health_kind_timeout(monkeypatch):
    _patch_opener(monkeypatch, exc=socket.timeout("timed out"))
    assert mr.probe_health_kind("http://x/health", 3.0) == (mr.PROBE_TIMEOUT, None)


def test_probe_health_kind_refused(monkeypatch):
    _patch_opener(monkeypatch, exc=ConnectionRefusedError("refused"))
    assert mr.probe_health_kind("http://x/health", 3.0) == (mr.PROBE_REFUSED, None)

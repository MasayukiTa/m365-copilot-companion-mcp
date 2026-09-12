# -*- coding: utf-8 -*-
"""A timeout this process measured is a fact. It must not be filed with the inferences.

PORTED SHAPE, not code, from deepseek-harness `packages/guard/timeout-policy/src/index.ts`
(L41-75): map a deadline overrun to a structured event so later layers can branch on it, and
scope the inner timer so an inner overrun is not mistaken for an outer one. That repository is
TypeScript; what transfers is the distinction.

THE REASON THIS IS NOT A DUPLICATE of `relay/turn_outcome.py`, which already classifies
THROTTLE / RECYCLE / TRANSIENT_ERROR out of the reply text:

    turn_outcome   INFERRED. The upstream told us something and we read the message. Right for
                   events inside Copilot, which this process cannot see.
    timeout metric CONFIRMED. `time.time() - self._t_send` passed a budget this process set.
                   Nobody told us; we measured it.

A rate computed over the union of the two cannot say whether the upstream is degrading or our
own budget is too small, which is the only question worth asking about timeouts.

CAUSE AND TREATMENT STAY SEPARATE FIELDS. "timeout" is what happened; retry / salvage / stuck
is what was then chosen. Collapsed into one outcome, a retry that worked and a retry that gave
up become indistinguishable at their origin -- and "timeout therefore retry" is a reflex rather
than a policy.

NOTHING HERE CHANGES CONTROL FLOW. Observe, then contract, then detect, then intervene: a
classifier that starts steering before its own numbers are known cannot be evaluated afterwards.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import relay_fleet as RF  # noqa: E402


class _Tx:
    def __init__(self):
        self.rows = []

    def metric(self, turn, name, value, **extra):
        self.rows.append(dict(extra, turn=turn, name=name, value=value))


def _worker(socket=False, per_turn=240):
    w = RF.RelayWorker.__new__(RF.RelayWorker)
    w._tx = _Tx()
    w.turn = 3
    w.socket = socket
    w.per_turn_timeout_s = per_turn
    w.transient = 1
    return w


# ── the record itself ─────────────────────────────────────────────────────────────────────

def test_a_measured_timeout_says_it_was_measured():
    """`observed=True` is the field that keeps this out of the inference pile. Without it a
    reader joining this to turn_outcome's classes cannot tell which rows were seen and which
    were believed."""
    w = _worker()
    w._note_timeout("per_turn", 301.4, "retry")
    row = w._tx.rows[-1]
    assert row["name"] == "timeout"
    assert row["observed"] is True
    assert row["value"] == 301.4


def test_the_clock_that_fired_is_named():
    """A socket turn is bounded at 1200s inside the driver while this branch tests a 240s
    per-turn budget. Without the origin an inner overrun reads as an outer one -- the exact
    distinction the original plugin scopes its inner timer for."""
    w = _worker(socket=True)
    w._note_timeout("socket_turn", 1250.0, "stuck")
    assert w._tx.rows[-1]["origin"] == "socket_turn"

    w2 = _worker(socket=False)
    w2._note_timeout("per_turn", 250.0, "retry")
    assert w2._tx.rows[-1]["origin"] == "per_turn"


def test_the_budget_travels_with_the_elapsed_time():
    """301 seconds means nothing without the budget it passed: it is an overrun against 240 and
    well inside 1200."""
    w = _worker(per_turn=240)
    w._note_timeout("per_turn", 301.4, "retry")
    assert w._tx.rows[-1]["budget_s"] == 240


def test_cause_and_treatment_are_different_fields():
    """A retry that worked and a retry that gave up begin identically. Recording only the
    outcome loses that, and invites `timeout -> always retry`."""
    seen = set()
    for treatment in ("retry", "salvaged", "stuck"):
        w = _worker()
        w._note_timeout("per_turn", 300.0, treatment)
        row = w._tx.rows[-1]
        assert row["name"] == "timeout", "the cause changed with the treatment"
        seen.add(row["treatment"])
    assert seen == {"retry", "salvaged", "stuck"}


def test_telemetry_beside_a_failure_path_cannot_fail_again():
    """This runs on the branch where a turn already went wrong. A record that can raise there
    turns one fault into two."""
    w = _worker()

    class _Boom:
        def metric(self, *a, **k):
            raise OSError("transcript gone")

    w._tx = _Boom()
    w._note_timeout("per_turn", 300.0, "retry")     # must not raise


# ── the two families stay apart ───────────────────────────────────────────────────────────

def test_the_inferred_classifier_is_untouched():
    """turn_outcome answers a different question and keeps answering it. It reads what the
    upstream SAID; a timeout we clocked ourselves was never in its vocabulary."""
    from relay import turn_outcome
    klass, code = turn_outcome.classify("GenAIToolPlannerRateLimitReached", "assistant")
    assert klass and klass != "OK"
    # and it has no opinion about our own clock
    assert turn_outcome.error_code("the turn took 301 seconds") == ""


def test_the_call_sites_pass_the_treatment_they_took():
    """Read from source: the three branches of the timeout arm each report what they did.
    Comments are stripped first so this cannot match the explanation beside them."""
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    body = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    i = body.index("if time.time() - self._t_send > self.per_turn_timeout_s:")
    arm = body[i:i + 1400]
    for treatment in ('"retry"', '"salvaged"', '"stuck"'):
        assert "_note_timeout(_origin, _elapsed, %s)" % treatment in arm, (
            "the %s branch does not record what it did" % treatment)


def test_the_timeout_arm_still_does_what_it_did():
    """OBSERVE, DO NOT STEER. The record was added to a failure path that already had a
    policy; if this change also altered the policy, neither could be evaluated afterwards."""
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    body = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    i = body.index("if time.time() - self._t_send > self.per_turn_timeout_s:")
    arm = body[i:i + 1400]
    assert "self._retry_transient()" in arm
    assert "self._salvage_via_checks()" in arm
    assert '"stuck", "STUCK"' in arm
    # the order is the policy: retry, then salvage, then give up
    assert (arm.index("_retry_transient") < arm.index("_salvage_via_checks")
            < arm.index('"stuck", "STUCK"'))

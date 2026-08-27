"""A defect that is only visible as a RATE needs a check that can see one.

The route captured 3.8 times a minute for weeks. Every individual capture was correct -- right
token, right template, page opened and closed properly, no leak, no error. There was nothing to
see in any single event, only in how many of them there were, and nothing in this repository
asked that question.

These tests pin the shape of the answer as much as the answer: what it asserts on, what it
refuses to assert on, and the three ways it could go quietly wrong.
"""
import json
import os
import time

import pytest

import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts", "win"))

import capture_budget as B


MINUTES = {}


def _log(tmp_path, captures=1, minutes=10.0, workers=5, agents=("T_one",)):
    """A coordinator log with a chosen number of capture lines and a chosen age."""
    path = tmp_path / ("coordinator_%d.log" % int(time.time() * 1000 % 1e6))
    lines = []
    for i in range(captures):
        lines.append("[socket_route] captured: %d min of token, agent %s"
                     % (30 + i, agents[i % len(agents)]))
    lines += ["worker_done"] * workers
    path.write_text("\n".join(lines), encoding="utf-8")
    # WINDOWS WILL NOT LET os.utime MOVE A CREATION TIME, so a fixture cannot make a
    # file look ten minutes old. The age is passed to verdict() instead.
    MINUTES[str(path)] = minutes * 60
    return str(path)


def _ledger(tmp_path, spans, base=None):
    """An ownership ledger with claim/release pairs of the given durations."""
    path = tmp_path / "ownership.jsonl"
    base = base or (time.time() - 300)
    rows = []
    for i, seconds in enumerate(spans):
        key = "page%d" % i
        rows.append({"kind": "page", "key": key, "state": "held", "ts": base + i * 10})
        rows.append({"kind": "page", "key": key, "state": "released",
                     "ts": base + i * 10 + seconds})
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return str(path)


# ---- what it asserts on ----------------------------------------------------------------

def test_an_ordinary_run_passes(tmp_path):
    log = _log(tmp_path, captures=1, minutes=10)
    led = _ledger(tmp_path, [4.0])
    ok, why = B.verdict(log_path=log, elapsed_s=MINUTES.get(log), ledger=led, acked_path=str(tmp_path / "ack.json"))
    assert ok, why


def test_the_run_that_spun_fails(tmp_path):
    """Sixteen captures in four minutes. The floor allows one every two minutes."""
    log = _log(tmp_path, captures=16, minutes=4)
    led = _ledger(tmp_path, [35.0] * 16)
    ok, why = B.verdict(log_path=log, elapsed_s=MINUTES.get(log), ledger=led, acked_path=str(tmp_path / "ack.json"))
    assert not ok
    assert "16 capture" in why


def test_the_allowance_comes_from_the_enforced_floor_not_from_a_measured_rate(tmp_path,
                                                                             monkeypatch):
    """NOT A THRESHOLD FROM THE DISTRIBUTION. The measured rate is the tenant's behaviour
    today -- median token 52 minutes -- and freezing that into a check makes it cry wolf the
    day Microsoft changes it, or get 'fixed' by making it adaptive, which is a machine for
    learning to call the next defect normal. What is asserted is arithmetic on a floor this
    code enforces on itself."""
    monkeypatch.setattr(B, "_floor_interval_s", lambda: 60.0)
    log = _log(tmp_path, captures=8, minutes=10)
    led = _ledger(tmp_path, [4.0])
    ok, _why = B.verdict(log_path=log, elapsed_s=MINUTES.get(log), ledger=led, acked_path=str(tmp_path / "ack.json"))
    assert ok, "ten minutes at a 60s floor allows ten captures"
    monkeypatch.setattr(B, "_floor_interval_s", lambda: 600.0)
    ok2, _ = B.verdict(log_path=log, elapsed_s=MINUTES.get(log), ledger=led, acked_path=str(tmp_path / "ack.json"))
    assert not ok2, "the same run must fail against a ten-minute floor"


def test_the_floor_is_read_from_the_module_that_enforces_it():
    """A check that hardcodes a value somebody can change by environment variable is a false
    alarm waiting for a config edit."""
    from relay.capture_floor import MIN_CAPTURE_INTERVAL_S
    assert B._floor_interval_s() == float(MIN_CAPTURE_INTERVAL_S)


def test_a_duty_cycle_catches_what_a_count_cannot(tmp_path):
    """The defect was not really 'a number of captures' -- it was that the browser was never
    WITHOUT a page. A count cannot tell a 4-second page from a 35-second one. This is the
    future defect it catches: captures normal in number, each hanging to its timeout."""
    log = _log(tmp_path, captures=2, minutes=10)
    led = _ledger(tmp_path, [200.0, 200.0])          # within the count, far over the duty
    ok, why = B.verdict(log_path=log, elapsed_s=MINUTES.get(log), ledger=led, acked_path=str(tmp_path / "ack.json"))
    assert not ok
    assert "duty" in why


# ---- the ways it could go quietly wrong --------------------------------------------------

def test_zero_captures_with_socket_workers_is_a_contradiction_not_a_pass(tmp_path):
    """SILENT ZERO IS FAIL-OPEN. Both figures are counted by matching text; if the wording
    changes the count becomes zero and the check goes green for ever. That is the same shape
    as an allowlist that fails open, and this repository has been bitten by it before."""
    log = _log(tmp_path, captures=0, minutes=10, workers=12)
    led = _ledger(tmp_path, [])
    ok, why = B.verdict(log_path=log, elapsed_s=MINUTES.get(log), ledger=led, acked_path=str(tmp_path / "ack.json"))
    assert not ok
    assert "blind" in why or "NO capture" in why


def test_zero_captures_with_no_workers_is_fine(tmp_path):
    """An idle run captured nothing because it did nothing. Only the combination is a
    contradiction."""
    log = _log(tmp_path, captures=0, minutes=10, workers=0)
    ok, why = B.verdict(log_path=log, elapsed_s=MINUTES.get(log), ledger=_ledger(tmp_path, []),
                        acked_path=str(tmp_path / "ack.json"))
    assert ok, why


def test_a_very_short_run_is_not_judged_on_a_denominator_of_nearly_zero(tmp_path):
    """A run that has just started has elapsed near zero, and its first legitimate capture
    would look like an enormous rate. The count form avoids this; the duty form needs the
    guard."""
    log = _log(tmp_path, captures=1, minutes=0.005)
    ok, why = B.verdict(log_path=log, elapsed_s=MINUTES.get(log), ledger=_ledger(tmp_path, [4.0]),
                        acked_path=str(tmp_path / "ack.json"))
    assert ok, why


def test_a_missing_log_or_ledger_is_not_a_failure(tmp_path):
    """No run on record is not evidence of misbehaviour, and a gate that blocks on the absence
    of history blocks the first run for ever."""
    ok, _why = B.verdict(log_path=str(tmp_path / "nope.log"),
                         ledger=str(tmp_path / "nope.jsonl"),
                         acked_path=str(tmp_path / "ack.json"))
    assert ok


def test_only_paired_claims_count_toward_the_duty_cycle(tmp_path):
    """A LIMITATION STATED RATHER THAN HIDDEN. An ordinary worker tab is claimed and never
    explicitly released -- the lease and the pid check retire it -- so this measures CAPTURE
    pages, not every page that was ever open."""
    path = tmp_path / "ownership.jsonl"
    base = time.time() - 100
    rows = [{"kind": "page", "key": "never-released", "state": "held", "ts": base},
            {"kind": "page", "key": "paired", "state": "held", "ts": base},
            {"kind": "page", "key": "paired", "state": "released", "ts": base + 5}]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    spans = B.capture_spans(path=str(path))
    assert len(spans) == 1 and abs((spans[0][1] - spans[0][0]) - 5) < 0.01


# ---- the red has to be able to clear -----------------------------------------------------

def test_a_red_can_be_acknowledged_with_a_reason(tmp_path):
    """THE TRAP THIS AVOIDS. The check reads the PREVIOUS run, so the run that would prove a
    fix is the one the gate is blocking. Without a way to clear, the first red teaches people
    to force past the gate, and the gate dies. An acknowledgement is a recorded decision with
    a reason, not a switch."""
    log = _log(tmp_path, captures=16, minutes=4)
    led = _ledger(tmp_path, [35.0] * 16)
    ack = str(tmp_path / "ack.json")
    assert not B.verdict(log_path=log, elapsed_s=MINUTES.get(log), ledger=led, acked_path=ack)[0]
    B.acknowledge(log, "known spin, fixed in 99498e4", path=ack)
    ok, why = B.verdict(log_path=log, elapsed_s=MINUTES.get(log), ledger=led, acked_path=ack)
    assert ok
    assert "acknowledged" in why and "99498e4" in why


def test_acknowledging_one_run_does_not_excuse_another(tmp_path):
    ack = str(tmp_path / "ack.json")
    first = _log(tmp_path, captures=16, minutes=4)
    B.acknowledge(first, "explained", path=ack)
    time.sleep(0.01)
    second = _log(tmp_path, captures=16, minutes=4)
    ok, _why = B.verdict(log_path=second, elapsed_s=MINUTES.get(second), ledger=_ledger(tmp_path, [35.0] * 16),
                         acked_path=ack)
    assert not ok


def test_an_acknowledgement_records_why_and_when(tmp_path):
    ack = str(tmp_path / "ack.json")
    log = _log(tmp_path, captures=16, minutes=4)
    data = B.acknowledge(log, "the tenant shortened tokens; margin issue tracked", path=ack)
    entry = data[os.path.basename(log)]
    assert entry["reason"].startswith("the tenant")
    assert entry["at"]


# ---- what it deliberately does not assert on ----------------------------------------------

def test_it_does_not_put_a_threshold_on_route_faults():
    """Fault frequency depends on Microsoft's weather, so any threshold becomes a wolf-cry.
    The checkpoint already prints the count; printing is enough."""
    import inspect
    src = inspect.getsource(B)
    assert "fault" not in src.lower() or "MAX_FAULT" not in src


def test_it_does_not_replace_the_exact_identity_the_checkpoint_already_holds():
    """`tab == fell_back` is an equality, not an estimate. Adding a statistical threshold
    beside an exact identity is a downgrade."""
    from scripts.win import checkpoint  # noqa: F401  -- import guard only
    text = open(os.path.join(ROOT, "scripts", "win", "checkpoint.py"), encoding="utf-8").read()
    assert "every tab was a fallback" in text


def test_the_checkpoint_actually_asks(tmp_path):
    """A check nobody is obliged to read is the failure this repository keeps rediscovering."""
    text = open(os.path.join(ROOT, "scripts", "win", "checkpoint.py"), encoding="utf-8").read()
    assert "capture_budget" in text and "capture budget kept" in text

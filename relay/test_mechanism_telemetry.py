"""Three states, and a single boolean cannot hold them.

Two independent reviews of the mechanism-usage data reached the same verdict: nothing in this
system has had a valid efficacy test, because everything was accepted against `outcome ==
DONE` -- a self-report whose measured precision is 71.8%. Reading the low usage numbers as
"the mechanisms do not work" repeats that error with a different label.

What makes the difference measurable is separating:
    never configured on / on but no opportunity / fired and changed nothing
"""
import io
import json

import pytest

from relay import mechanism_telemetry as MT


@pytest.fixture
def log(tmp_path, monkeypatch):
    p = tmp_path / "mechanisms.jsonl"
    monkeypatch.setattr(MT, "LOG", str(p))
    return p


def test_a_step_below_a_false_step_is_null_not_false(log):
    """None means 'never reached'; False means 'reached, and the answer was no'.

    Collapsing them is how a mechanism that is switched OFF comes to look like one that ran
    and did nothing -- which is the reading the whole measurement turned on."""
    MT.record("panel", configured=False, config_source="default", config_value=0)
    row = json.loads(io.open(log, encoding="utf-8").read().strip())
    assert row["configured"] is False
    assert row["eligible"] is None and row["triggered"] is None
    assert row["executed"] is None and row["changed_decision"] is None


def test_the_funnel_says_where_it_stopped(log):
    """The interesting number is the step it stops at, not the total."""
    MT.record("panel", configured=False)
    MT.record("refuter", configured=True, eligible=True, triggered=True,
              executed=True, changed_decision=False)
    MT.record("bestofn", configured=True, eligible=False,
              ineligible_reason="candidates did not differ in correctness")
    f = MT.funnel(MT.load(str(log)))
    assert f["panel"]["stops_at"] == "never configured"
    assert f["bestofn"]["stops_at"] == "no opportunity"
    assert f["refuter"]["stops_at"] == "changed nothing"


def test_every_record_carries_the_join_keys_to_the_grader(log):
    """The most expensive part of the analysis that led here was reconstructing which attempt
    produced which patch. A hash and a self-reported outcome on every record turn that into a
    join."""
    MT.record("retry", configured=True, eligible=True, triggered=True, executed=True,
              self_report_outcome="DONE", artifact_hash=MT.patch_hash("diff --git a b"))
    row = json.loads(io.open(log, encoding="utf-8").read().strip())
    assert row["self_report_outcome"] == "DONE"
    assert row["artifact_hash"] and len(row["artifact_hash"]) == 16


def test_telemetry_never_raises_into_the_run(monkeypatch):
    """A tool call must not fail over bookkeeping, and neither must a turn."""
    monkeypatch.setattr(MT, "LOG", "Z:\\nonexistent\\path\\x.jsonl")
    MT.record("retry", configured=True)   # must not raise


def test_absence_of_a_record_is_not_evidence_of_not_firing():
    """Stated in the module, because the alternative reading is the one that produced two
    years of accepting a self-report."""
    import inspect
    src = inspect.getsource(MT)
    assert "absence of a record" in src or "logging failed" in src


def test_every_named_mechanism_can_be_reported_on(log):
    """A mechanism that never reports should show as a gap, not as an absence nobody saw."""
    f = MT.funnel([])
    assert set(f) == set(MT.MECHANISMS)
    assert all(v["stops_at"] == "never configured" for v in f.values())

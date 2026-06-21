"""Tests for the unified self-improvement status view. Run: python -m relay.selfimprove.test_status"""
import os
import tempfile

from relay.selfimprove import status as S
from relay.selfimprove import dashboard as D
from relay.selfimprove import calibration as C
from relay.selfimprove import targeting as T


def test_status_text_has_three_sections():
    txt = S.status_text()
    assert isinstance(txt, str) and txt
    # the three views are present (each renders its own header even with no data)
    assert "SCORECARD" in txt
    assert "MEASURED COMPETENCE" in txt
    assert "NEXT TARGET" in txt
    print("ok test_status_text_has_three_sections")


def test_target_text_handles_no_target():
    # no-target plan renders a clean (none) line, never raises
    out = S._target_text({"target": None, "misses": [], "note": "no weak class"})
    assert "(none)" in out and "no weak class" in out
    print("ok test_target_text_handles_no_target")


def test_target_text_renders_a_target():
    plan = {"target": {"task_class": "sphinx-doc", "pass_at_1": 0.556, "n": 9,
                       "ci_low": 26.7, "ci_high": 81.1, "headroom": 0.344, "reason": "weak"},
            "misses": ["a__a-1", "a__a-2"], "note": "domain-general only"}
    out = S._target_text(plan)
    assert "sphinx-doc" in out and "55.6%" in out and "2 real miss" in out
    print("ok test_target_text_renders_a_target")


def test_never_raises_on_empty_ledgers():
    # point every view at an empty temp dir -> all three degrade cleanly, status_text still composes.
    with tempfile.TemporaryDirectory() as d:
        empty = os.path.join(d, "nope.jsonl")
        # calibration / targeting accept explicit empty paths; dashboard uses defaults but is defensive.
        assert C.render_text(C.calibration_report(empty)) is not None
        assert T.improvement_plan(grade_results_path=empty)["target"] is None
        txt = S.status_text()           # must not raise regardless of live ledger state
        assert isinstance(txt, str) and txt
    print("ok test_never_raises_on_empty_ledgers")


if __name__ == "__main__":
    test_status_text_has_three_sections()
    test_target_text_handles_no_target()
    test_target_text_renders_a_target()
    test_never_raises_on_empty_ledgers()
    print("ALL STATUS TESTS PASSED")

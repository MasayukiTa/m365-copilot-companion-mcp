"""Hermetic unit tests for the regression-aware verify feedback (domain-general, no live WSL,
no dependency on any specific SWE-bench instance's artifacts).

Locks in the generalization for the 'fixes the symptom but drops a config/branch behavior' miss
class: swe_check must split a failing eval into REGRESSIONS (PASS_TO_PASS now failing) vs
still-UNFIXED FAIL_TO_PASS targets, and the banner must instruct the agent to gate its change on
the broken test's condition rather than retry blind.

  .venv\\Scripts\\python.exe -m pytest bench/test_swe_check_feedback.py -q
  (or run directly: .venv\\Scripts\\python.exe bench/test_swe_check_feedback.py)
"""
import importlib.util
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("swe_check", os.path.join(_HERE, "swe_check.py"))
swe_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(swe_check)


class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout


def _patch_report(monkey_report):
    """Replace swe_check.wsl so _verdict_breakdown reads our synthetic report.json instead of WSL."""
    def fake_wsl(script, timeout=1000, capture=False):
        if "report.json" in script:
            return _FakeProc(json.dumps(monkey_report))
        return _FakeProc("")
    swe_check.wsl = fake_wsl


def _report(inst, f2p_pass, f2p_fail, p2p_pass, p2p_fail):
    return {inst: {"tests_status": {
        "FAIL_TO_PASS": {"success": f2p_pass, "failure": f2p_fail},
        "PASS_TO_PASS": {"success": p2p_pass, "failure": p2p_fail},
    }}}


def test_regression_only():
    """A config-branch regression (the sphinx-7738 shape): target fixed, one sibling broke."""
    inst = "demo__lib-1"
    _patch_report(_report(inst, f2p_pass=["t::test_bug"], f2p_fail=[],
                          p2p_pass=["t::test_a", "t::test_b"], p2p_fail=["t::test_flag_branch"]))
    reg, unf = swe_check._verdict_breakdown("agent_demo_lib-1", inst)
    assert reg == ["t::test_flag_branch"], reg
    assert unf == [], unf
    banner = swe_check._mode_banner(reg, unf)
    assert "REGRESSION" in banner
    assert "test_flag_branch" in banner
    assert "CONDITIONAL" in banner and "gate it on the same condition" in banner
    # an unfixed-target line must NOT appear when there are none
    assert "Still-unfixed" not in banner


def test_unfixed_only():
    """The original bug isn't fixed yet; nothing regressed -> no false REGRESSION alarm."""
    inst = "demo__lib-2"
    _patch_report(_report(inst, f2p_pass=[], f2p_fail=["t::test_bug"],
                          p2p_pass=["t::test_a"], p2p_fail=[]))
    reg, unf = swe_check._verdict_breakdown("agent_demo_lib-2", inst)
    assert reg == []
    assert unf == ["t::test_bug"]
    banner = swe_check._mode_banner(reg, unf)
    assert "REGRESSION" not in banner
    assert "Still-unfixed" in banner and "test_bug" in banner


def test_both_modes():
    inst = "demo__lib-3"
    _patch_report(_report(inst, f2p_pass=["t::ok"], f2p_fail=["t::still_broken"],
                          p2p_pass=["t::a"], p2p_fail=["t::regressed"]))
    reg, unf = swe_check._verdict_breakdown("agent_demo_lib-3", inst)
    assert reg == ["t::regressed"] and unf == ["t::still_broken"]
    banner = swe_check._mode_banner(reg, unf)
    assert "REGRESSION" in banner and "Still-unfixed" in banner


def test_resolved_is_empty():
    """A passing instance must yield no regressions and no unfixed -> empty banner (no noise)."""
    inst = "demo__lib-4"
    _patch_report(_report(inst, f2p_pass=["t::test_bug"], f2p_fail=[],
                          p2p_pass=["t::a", "t::b"], p2p_fail=[]))
    reg, unf = swe_check._verdict_breakdown("agent_demo_lib-4", inst)
    assert reg == [] and unf == []
    assert swe_check._mode_banner(reg, unf) == ""


def test_missing_report_is_safe():
    """If the report can't be read, degrade to ([], []) -- never break the verify flow."""
    def fake_wsl(script, timeout=1000, capture=False):
        return _FakeProc("")          # empty -> json.loads fails -> caught
    swe_check.wsl = fake_wsl
    reg, unf = swe_check._verdict_breakdown("agent_x", "x__y-1")
    assert reg == [] and unf == []


def test_truncation_caps_at_8():
    inst = "demo__lib-5"
    many = ["t::r%02d" % i for i in range(12)]
    _patch_report(_report(inst, f2p_pass=[], f2p_fail=[], p2p_pass=[], p2p_fail=many))
    reg, _ = swe_check._verdict_breakdown("agent_demo_lib-5", inst)
    banner = swe_check._mode_banner(reg, [])
    assert "... and 4 more" in banner          # 12 - 8 shown
    assert banner.count("  t::r") == 8


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
            passed += 1
        except Exception:
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print("\n%d/%d passed" % (passed, len(fns)))
    raise SystemExit(0 if passed == len(fns) else 1)

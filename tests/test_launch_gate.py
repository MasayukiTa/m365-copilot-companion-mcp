"""A run must not begin on a stack whose state would make its results untrue.

The detector already existed. It wrote a breach at 22:40 and runs were launched on top of it
for the next nine and a half hours, so every memory figure in that window carried a leaked
Copilot page -- 341 MB median for the browser without one, 697 MB with one, across 4,549
samples of which 61.6% had one. That is not a detection failure. It is an obligation failure,
and a gate is the obligation.
"""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _runner():
    spec = importlib.util.spec_from_file_location(
        "fleet_runner_gate", os.path.join(ROOT, "relay", "fleet_runner.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _checkpoint():
    spec = importlib.util.spec_from_file_location(
        "checkpoint_gate", os.path.join(ROOT, "scripts", "win", "checkpoint.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_only_the_named_invariants_can_stop_a_launch():
    """A gate that refuses on everything the screen reports gets bypassed by habit and then
    deleted. Memory near a ceiling and an already-closed route are worth an eye, not a
    refusal."""
    rn = _runner()
    assert rn.BLOCKING_INVARIANTS == ("no idle Copilot page", "no browser window")


def test_a_clean_stack_produces_no_blockers(monkeypatch):
    rn = _runner()
    monkeypatch.setattr(rn, "_launch_blockers", lambda: [])
    assert rn._launch_blockers() == []


def test_a_leaked_page_blocks(monkeypatch):
    """The nine-and-a-half-hour page, arriving at a launch."""
    rn = _runner()
    cp = _checkpoint()
    monkeypatch.setattr(cp, "verdicts_now", lambda: (
        [("no browser window", True, "headed: none"),
         ("no idle Copilot page", False, "copilot pages: {'companion': 1}"),
         ("every tab was a fallback", True, ""),
         ("tab audit clean", True, "")], {}))

    import sys
    sys.modules["checkpoint"] = cp
    monkeypatch.setattr(rn, "_launch_blockers",
                        lambda: [(n, d) for n, ok, d in cp.verdicts_now()[0]
                                 if not ok and n in rn.BLOCKING_INVARIANTS])
    blockers = rn._launch_blockers()
    assert [n for n, _ in blockers] == ["no idle Copilot page"]


def test_a_headed_browser_blocks(monkeypatch):
    """A capture opens a tab; in a headed browser that raises a window onto the user's screen,
    which is the one thing they asked never to happen again."""
    rn = _runner()
    cp = _checkpoint()
    monkeypatch.setattr(cp, "verdicts_now", lambda: (
        [("no browser window", False, "headed: copilot-bridge-edge"),
         ("no idle Copilot page", True, "")], {}))
    monkeypatch.setattr(rn, "_launch_blockers",
                        lambda: [(n, d) for n, ok, d in cp.verdicts_now()[0]
                                 if not ok and n in rn.BLOCKING_INVARIANTS])
    assert [n for n, _ in rn._launch_blockers()] == ["no browser window"]


def test_a_non_blocking_breach_does_not_stop_a_launch(monkeypatch):
    """Route already closed is a real finding and not a reason to refuse work."""
    rn = _runner()
    cp = _checkpoint()
    monkeypatch.setattr(cp, "verdicts_now", lambda: (
        [("every tab was a fallback", False, "route closed"),
         ("no idle Copilot page", True, ""), ("no browser window", True, "")], {}))
    monkeypatch.setattr(rn, "_launch_blockers",
                        lambda: [(n, d) for n, ok, d in cp.verdicts_now()[0]
                                 if not ok and n in rn.BLOCKING_INVARIANTS])
    assert rn._launch_blockers() == []


def test_a_gate_that_cannot_ask_does_not_block(monkeypatch):
    """A crashing gate is worse than none: it turns "the stack is dirty" into "no run can
    start, for a reason nobody can see"."""
    rn = _runner()
    monkeypatch.setattr(rn, "os", rn.os)

    def boom(*a, **k):
        raise RuntimeError("no checkpoint here")

    import importlib.util as ilu
    monkeypatch.setattr(ilu, "spec_from_file_location", boom)
    assert rn._launch_blockers() == []


def test_the_gate_can_be_overridden():
    """A gate with no way past it is a gate somebody eventually deletes."""
    src = open(os.path.join(ROOT, "relay", "fleet_runner.py"), encoding="utf-8").read()
    assert '"--force"' in src
    assert 'getattr(args, "force", False)' in src


def test_the_gate_runs_after_the_cleanup_and_before_any_worker():
    """Order is the point: it must judge what could not be fixed, not what needed tidying,
    and it must do so before a single worker starts."""
    src = open(os.path.join(ROOT, "relay", "fleet_runner.py"), encoding="utf-8").read()
    cleanup = src.index("_close_idle_copilot_pages(context)")
    gate = src.index("_launch_blockers()", cleanup)
    start = src.index("run_relay_fleet(context", cleanup)
    assert cleanup < gate < start

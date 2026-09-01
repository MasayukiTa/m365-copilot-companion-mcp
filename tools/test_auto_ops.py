# -*- coding: utf-8 -*-
"""The model-facing door to the atomic edit-verify cell, and the gates on it.

THE RISK THIS FILE IS ABOUT. edit_and_verify runs an ARBITRARY COMMAND. A new tool that runs
commands without the screening shell_exec has is the cheapest possible way around that
screening: a caller refused `shell_exec("rm -rf ...")` could pass the same string as a
verification command. A guarded system stops being guarded one unguarded entry point at a time,
and this repository has watched that happen -- an MFA gate on one branch and every other route
straight past it.
"""
import json

import pytest

from tools import auto_ops as O


@pytest.fixture(autouse=True)
def unlocked(monkeypatch):
    monkeypatch.setattr(O, "require_unlocked", lambda: None)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("N = 1\n", encoding="utf-8")
    from tools.auto import autoloop as A
    monkeypatch.setattr(A, "stop_check", lambda: "RUN")
    return tmp_path


EDITS = [{"path": "a.py", "old": "N = 1", "new": "N = 2"}]


# -- the command screening -----------------------------------------------------------------

def test_a_destructive_verification_command_is_screened(monkeypatch, tree):
    """THE BYPASS THIS CLOSES. The verify command is a command; it goes through the same net."""
    seen = {}
    from tools import contract_gate as CG
    monkeypatch.setattr(CG, "destructive_shell", lambda c: seen.setdefault("cmd", c) or True)
    monkeypatch.setattr(CG, "check_op", lambda op, detail: "[refused: %s]" % op)
    out = O.edit_and_verify(EDITS, verify_command="rm -rf /", repo=str(tree))
    assert out.startswith("[refused: shell_destructive]")
    assert seen["cmd"] == "rm -rf /"
    assert (tree / "a.py").read_text(encoding="utf-8") == "N = 1\n", "edits ran despite refusal"


def test_the_review_judge_can_refuse_it_too(monkeypatch, tree):
    """The second half of shell_exec's screening, not just the deterministic half."""
    import tools.code_exec as CE
    monkeypatch.setattr(CE, "_judged", lambda kind, cmd, wd: "[held for review]")
    out = O.edit_and_verify(EDITS, verify_command="pytest -x", repo=str(tree))
    assert out == "[held for review]"
    assert (tree / "a.py").read_text(encoding="utf-8") == "N = 1\n"


def test_the_screening_is_the_same_code_shell_exec_uses():
    """SOURCE-LEVEL, stated as such. Reimplementing the net here would let the copy drift, and
    a drifted copy of a safety net is worse than an obvious absence."""
    import inspect
    src = inspect.getsource(O._screen)
    assert "from tools.code_exec import _gate_detail, _judged" in src
    assert "destructive_shell" in src and "check_op" in src


def test_no_command_means_nothing_to_screen(tree):
    """A compile-only run executes nothing, and must not be blocked as though it did."""
    out = json.loads(O.edit_and_verify(EDITS, repo=str(tree)))
    assert out["ok"] is True
    assert "no verification command" in out["stage"]


# -- the unlock gate -----------------------------------------------------------------------

@pytest.mark.parametrize("fn,args", [
    ("edit_and_verify", (EDITS,)),
    ("restore_point", ()),
    ("roll_back", ({"ok": True, "kind": "git", "repo": ".", "head": "x"},)),
])
def test_every_mutating_entry_point_is_gated(monkeypatch, fn, args):
    """Skill trust and tool convenience never widen execution rights. These write files and run
    commands, so they sit behind the same gate as everything else that does."""
    monkeypatch.setattr(O, "require_unlocked", lambda: "[locked: no]")
    assert getattr(O, fn)(*args) == "[locked: no]"


def test_reading_the_trajectory_is_not_gated():
    """Reading back what already happened changes nothing. Gating it would only mean the record
    is unreadable exactly when someone is trying to work out what went wrong."""
    out = json.loads(O.loop_trajectory("no-such-run"))
    assert out["iterations"] == 0


# -- shape and failure ----------------------------------------------------------------------

def test_it_returns_json_a_caller_can_act_on(tree):
    out = json.loads(O.edit_and_verify(EDITS, repo=str(tree)))
    for key in ("ok", "stage", "reverted", "files", "restore_failed"):
        assert key in out


def test_an_empty_edit_list_is_refused_with_a_reason(tree):
    assert "non-empty list" in O.edit_and_verify([], repo=str(tree))
    assert "non-empty list" in O.edit_and_verify("not a list", repo=str(tree))


def test_roll_back_rejects_something_that_is_not_a_restore_point():
    assert "pass the dict returned by restore_point" in O.roll_back("HEAD~1")


def test_an_internal_error_is_reported_not_raised(monkeypatch, tree):
    from tools.auto import autoloop as A
    monkeypatch.setattr(A, "edit_and_verify",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert O.edit_and_verify(EDITS, repo=str(tree)).startswith("[edit_and_verify error: RuntimeError")


def test_a_failing_verification_reverts_through_this_door_too(tree):
    """The property is the mechanism's, but it has to survive the wrapper -- that is where a
    forgotten argument would quietly turn revert off."""
    out = json.loads(O.edit_and_verify(EDITS, verify_command="python -c \"raise SystemExit(1)\"",
                                       repo=str(tree)))
    assert out["ok"] is False and out["reverted"] is True
    assert (tree / "a.py").read_text(encoding="utf-8") == "N = 1\n"


# -- the loop, which was reported as done and did not exist ---------------------------------

def test_the_loop_is_gated_and_screens_its_verification_command(monkeypatch, tree):
    """Same door, same gates. A loop that runs an arbitrary command N times without the
    screening would be a worse bypass than the single-shot cell, not a better one."""
    monkeypatch.setattr(O, "require_unlocked", lambda: "[locked: no]")
    assert O.loop_until_verified([EDITS]) == "[locked: no]"


def test_the_loops_verification_command_goes_through_the_same_net(monkeypatch, tree):
    from tools import contract_gate as CG
    monkeypatch.setattr(CG, "destructive_shell", lambda c: True)
    monkeypatch.setattr(CG, "check_op", lambda op, detail: "[refused: %s]" % op)
    out = O.loop_until_verified([EDITS], verify_command="rm -rf /", repo=str(tree))
    assert out.startswith("[refused: shell_destructive]")


def test_running_out_of_iterations_is_reported_as_not_converged(tree):
    """THE DISTINCTION THE CELL COULD NOT MAKE, because the cell has no notion of iterations.
    Exhausting the budget and passing are different facts.

    Two rounds are supplied for a budget of two: with only one candidate the loop would run out
    of CANDIDATES first, which is a different outcome and is tested below."""
    rounds = [EDITS, [{"path": "a.py", "old": "N = 1", "new": "N = 3"}]]
    out = json.loads(O.loop_until_verified(
        rounds, verify_command="python -c \"raise SystemExit(1)\"", repo=str(tree), max_iter=2))
    assert out["converged"] is False
    assert out["stop"] == "max_iter"
    assert "did NOT converge" in out["summary"]


def test_running_out_of_candidates_is_a_different_outcome(tree):
    """`stuck` and `max_iter` are not the same fact: one means we had nothing left to try, the
    other means we were still trying when the budget ran out."""
    out = json.loads(O.loop_until_verified(
        [EDITS], verify_command="python -c \"raise SystemExit(1)\"", repo=str(tree), max_iter=4))
    assert out["stop"] == "stuck"
    assert out["converged"] is False


def test_a_passing_round_converges_and_says_so(tree):
    out = json.loads(O.loop_until_verified(
        [EDITS], verify_command="python -c \"\"", repo=str(tree), max_iter=3))
    assert out["converged"] is True and out["stop"] == "converged"


def test_an_empty_round_list_is_refused_with_a_reason(tree):
    assert "non-empty list" in O.loop_until_verified([], repo=str(tree))

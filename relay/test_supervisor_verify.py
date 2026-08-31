# -*- coding: utf-8 -*-
"""DONE is produced here, by running the contract's own commands, or it is not produced.

WHY STEP 3 WAS NOT ENOUGH. evidence_manifest reads what the worker HAPPENED to run. That catches
the accident -- a test from before the last edit, a DONE with no write -- and cannot catch a
choice: run the right command at the right moment, then edit once more, and the record reads
correctly. The supervisor runs the commands itself, after the worker stops, against a tree whose
state it hashed.

The rules these tests hold:

  * a claim is a CANDIDATE until something independent says otherwise
  * a broken check is not a pass
  * a tree that moved during verification proves nothing about any state
  * the verifier never repairs what it is verifying
"""
import os

import pytest

from relay import supervisor_verify as SV


CONTRACT = {"checks": [{"id": "unit", "command": "pytest -x"}], "cwd": ""}


def runner(results):
    """A fake command runner. The DECISION is what must not be wrong; a test that shells out
    is testing the shell."""
    seq = list(results)

    def _run(command):
        return dict(seq.pop(0), command=command)
    return _run


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print('hi')\n", encoding="utf-8")
    return str(tmp_path)


# ── promotion ─────────────────────────────────────────────────────────────────────────────

def test_a_passing_check_promotes(tree):
    v = SV.verify(dict(CONTRACT, cwd=tree), runner=runner([{"ok": True}]))
    assert v["state"] == SV.DONE
    assert SV.promote(True, v) == SV.DONE


def test_a_failing_check_does_not(tree):
    v = SV.verify(dict(CONTRACT, cwd=tree), runner=runner([{"ok": False}]))
    assert v["state"] == SV.VERIFY_FAILED
    assert SV.promote(True, v) == SV.VERIFY_FAILED


def test_every_check_must_pass(tree):
    c = {"checks": [{"id": "a", "command": "x"}, {"id": "b", "command": "y"}], "cwd": tree}
    v = SV.verify(c, runner=runner([{"ok": True}, {"ok": False}]))
    assert v["state"] == SV.VERIFY_FAILED
    assert "b" in " ".join(v["reasons"])


# ── the states that must never become DONE ────────────────────────────────────────────────

def test_a_check_that_could_not_run_is_not_a_pass(tree):
    """A missing binary, a timeout, an unreadable tree. The claim stays a candidate."""
    v = SV.verify(dict(CONTRACT, cwd=tree),
                  runner=runner([{"ok": False, "unavailable": True}]))
    assert v["state"] == SV.VERIFY_UNAVAILABLE
    assert SV.promote(True, v) == SV.CANDIDATE_DONE


def test_no_contract_cannot_promote(tree):
    assert SV.promote(True, SV.verify(None, cwd=tree)) == SV.CANDIDATE_DONE


def test_a_task_with_no_mechanical_check_is_not_promoted_by_default(tree):
    """The contract SAYS this cannot be checked by running something. That is a reason not to
    promote automatically, not a reason to wave it through."""
    v = SV.verify({"checks": [], "cwd": tree})
    assert v["state"] == SV.VERIFY_UNAVAILABLE
    assert SV.promote(True, v) == SV.CANDIDATE_DONE


def test_a_missing_working_tree_is_not_a_pass():
    v = SV.verify(dict(CONTRACT, cwd="Z:/definitely/absent"))
    assert v["state"] == SV.VERIFY_UNAVAILABLE


def test_no_claim_produces_no_state(tree):
    assert SV.promote(False, SV.verify(dict(CONTRACT, cwd=tree),
                                       runner=runner([{"ok": True}]))) == ""


# ── the tree has to hold still ────────────────────────────────────────────────────────────

def test_a_tree_that_changes_during_verification_voids_the_result(tree):
    """A check that edits what it checks has signed off on its own work. Whatever it printed,
    it did not describe a single state."""
    def _edit_and_pass(command):
        with open(os.path.join(tree, "src", "a.py"), "a", encoding="utf-8") as fh:
            fh.write("# touched by the check\n")
        return {"ok": True, "command": command}

    v = SV.verify(dict(CONTRACT, cwd=tree), runner=_edit_and_pass)
    assert v["state"] == SV.VERIFY_UNAVAILABLE
    assert "changed during verification" in " ".join(v["reasons"])
    assert SV.promote(True, v) == SV.CANDIDATE_DONE


def test_the_same_tree_hashes_the_same(tree):
    assert SV.tree_hash(tree) == SV.tree_hash(tree)


def test_any_edit_changes_the_hash(tree):
    before = SV.tree_hash(tree)
    (tmp := os.path.join(tree, "src", "a.py"))
    with open(tmp, "a", encoding="utf-8") as fh:
        fh.write("x = 1\n")
    assert SV.tree_hash(tree) != before


def test_build_output_does_not_count_as_a_change(tree):
    """__pycache__ and node_modules move on their own; if they counted, every verification
    would void itself and the whole step would report UNAVAILABLE forever."""
    before = SV.tree_hash(tree)
    os.makedirs(os.path.join(tree, "__pycache__"), exist_ok=True)
    with open(os.path.join(tree, "__pycache__", "a.pyc"), "w", encoding="utf-8") as fh:
        fh.write("junk")
    os.makedirs(os.path.join(tree, "node_modules", "x"), exist_ok=True)
    with open(os.path.join(tree, "node_modules", "x", "index.js"), "w", encoding="utf-8") as fh:
        fh.write("module.exports = 1")
    assert SV.tree_hash(tree) == before


# ── robustness ────────────────────────────────────────────────────────────────────────────

def test_a_raising_runner_does_not_raise_out(tree):
    def _boom(command):
        raise RuntimeError("the runner exploded")
    v = SV.verify(dict(CONTRACT, cwd=tree), runner=_boom)
    assert v["state"] == SV.VERIFY_UNAVAILABLE
    assert SV.promote(True, v) == SV.CANDIDATE_DONE


def test_the_only_place_done_is_produced():
    """DONE must not be reachable from anything except a passing independent verification --
    otherwise a later caller can mint it, which is where the 0.718 came from."""
    import inspect
    src = inspect.getsource(SV.promote)
    assert src.count("return DONE") == 1
    assert "verification" in src


# ── the tests failed, or the runner never started ─────────────────────────────────────────

@pytest.mark.parametrize("output", [
    "'pytest' is not recognized as an internal or external command",
    "bash: npm: command not found",
    "npm ERR! missing script: test",
    "Error: Cannot find module ... ENOENT",
    "can't open file 'run.py': [Errno 2] No such file or directory",
])
def test_a_runner_that_never_started_is_not_a_failed_test(output):
    """MEASURED WHILE WIRING THIS: an absent worktree made `npm test` exit non-zero, and that
    read as VERIFY_FAILED -- a verdict about code that was never run. "The tests failed" and
    "the test runner never started" are different facts and only the first is evidence."""
    assert SV.not_actually_run(output)


@pytest.mark.parametrize("output", [
    "FAILED tests/test_retry.py::test_backoff - AssertionError",
    "1 failed, 12 passed in 3.2s",
    "Tests: 2 failed, 40 passed",
    "AssertionError: expected 3 got 4",
])
def test_a_genuine_test_failure_is_not_mistaken_for_a_broken_runner(output):
    """THE DIRECTION THAT MATTERS. A pattern here that a real failure could also print would
    turn a real failure into "could not check", which promotes nothing but hides everything."""
    assert not SV.not_actually_run(output)


def test_an_unstartable_check_leaves_the_claim_a_candidate(tree):
    v = SV.verify(dict(CONTRACT, cwd=tree),
                  runner=runner([{"ok": False, "unavailable": True}]))
    assert SV.promote(True, v) == SV.CANDIDATE_DONE
    assert v["state"] == SV.VERIFY_UNAVAILABLE

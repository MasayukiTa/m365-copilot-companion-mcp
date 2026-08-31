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


# ── collection never completed, so nothing was exercised ──────────────────────────────────

def test_a_collection_error_is_unavailable_not_a_failure():
    """MEASURED ON THE BENCHMARK. `pytest -x` in a staged worktree died in 3.5 seconds with
    "ModuleNotFoundError: No module named 'web'" out of the project's own conftest, because the
    repository's dependencies are not installed in the worktree. Every instance was recorded
    VERIFY_FAILED -- a verdict about code that had never been exercised.

    A collection error can also be the patch's own fault, and nothing in the output
    distinguishes the two. UNAVAILABLE neither credits nor condemns it, which is the honest
    answer when the evidence cannot tell them apart."""
    out = ("openlibrary/conftest.py:5: in <module>\n    import web\n"
           "E   ModuleNotFoundError: No module named 'web'\n"
           "!!!! Interrupted: 1 error during collection !!!!\n")
    assert SV.not_actually_run(out) is True


def test_a_conftest_import_error_is_unavailable():
    assert SV.not_actually_run("ImportError while loading conftest '/x/conftest.py'.") is True


def test_a_suite_that_ran_nothing_is_not_a_pass_and_not_a_defect():
    assert SV.not_actually_run("collected 0 items\n\n= no tests ran in 0.31s =") is True


def test_a_real_test_failure_is_still_a_failure():
    """THE LINE THAT MUST NOT MOVE. Widening the unavailable markers until genuine failures
    fall through turns the whole verifier into a machine that never says no."""
    assert SV.not_actually_run("2 failed, 118 passed in 41.2s") is False
    assert SV.not_actually_run(
        "E   AssertionError: assert 3 == 4\n1 failed, 9 passed in 2.1s") is False


def test_an_import_error_inside_a_test_is_still_a_failure():
    """An ImportError raised while a test RUNS is a result about the patch. Only a failure
    during collection means nothing ran."""
    assert SV.not_actually_run(
        "test_x.py::test_thing FAILED\nE   ImportError: cannot import name 'foo'\n"
        "1 failed, 3 passed in 1.4s") is False


def test_an_unavailable_check_cannot_promote():
    """The taxonomy, end to end: a check that could not run must not become DONE."""
    contract = {"checks": [{"id": "project_tests", "command": "pytest -x"}], "cwd": "."}
    v = SV.verify(contract, cwd=".",
                  runner=lambda c: {"command": c, "ok": False, "unavailable": True,
                                    "output": "1 error during collection", "duration_s": 3.5})
    assert v["state"] == SV.VERIFY_UNAVAILABLE
    assert SV.promote(True, v) != SV.DONE


# ── where the ledger settles what the runner could not ────────────────────────────────────

UNAVAIL = {"state": SV.VERIFY_UNAVAILABLE}
CONTRA = {"verdict": "CONTRADICTED", "reasons": ["claimed DONE with no successful write"]}


def test_evidence_settles_a_claim_when_nothing_could_be_run():
    """THE GAP THIS CLOSES. In a staged worktree the acceptance command cannot run at all, so
    the ledger is the only signal there is -- and it was being collected and discarded. One log
    line said CONTRADICTED and the next said CANDIDATE_DONE, about the same task."""
    assert SV.promote(True, UNAVAIL, CONTRA) == SV.EVIDENCE_CONTRADICTED


def test_it_is_still_not_a_failure_because_nothing_was_executed():
    """Weaker than VERIFY_FAILED on purpose. No command ran, so no command failed."""
    assert SV.EVIDENCE_CONTRADICTED != SV.VERIFY_FAILED
    assert SV.EVIDENCE_CONTRADICTED != SV.DONE


def test_a_passing_independent_run_outranks_the_ledger():
    """If the contract's own commands passed against the finished tree, the work is done
    whatever the ledger looks like. Execution beats bookkeeping."""
    assert SV.promote(True, {"state": SV.DONE}, CONTRA) == SV.DONE


def test_a_failing_independent_run_stands_on_its_own():
    assert SV.promote(True, {"state": SV.VERIFY_FAILED}, CONTRA) == SV.VERIFY_FAILED


def test_supporting_evidence_never_promotes_anything():
    """Tool calls show what was ATTEMPTED, not that it was right. Treating them as proof would
    rebuild the self-report this whole pipeline exists to stop believing."""
    supported = {"verdict": "SUPPORTED", "reasons": ["ran the acceptance command"]}
    assert SV.promote(True, UNAVAIL, supported) == SV.CANDIDATE_DONE


def test_unverifiable_evidence_changes_nothing():
    assert SV.promote(True, UNAVAIL, {"verdict": "UNVERIFIABLE"}) == SV.CANDIDATE_DONE
    assert SV.promote(True, UNAVAIL, None) == SV.CANDIDATE_DONE
    assert SV.promote(True, UNAVAIL, {}) == SV.CANDIDATE_DONE


def test_no_claim_means_no_state_whatever_the_evidence_says():
    """Nothing to contradict. A worker that never claimed DONE is not being judged."""
    assert SV.promote(False, UNAVAIL, CONTRA) == ""


def test_the_cycle_passes_the_evidence_in():
    """SOURCE-LEVEL, stated as such: the cycle stages repositories and runs a fleet. This
    catches the wiring being dropped, which is how the signal came to be discarded before."""
    import io
    import os as _os
    repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    src = io.open(_os.path.join(repo, "bench", "pro_cycle.py"), encoding="utf-8").read()
    assert "SV.promote(inst in claimed, v, _EVIDENCE_BY_INSTANCE.get(inst))" in src

# -*- coding: utf-8 -*-
"""The terms a task was accepted under, fixed before the task starts.

WHY. The only thing between a worker and a DONE today is the worker's own judgement that it
finished, and that judgement measures 0.718 -- 11 of 39 claims wrong on a 40-instance slice. A
worker that picks its own acceptance test after the fact picks one it passes. That is not
dishonesty; it is what "check whether you are finished" means when the checker and the checked
are the same process.

These tests hold three properties, and the second and third are the ones that make it worth
having at all:

  * the contract exists BEFORE the first turn
  * it cannot be changed afterwards
  * "nobody wrote a check" and "this task has no mechanical oracle" are different states
"""
import json

import pytest

from relay import acceptance_contract as AC


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "contracts.jsonl")


# ── written first, and hashed ─────────────────────────────────────────────────────────────

def test_a_contract_records_the_commands_the_controller_chose(path):
    c = AC.ensure("t1", goal="fix the retry loop",
                  checks=[{"id": "unit", "command": "pytest -x tests/test_retry.py"}], path=path)
    assert c["checks"][0]["command"] == "pytest -x tests/test_retry.py"
    assert c["verifiable"] is True
    assert AC.intact(c)


def test_the_hash_covers_the_commands(path):
    c = AC.ensure("t1", checks=["pytest -x"], path=path)
    tampered = dict(c)
    tampered["checks"] = [{"id": "c1", "command": "true", "expect": "exit_zero"}]
    assert not AC.intact(tampered), "the terms were changed and the hash still matched"


def test_a_hand_edited_contract_is_detected(path):
    """Not a security boundary -- anyone who can edit the file can recompute the hash. What it
    catches is the accident: a hand edit, a torn line, a refactor that altered tasks in flight."""
    AC.ensure("t1", checks=["pytest -x"], path=path)
    rows = [json.loads(l) for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]
    rows[0]["cwd"] = "somewhere/else"
    assert not AC.intact(rows[0])


# ── it cannot be changed afterwards ───────────────────────────────────────────────────────

def test_a_second_contract_cannot_replace_the_first(path):
    """A contract that could be rewritten once the work started is not a contract."""
    first = AC.ensure("t1", checks=["pytest -x tests/hard.py"], path=path)
    AC.record(AC.build("t1", checks=["true"]), path)          # a later, easier one
    assert AC.load("t1", path)["checks"][0]["command"] == "pytest -x tests/hard.py"
    assert AC.load("t1", path)["hash"] == first["hash"]


def test_re_admission_returns_the_same_terms(path):
    """Admission is retried -- a re-queued goal, a resumed run -- and a retry must not change
    what the task was accepted under."""
    a = AC.ensure("t1", checks=["pytest -x"], path=path)
    b = AC.ensure("t1", checks=["echo definitely-passes"], path=path)
    assert a["hash"] == b["hash"]
    assert b["checks"][0]["command"] == "pytest -x"


def test_the_creation_time_is_recorded(path):
    """A verifier has to be able to say a test ran AFTER the final edit. That comparison needs
    the contract's own clock, not the worker's account of the order things happened in."""
    c = AC.ensure("t1", checks=["pytest -x"], path=path, ts=1788160000.0)
    assert c["created_ts"] == 1788160000.0


# ── the distinction that stops an omission passing as a fact ──────────────────────────────

def test_a_task_with_no_mechanical_oracle_says_so(path):
    """Plenty of real work -- a summary, an investigation, a question -- cannot be checked by
    running something. That is a fact about the task."""
    c = AC.ensure("t1", goal="このリポジトリの構成を3行で説明して", checks=[], path=path)
    assert c["verifiable"] is False
    assert c["checks"] == []
    assert AC.intact(c)


def test_no_contract_is_not_the_same_as_an_unverifiable_one(path):
    """THE DISTINCTION THAT MATTERS. An absent contract is an omission at admission; a contract
    saying `verifiable: false` is a decision. Collapsing them makes every un-set-up task look
    like a task with nothing to check, which passes."""
    AC.ensure("has-one", checks=[], path=path)
    assert AC.load("has-one", path) is not None
    assert AC.load("never-admitted", path) is None
    assert AC.missing_contract_tasks(["has-one", "never-admitted"], path) == ["never-admitted"]


# ── shape and robustness ──────────────────────────────────────────────────────────────────

def test_a_bare_string_check_is_accepted_and_normalised(path):
    c = AC.ensure("t1", checks=["pytest -x", "  ", ""], path=path)
    assert len(c["checks"]) == 1
    assert c["checks"][0]["id"] and c["checks"][0]["expect"] == "exit_zero"


def test_the_goal_is_referenced_by_digest_not_copied(path):
    """The goal is already stored 15 times per task elsewhere in this system; a contract does
    not need a sixteenth copy. The digest is enough to say which goal it was."""
    goal = "x" * 5000
    c = AC.ensure("t1", goal=goal, checks=["pytest -x"], path=path)
    assert len(json.dumps(c)) < 1000
    assert len(c["goal_sha16"]) == 16


def test_the_same_contract_hashes_the_same_twice(path):
    a = AC.build("t1", goal="g", checks=["pytest -x"], cwd="/w", ts=1.0)
    b = AC.build("t1", goal="g", checks=["pytest -x"], cwd="/w", ts=1.0)
    assert a["hash"] == b["hash"]


def test_a_torn_line_does_not_hide_a_later_contract(path):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"task": "t1", "hash": "x"\n')
    AC.ensure("t2", checks=["pytest -x"], path=path)
    assert AC.load("t2", path) is not None


def test_writing_never_raises_on_an_impossible_path():
    assert AC.record(AC.build("t1", checks=["pytest -x"]), "Z:/nope/x.jsonl") is False


def test_loading_a_missing_file_is_none_not_an_error(tmp_path):
    assert AC.load("t1", str(tmp_path / "absent.jsonl")) is None

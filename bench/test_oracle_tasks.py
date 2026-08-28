"""The entry condition for a benchmark task: its answer is checkable without the worker.

Every headline number this project produced before this was an internal event -- a procedure
performed, a DONE written, a refuter's verdict, a selector nothing calls. An external review
named the error: the unit of progress had become a measurable internal event rather than an
externally verified correct answer.
"""
import pytest

from bench.oracle_suite_repo import tasks, truths
from bench.oracle_tasks import Task, contract_card


def test_every_task_passes_on_the_truth_and_fails_on_a_wrong_number():
    """The minimum an oracle must do. One that cannot fail is not an oracle."""
    t = truths()
    for task in tasks():
        assert task.grade("答えは %d です\nDONE" % t[task.tid])["passed"] is True, task.tid
        assert task.grade("答えは 99999 です\nDONE")["passed"] is False, task.tid


def test_an_answer_with_no_number_fails_rather_than_passing_vacuously():
    for task in tasks():
        assert task.grade("調べました。")["passed"] is False, task.tid


def test_an_oracle_that_raises_is_not_a_pass():
    """A broken check must never read as a clean run -- that is the direction that flatters."""
    boom = Task("t", "g", "c", lambda a: (_ for _ in ()).throw(RuntimeError("x")))
    r = boom.grade("anything")
    assert r["passed"] is False and "oracle raised" in r["detail"]


def test_the_contract_arm_and_the_control_differ_only_by_the_card():
    """An arm that also changes the question is measuring two things at once. The previous
    experiment did exactly that and its result could not be attributed."""
    task = tasks()[0]
    control = task.full_goal(with_contract=False)
    contract = task.full_goal(with_contract=True)
    assert contract.startswith(control)
    assert contract[len(control):].strip() == task.contract.strip()


def test_the_card_states_what_is_wanted_not_how_to_do_it():
    """NOT A PROCEDURE. The last experiment appended a procedure instruction and then scored
    whether the answer looked like the procedure, which measured nothing about correctness --
    and gave zero to the most checkable answer of thirty runs because it reached the right
    result by a shorter route."""
    card = contract_card("件数", "期間内のみ", "原データから再計算")
    for banned in ("skill_match", "skill_load", "手順どおり", "分割して", "ページング"):
        assert banned not in card
    assert "出すもの" in card and "照合方法" in card


def test_a_claimed_total_is_read_rather_than_any_number_in_the_text():
    """An answer that lists many numbers must not pass by accident -- a loose 'contains' check
    would let a wrong total through whenever the right digits appear anywhere."""
    task = [t for t in tasks() if t.tid == "count_outcomes"][0]
    truth = truths()["count_outcomes"]
    listing = "DONE, FANOUT, MAXTURNS ... 全部で %d 個" % (truth + 5)
    assert task.grade(listing)["passed"] is False

"""Tasks whose correct answer can be established WITHOUT asking the worker.

WHY THIS FILE EXISTS. Every headline number this project produced was an internal event:
whether a procedure was performed, whether the worker wrote DONE, what a refuter said, what a
selector that nothing calls would have chosen. An external review named the error plainly --
the unit of progress had become "a measurable internal event" rather than "an externally
verified correct answer" -- and it was right. `outcome == "DONE"` is the worker reporting that
it finished; it was reported as an accuracy floor of 0.931 that other mechanisms would have to
beat, and it is not one.

So a task belongs here only if its answer can be checked by something the worker does not
control. That is the whole entry condition.

WHAT AN ORACLE MAY BE:
  * a recomputation from the source data, done here rather than by the worker
  * a read-after-write: the worker changed something, and we read the thing back
  * an executed test
  * a value that is a fact about the environment and can simply be looked up

WHAT AN ORACLE MAY NOT BE:
  * the worker's own report that it is done, or correct, or that it checked
  * a judgement about whether the answer LOOKS like the right procedure -- the previous
    grader did exactly that, and scored the single most checkable answer of thirty runs at
    zero because it reached the right result without performing an unnecessary ritual

A task that cannot be graded this way is not a bad task; it simply is not a benchmark task,
and putting it in a benchmark is how a suite starts measuring its own vocabulary.
"""
from __future__ import annotations

import json
import os


class Task:
    """One benchmark item: what to ask, and how to check the answer independently.

    `contract` is the deliverable stated to the worker -- what is wanted, under which
    constraints, in what form. It is frozen before the run: writing it afterwards is how a
    grader ends up describing whatever came back.

    `oracle` is a callable taking the worker's final text and returning
    (passed: bool, detail: str). It must reach its verdict from the environment, never from
    the worker's claims about the environment.
    """

    def __init__(self, tid, goal, contract, oracle, *, kind="general"):
        self.tid = tid
        self.goal = goal
        self.contract = contract
        self.oracle = oracle
        self.kind = kind

    def full_goal(self, with_contract=True):
        """The text sent to the worker. `with_contract` False is the control arm."""
        if not with_contract:
            return self.goal
        return self.goal + "\n\n" + self.contract

    def grade(self, answer):
        try:
            passed, detail = self.oracle(answer or "")
        except Exception as exc:                      # an oracle that raises is not a pass
            return {"passed": False, "detail": "oracle raised: %s: %s"
                                               % (type(exc).__name__, exc)}
        return {"passed": bool(passed), "detail": detail}


def contract_card(deliverable, constraints, evidence):
    """The short block appended to a goal in the contract arm.

    Deliberately NOT a procedure. The previous experiment appended a procedure instruction and
    then scored whether the answer looked like the procedure, which measured nothing about
    correctness. This states WHAT IS WANTED and HOW IT WILL BE CHECKED, and leaves the method
    to the worker -- so a worker that reaches the right answer by a shorter route still passes.
    """
    return ("【この作業の成果契約】\n"
            "- 出すもの: %s\n"
            "- 満たすこと: %s\n"
            "- 照合方法: %s\n"
            "この契約は独立に照合されます。確認していないことを確認済みと書かないでください。"
            % (deliverable, constraints, evidence))

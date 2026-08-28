"""Benchmark tasks over this repository, graded by recomputing the answer here.

WHY THE REPOSITORY. An oracle has to reach its verdict without asking the worker, and the
cheapest honest oracle is a fact this process can compute for itself. Counting files, reading
a closed set out of a module, finding which functions lack a caller -- all of these have one
right answer, obtainable in milliseconds, that no amount of confident prose can change.

They are also tasks the worker CAN get wrong in the ways that matter: by counting the wrong
population, by answering a nearby question, by reporting a number it did not verify. Those are
the failure modes an accuracy benchmark exists to catch, and none of them were visible in the
previous probe, which asked whether an answer LOOKED like a procedure.

NOT A SUBSTITUTE FOR HARD TASKS. These are small and mechanical on purpose: they establish
whether the harness can be measured at all before an expensive suite is spent finding out.
"""
from __future__ import annotations

import glob
import io
import os
import re

from bench.oracle_tasks import Task, contract_card

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _num(answer):
    """Every integer the answer states, in order. The oracle asks whether the RIGHT one is
    present rather than parsing a sentence, because a wrong parse would fail a correct worker
    and that failure would look like a capability finding."""
    return [int(x) for x in re.findall(r"\d+", answer or "")]


def _truth_py_nontest():
    files = glob.glob(os.path.join(REPO, "relay", "*.py"))
    return len([f for f in files if not os.path.basename(f).startswith("test_")])


def _truth_outcomes():
    src = io.open(os.path.join(REPO, "relay", "outcomes.py"), encoding="utf-8").read()
    block = src.split("STATUS_OF = {", 1)[1].split("}", 1)[0]
    return len(re.findall(r'"[A-Z_]+":', block))


def _truth_excludable():
    from relay.outcomes import EXCLUDED_WITHOUT_WORK
    return len(EXCLUDED_WITHOUT_WORK)


def _exact_int(truth):
    """Pass only when the answer states the right integer.

    A LOOSE MATCH IS THE FAILURE MODE HERE: an answer listing many numbers would pass a
    "contains" test by accident, so the check is that the truth appears AND no other integer is
    offered as the total. Answers state the total last in practice, so the last integer is the
    claim; when there is only one integer, that is the claim too.
    """
    def check(answer):
        nums = _num(answer)
        if not nums:
            return False, "no integer in the answer"
        # The claim is the integer nearest a total-word, else the last one stated.
        m = re.findall(r"(?:合計|全部で|計|は)\s*(\d+)\s*(?:個|件|本|つ|ファイル)?", answer or "")
        claim = int(m[-1]) if m else nums[-1]
        return claim == truth, "claimed %d, truth %d" % (claim, truth)
    return check


def tasks():
    """The suite. Truths are computed at call time so an edit to the repo cannot stale them."""
    t_py = _truth_py_nontest()
    t_out = _truth_outcomes()
    t_exc = _truth_excludable()
    repo = REPO.replace("\\", "/")
    return [
        Task(
            "count_nontest_py",
            "リポジトリ %s の relay フォルダ直下にある .py ファイルのうち、"
            "ファイル名が test_ で始まらないものが何個あるか数えてください。"
            "サブフォルダは含めません。最後の行に DONE と書いてください。" % repo,
            contract_card(
                "個数(整数1つ)",
                "relay 直下のみ・サブフォルダ除外・test_ で始まる名前は除外",
                "こちらで同じ条件を再計算して突き合わせます"),
            _exact_int(t_py),
            kind="count"),
        Task(
            "count_outcomes",
            "リポジトリ %s の relay/outcomes.py にある STATUS_OF の要素数(結末の種類の数)を"
            "答えてください。最後の行に DONE と書いてください。" % repo,
            contract_card(
                "要素数(整数1つ)",
                "STATUS_OF の要素のみ・他の集合(RETRYABLE 等)は数えない",
                "こちらでソースから同じ数を取り出して突き合わせます"),
            _exact_int(t_out),
            kind="count"),
        Task(
            "count_excludable",
            "リポジトリ %s の relay/outcomes.py で、EXCLUDED_WITHOUT_WORK に入っている"
            "結末はいくつありますか。最後の行に DONE と書いてください。" % repo,
            contract_card(
                "個数(整数1つ)",
                "EXCLUDED_WITHOUT_WORK の要素のみ",
                "こちらで同じ定数を読んで突き合わせます"),
            _exact_int(t_exc),
            kind="count"),
    ]


def truths():
    return {"count_nontest_py": _truth_py_nontest(),
            "count_outcomes": _truth_outcomes(),
            "count_excludable": _truth_excludable()}

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


#: The one line an answer must contain for the oracle to read it. Stated in the QUESTION, not
#: in the contract card, so both arms carry it and the arms still differ by the card alone.
ANSWER_LINE = "ANSWER: <整数>"
ANSWER_RE = re.compile(r"^\s*ANSWER\s*[:：]\s*(-?\d+)\s*$", re.MULTILINE)


def _claim(answer):
    """The integer the answer OFFERS as its result, or None.

    PARSING PROSE FOR THE CLAIM WAS THE THIRD INSTRUMENT FAILURE IN THIS PROJECT, and the
    worst, because it inverted a result rather than losing one. The first version looked for a
    number near a total-word and took the last such match. Against the real answer

        「**53個** です。relay 直下の .py ファイルは計135個で、うち test_ で始まるものが
         82個、始まらないものが **53個** です」

    it matched 計135 and reported "claimed 135, truth 53" -- so a CORRECT answer was graded
    wrong, three runs in a row, and written up as "the worker skips the exclusion condition in
    the question". The workers had done exactly what was asked.

    A grader that reads prose is guessing, and its guesses fail silently in whichever
    direction the sentence happens to run. So the answer states its result in one fixed line
    and the oracle reads only that. If the line is absent the task is not graded as wrong --
    it is unparseable, which is a different fact and is returned as one.
    """
    m = ANSWER_RE.findall(answer or "")
    if not m:
        return None
    # The LAST such line: a worker that corrects itself puts the correction after.
    return int(m[-1])


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
    """Pass only when the ANSWER line states the right integer."""
    def check(answer):
        claim = _claim(answer)
        if claim is None:
            return False, "no ANSWER line (unparseable, not necessarily wrong)"
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
            "サブフォルダは含めません。"
            "答えは必ず `ANSWER: <整数>` の1行で書いてください(その行だけを採点します)。"
            "最後の行に DONE と書いてください。" % repo,
            contract_card(
                "個数(整数1つ)",
                "relay 直下のみ・サブフォルダ除外・test_ で始まる名前は除外",
                "こちらで同じ条件を再計算して突き合わせます"),
            _exact_int(t_py),
            kind="count"),
        Task(
            "count_outcomes",
            "リポジトリ %s の relay/outcomes.py にある STATUS_OF の要素数(結末の種類の数)を"
            "答えてください。"
            "答えは必ず `ANSWER: <整数>` の1行で書いてください(その行だけを採点します)。"
            "最後の行に DONE と書いてください。" % repo,
            contract_card(
                "要素数(整数1つ)",
                "STATUS_OF の要素のみ・他の集合(RETRYABLE 等)は数えない",
                "こちらでソースから同じ数を取り出して突き合わせます"),
            _exact_int(t_out),
            kind="count"),
        Task(
            "count_excludable",
            "リポジトリ %s の relay/outcomes.py で、EXCLUDED_WITHOUT_WORK に入っている"
            "結末はいくつありますか。"
            "答えは必ず `ANSWER: <整数>` の1行で書いてください(その行だけを採点します)。"
            "最後の行に DONE と書いてください。" % repo,
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

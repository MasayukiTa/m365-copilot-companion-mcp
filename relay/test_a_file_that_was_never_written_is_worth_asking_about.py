# -*- coding: utf-8 -*-
"""A dropped connection must not bury a goal that demonstrably never ran.

WHAT HAPPENED, 2026-09-14 18:52. A goal reading 「…9月報告分の資料を作ってください。8月までの
pptxと同じようなものを、同じフォルダに出してください」 lost its websocket six minutes in. The
relay refused to re-send it, and the refusal was RIGHT as far as it went: the turn may already
have been delivered, the goal acts, and re-running an act you cannot verify is worse than not
running it. Then nothing happened for the remaining fifty minutes of the hour, and nothing
happened overnight. The queue still held the job in the morning.

Nothing had been written. The question "did the act happen" had an answer sitting on disk the
whole time, and the code that already knew how to ask such questions -- `effect_is_checkable`,
which looks a commit up in `git log` rather than guessing -- did not count a file as a trace it
could read.

WHAT THESE TESTS FIX IN PLACE. The asymmetry that made the original refusal correct: mail leaves
nothing this process can read, so a mail goal stays uncheckable and stays refused. A file does
leave something -- but only one direction of it is proof. Seeing a new file does NOT establish
that this goal's effect completed (a sibling worker writes into the same folder), so that reads
as `unknown` and still refuses. The only verdict that changes behaviour is a folder demonstrably
untouched since the turn was sent, and that one is not a guess.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import relay_fleet, transport_policy  # noqa: E402

PRESENT = transport_policy.CHECK_PRESENT
ABSENT = transport_policy.CHECK_ABSENT
UNKNOWN = transport_policy.CHECK_UNKNOWN


def _worker(goal, sent_at, cwd=None):
    """A real worker instance with nothing running behind it."""
    cls = None
    for name in dir(relay_fleet):
        obj = getattr(relay_fleet, name)
        if isinstance(obj, type) and hasattr(obj, "_file_effect_checker"):
            cls = obj
            break
    assert cls is not None, "no class on relay_fleet defines _file_effect_checker"
    w = cls.__new__(cls)
    w.goal = goal
    w._turn_sent_at = sent_at
    w.cwd = cwd
    return w


GOAL = "過去の資料を参考にして、9月報告分の資料を作ってください。%s に出してください。"


def test_a_folder_untouched_since_the_send_says_the_act_did_not_run(tmp_path):
    folder = tmp_path / "ogf"
    folder.mkdir()
    (folder / "august.pptx").write_bytes(b"old")
    # Everything present predates the send.
    sent_at = time.time() + 5.0
    w = _worker(GOAL % str(folder), sent_at)
    check = w._file_effect_checker()
    assert check is not None, "a goal naming an existing folder gave no checker"
    assert check(w.goal) == ABSENT


def test_a_file_written_after_the_send_stops_the_resend(tmp_path):
    """Something ran. That is not proof THIS goal finished, so the verdict is `unknown` -- and
    `unknown` refuses, which is the answer the code gave before any of this existed."""
    folder = tmp_path / "ogf"
    folder.mkdir()
    sent_at = time.time() - 60.0
    (folder / "september.pptx").write_bytes(b"new")
    w = _worker(GOAL % str(folder), sent_at)
    assert w._file_effect_checker()(w.goal) == UNKNOWN


def test_an_unrelated_write_does_not_license_a_resend(tmp_path):
    """The conservative direction. A sibling worker touching the same folder is indistinguishable
    from this goal having run, so the verdict stays `unknown` and the refusal stands."""
    folder = tmp_path / "ogf"
    folder.mkdir()
    sent_at = time.time() - 60.0
    (folder / "somebody_elses_scratch.json").write_bytes(b"{}")
    w = _worker(GOAL % str(folder), sent_at)
    assert w._file_effect_checker()(w.goal) == UNKNOWN


def test_without_a_send_time_there_is_nothing_to_compare_against(tmp_path):
    """Otherwise the question degrades to "are there files here", which is yes everywhere."""
    folder = tmp_path / "ogf"
    folder.mkdir()
    (folder / "x.pptx").write_bytes(b"x")
    w = _worker(GOAL % str(folder), 0.0)
    assert w._file_effect_checker() is None


def test_a_folder_that_does_not_exist_gives_no_checker(tmp_path):
    w = _worker(GOAL % str(tmp_path / "nope"), time.time())
    assert w._file_effect_checker() is None


def test_a_named_file_stands_for_its_folder(tmp_path):
    """An instruction about a file is an instruction about the folder it goes in -- and the
    output file does not exist yet at the time the instruction is written, which is why the
    folder rather than the file is what gets looked at."""
    folder = tmp_path / "ogf"
    folder.mkdir()
    (folder / "already_here.pptx").write_bytes(b"old")
    sent_at = time.time() + 5.0
    w = _worker("%s に保存してください。" % str(folder / "report.pptx"), sent_at)
    check = w._file_effect_checker()
    assert check is not None, "a goal naming a file in an existing folder gave no checker"
    assert check(w.goal) == ABSENT


def test_mail_stays_uncheckable_and_therefore_stays_refused():
    """The asymmetry that made the original refusal correct, held in place.

    Nothing this process can read says whether a mail went out, so the safe refusal must
    survive every change made for files.
    """
    goal = "取引先に見積書をメールで送信してください。"
    assert transport_policy.resend_decision_for_landed_act(goal, checker=None) \
        == transport_policy.REFUSE


def test_an_output_goal_is_recognised_as_checkable():
    """`effect_is_checkable` is gated on `goal_may_act`, and that ordering is deliberate: a goal
    that does not act never reaches the refuse branch, so its checkability is not a question.
    The English phrasing "save the file into that folder" is NOT currently read as acting, so it
    is not asserted here -- widening `goal_may_act` would change which goals are treated as
    dangerous, which is a bigger decision than this one and does not belong in this change.
    """
    assert transport_policy.goal_may_act(
        "9月報告分の資料を作成して、同じフォルダに出力してください。")
    assert transport_policy.effect_is_checkable(
        "9月報告分の資料を作成して、同じフォルダに出力してください。")


@pytest.mark.parametrize("verdict,expected", [
    (ABSENT, transport_policy.RESEND),
    (UNKNOWN, transport_policy.REFUSE),
])
def test_only_a_demonstrably_absent_effect_licenses_a_resend(verdict, expected):
    """PRESENT is not in this table on purpose.

    The decision function reads `present` as "already in the world, so a re-send is a harmless
    no-op" -- true of a commit subject, false of a file, which a second run writes again. The
    file checker therefore never returns `present`; what it can establish is absence, and
    anything else is `unknown`, which refuses.
    """
    goal = "資料を作成して同じフォルダに出力してください。"
    assert transport_policy.resend_decision_for_landed_act(
        goal, checker=lambda _g: verdict) == expected


def test_the_file_checker_never_claims_the_effect_is_present(tmp_path):
    """Because `present` would license the re-send this whole path exists to prevent."""
    folder = tmp_path / "ogf"
    folder.mkdir()
    (folder / "written_after_the_send.pptx").write_bytes(b"x")
    w = _worker(GOAL % str(folder), time.time() - 60.0)
    assert w._file_effect_checker()(w.goal) != PRESENT


def test_folders_named_in_reads_paths_it_can_see_and_invents_none(tmp_path):
    real = tmp_path / "ogf"
    real.mkdir()
    goal = "デスクトップの ogf フォルダと %s を見てください。" % str(real)
    got = relay_fleet._folders_named_in(goal)
    assert [os.path.normcase(os.path.normpath(p)) for p in got] \
        == [os.path.normcase(os.path.normpath(str(real)))], (
        "prose locations must not be guessed into paths: %r" % (got,))

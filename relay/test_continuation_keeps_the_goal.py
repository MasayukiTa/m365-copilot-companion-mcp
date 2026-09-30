"""Continuation, recovery and fresh-conversation prompts must carry the WHOLE goal.

Incident (three trip-planning runs, 2026-09-28..30): `_task_anchor` restated only the first
~160 characters of the goal on every continuation turn. The goal's one hard constraint sat
after character 200, so from turn 3 on the worker no longer saw it, answered a different
question, and finally told the reviewer the constraint "does not exist in the user's text".
A recovery message with an EMPTY "--- 元のゴール ---" section also replaced the whole task
for several turns and could seed a recycle prompt's goal.

Hermetic: no browser, no network, no filesystem outside pytest tmp.
"""
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import relay_fleet as F  # noqa: E402
from relay import fanout  # noqa: E402
from relay import fleet_runner  # noqa: E402
from relay.copilot_autopilot_relay import (  # noqa: E402
    CONTINUE_JOB, FIX_JOB, REFUTE_FIX_JOB, RETRY_JOB, VERIFY_FIX_JOB,
)

PW = "test-placeholder-not-a-real-password"

# A vague, roughly 550 character goal whose HARD constraint comes after character 200.
HARD = "10/3 14:00ごろ馬場島から入山、10/4 4:00上市駅"
GOAL = (
    "北陸方面で一泊二日の山行を計画したいです。同行者は初心者気味なので、無理のない範囲で"
    "景色のよいところを選んでください。天気が悪ければ順延でも構いませんし、コースの選び方は"
    "そちらにお任せします。宿や食事についても、おすすめがあれば添えてください。"
    "季節の見どころや、混雑しやすい時間帯、駐車場や更衣の段取りについても分かる範囲で触れて"
    "もらえると助かります。文章は堅くなりすぎないようにお願いします。"
    "ただし絶対に守ってほしい条件があります。" + HARD + "に着く行程にすること。"
    "この時刻は変えられません。行きの交通手段は問いませんが、帰りは公共交通機関で戻れるように"
    "してください。予算は一人あたり三万円程度、装備は貸し出しを前提にせず自前で揃えられる"
    "ものだけで考えます。最後に、全体の行程表を時刻つきで一覧にして、休憩や補給の地点、"
    "エスケープルート、緊急連絡先の集め方も簡単に書いてください。"
)


def test_the_fixture_matches_the_incident_shape():
    assert 400 < len(GOAL) < 700
    assert GOAL.index(HARD) > 200


def _worker(goal=GOAL, **kw):
    return F.RelayWorker(goal, "w0", max_continue=8, max_no_progress=100, **kw)


NUDGES = {
    "RETRY_JOB": RETRY_JOB,
    "CONTINUE_JOB": CONTINUE_JOB,
    "FIX_JOB": FIX_JOB,
    "VERIFY_FIX_JOB": VERIFY_FIX_JOB % "check failed",
    "REFUTE_FIX_JOB": REFUTE_FIX_JOB % "boundary missed",
    "escalating continue": F._continue_nudge(3),
    "SPLIT_JOB": fanout.SPLIT_JOB,
}


@pytest.mark.parametrize("name", sorted(NUDGES))
def test_every_anchored_nudge_carries_the_complete_goal(name):
    job = _worker()._task_anchor(NUDGES[name])
    assert GOAL in job, "the goal was shortened in a %s prompt" % name
    assert HARD in job
    assert NUDGES[name] in job


def test_a_very_long_goal_is_bounded_and_says_so_and_keeps_head_and_tail():
    head = "HEAD-SENTENCE。"
    tail = "TAIL-SENTENCE-WITH-CONSTRAINT。"
    goal = head + ("あ" * 30000) + tail
    job = _worker(goal)._task_anchor("next")
    assert len(job) < F.ANCHOR_GOAL_CAP + 1500
    assert head in job and tail in job
    m = re.search(r"\(truncated (\d+) chars\)", job)
    assert m, "a truncated goal must be marked"
    assert int(m.group(1)) == len(goal) - F.ANCHOR_GOAL_CAP


def test_a_goal_at_the_cap_is_not_marked_truncated():
    goal = "x" * F.ANCHOR_GOAL_CAP
    assert "truncated" not in _worker(goal)._task_anchor("next")


def test_a_fanout_child_keeps_its_scope_block():
    kids = fanout.child_goals(GOAL, ["1月分", "2月分", "3月分"])
    child = kids[1]
    w = _worker(child)
    job = w._task_anchor("next")
    assert "担当する範囲 — 全体の 2/3" in job
    assert "2月分" in job
    assert HARD in job


def test_a_non_coding_goal_gets_neutral_wording():
    job = _worker()._task_anchor("next")
    assert "修正中" not in job
    assert "修正" not in job.replace("next", "")


def test_a_goal_with_checks_keeps_the_working_tree_wording(tmp_path):
    goal = {"text": GOAL, "cwd": str(tmp_path),
            "checks": [{"type": "file_exists", "path": str(tmp_path / "out.txt")}]}
    w = _worker(goal)
    assert w.checks, "fixture must carry a verification card"
    job = w._task_anchor("next")
    assert GOAL in job and str(tmp_path) in job
    assert "修正中" in job


def test_no_continuation_job_is_assigned_without_the_anchor():
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    bad = re.findall(r"self\.job = (?:VERIFY_FIX_JOB|_refute_fix_job\()", src)
    assert not bad, "a continuation prompt bypasses _task_anchor: %r" % bad


# ---- recovery / recycle / replay ------------------------------------------------------

def _unlock_text():
    return F.UNLOCK_PREFIX % PW


def test_a_recovery_steer_with_an_empty_goal_section_gets_the_goal_back():
    w = _worker()
    job = w._steer_job(_unlock_text())
    assert F.SYSTEM_RECOVERY_PREFIX in job
    assert GOAL in job
    tail = job.split("--- 元のゴール ---", 1)[1]
    assert GOAL in tail


def test_a_recovery_steer_that_already_has_the_goal_is_not_duplicated():
    w = _worker()
    job = w._steer_job(_unlock_text() + GOAL)
    assert job.count(GOAL) == 1


def test_a_human_steer_is_unchanged():
    w = _worker()
    job = w._steer_job("追加指示: ログも出力して")
    assert job.startswith("【ユーザーからの追加指示】追加指示: ログも出力して")
    assert F.SYSTEM_RECOVERY_PREFIX not in job


def test_begin_send_uses_the_filled_recovery_job():
    w = _worker()
    w.steer(_unlock_text())
    w._begin_send()
    assert GOAL in w.job


@pytest.fixture
def pw(monkeypatch):
    monkeypatch.setattr(F, "_unlock_password", lambda: PW)


def test_a_recycle_never_carries_an_empty_goal(pw):
    w = _worker("")
    w._recycles = 1
    with pytest.raises(F.EmptyGoalError):
        w._recycle_job()


def test_a_replay_never_carries_an_empty_goal(pw):
    w = _worker("")
    with pytest.raises(F.EmptyGoalError):
        w._replay_job()


def test_unlock_text_never_becomes_the_goal_of_a_recycle(pw):
    w = _worker(_unlock_text() + GOAL)
    w._recycles = 1
    job = w._recycle_job()
    after = job.split("--- 元のゴール ---", 1)[1]
    assert after.lstrip().startswith(GOAL)
    assert "【要解錠】" not in job and PW not in job


def test_unlock_text_with_no_goal_refuses_to_build_a_recycle(pw):
    w = _worker(_unlock_text())
    w._recycles = 1
    with pytest.raises(F.EmptyGoalError):
        w._recycle_job()


def test_unlock_text_never_becomes_the_goal_of_a_replay(pw):
    w = _worker(_unlock_text() + GOAL)
    job = w._replay_job()
    assert GOAL in job
    assert "【要解錠】" not in job and PW not in job


def test_the_follow_up_channel_puts_the_goal_into_a_recovery_payload():
    class _W:
        name = "w0"
        goal = GOAL
        cwd = ""
    sent = []
    ok = fleet_runner._follow_up(_W(), _unlock_text(), sent.append, lambda m: None)
    assert ok
    text = sent[0]["text"]
    assert GOAL in text.split("--- 元のゴール ---", 1)[1]
    assert sent[0]["follow_up_to"] == GOAL

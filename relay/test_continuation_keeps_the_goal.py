"""Continuation and recovery prompts carry a COMPACT LEDGER of the goal, never the whole goal.

Incident (three trip-planning runs, 2026-09-28..30): `_task_anchor` restated only the first
~160 characters of the goal on every continuation turn. The goal's one hard constraint sat
after character 200, so from turn 3 on the worker no longer saw it. PR #80 fixed that by
restating the WHOLE goal every turn; the owner rejected that ("the context does not hold
much; handing long text every turn is a bad move"). The first message of a conversation keeps
the full goal; every later prompt carries a deterministic ledger of at most
F.LEDGER_MAX_CHARS characters: one task line, the goal's fixed-constraint sentences, the
fan-out child's scope block, and a pointer to the first message.

A recovery message with an EMPTY "--- 元のゴール ---" section must still never replace the task.
A brand-new conversation (recycle / replay) has no earlier message, so its first message
still carries the full goal (and the empty-goal guards stay).

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

# A vague, roughly 600 character goal whose HARD constraint comes after character 200.
S_TIME = "10/3の14:00ごろ馬場島から入山し、10/4の4:00には上市駅に着く行程にすること。"
S_FIXED = "この時刻は絶対に動かせません。"
S_LEAD = "ただし絶対に守ってほしい条件があります。"
S_BUDGET = ("予算は一人あたり三万円程度、装備は貸し出しを前提にせず自前で揃えられる"
            "ものだけで考えます。")
HARD = S_TIME
CONSTRAINTS = [S_LEAD, S_TIME, S_FIXED, S_BUDGET]
FIRST = "北陸方面で一泊二日の山行を計画したいです。"
GOAL = (
    FIRST
    + "同行者は初心者気味なので、無理のない範囲で"
    "景色のよいところを選んでください。天気が悪ければ順延でも構いませんし、コースの選び方は"
    "そちらにお任せします。宿や食事についても、おすすめがあれば添えてください。"
    "季節の見どころや、混雑しやすい時間帯、駐車場や更衣の段取りについても分かる範囲で触れて"
    "もらえると助かります。文章は堅くなりすぎないようにお願いします。"
    + S_LEAD + S_TIME + S_FIXED
    + "行きの交通手段は問いませんが、帰りは公共交通機関で戻れるようにしてください。"
    + S_BUDGET
    + "最後に、全体の行程表を時刻つきで一覧にして、休憩や補給の地点、"
    "エスケープルート、緊急連絡先の集め方も簡単に書いてください。"
)


def test_the_fixture_matches_the_incident_shape():
    assert 400 < len(GOAL) < 800
    assert GOAL.index(S_TIME) > 200


def _worker(goal=GOAL, **kw):
    return F.RelayWorker(goal, "w0", max_continue=8, max_no_progress=100, **kw)


def _anchor_of(w, nudge):
    """The anchor text alone (what _task_anchor put in front of the nudge)."""
    job = w._task_anchor(nudge)
    assert job.endswith(nudge)
    return job[:len(job) - len(nudge)]


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
def test_every_anchored_nudge_carries_a_ledger_not_the_goal(name):
    w = _worker()
    job = w._task_anchor(NUDGES[name])
    anchor = _anchor_of(w, NUDGES[name])
    assert len(anchor) <= F.LEDGER_MAX_CHARS, len(anchor)
    for sentence in CONSTRAINTS:
        assert sentence in anchor, "constraint lost in a %s prompt: %s" % (name, sentence)
    assert HARD in anchor
    assert GOAL not in job, "the whole goal was restated in a %s prompt" % name
    assert FIRST in anchor                       # the one-line task
    assert "最初のメッセージ" in anchor           # the pointer to the full goal
    assert job.count(S_TIME) == 1


def test_the_ledger_is_deterministic():
    assert _worker()._task_anchor("n") == _worker()._task_anchor("n")


def test_the_incident_ledger_is_below_the_cap_and_the_cap_is_about_1000():
    anchor = _anchor_of(_worker(), "n")
    assert len(anchor) <= F.LEDGER_MAX_CHARS
    assert 900 <= F.LEDGER_MAX_CHARS <= 1100     # "about 1,000 characters"; not inflated
    assert len(anchor) < len(GOAL)


def test_a_goal_without_marker_sentences_falls_back_to_its_tail():
    goal = ("部屋の写真から家具の一覧を作ってください。名前は分かりやすい言葉にしてください。"
            "色も書いてください。読みやすい表にまとめてください。"
            "最後に、全体の感想を一言添えてください。")
    anchor = _anchor_of(_worker(goal), "n")
    assert "部屋の写真から家具の一覧を作ってください" in anchor     # task line
    assert "読みやすい表にまとめてください。" in anchor               # tail sentence 1
    assert "最後に、全体の感想を一言添えてください。" in anchor        # tail sentence 2
    assert "名前は分かりやすい言葉にしてください" not in anchor        # middle not restated
    assert len(anchor) <= F.LEDGER_MAX_CHARS


def test_a_very_long_goal_keeps_the_ledger_under_the_cap_and_the_constraint():
    filler = "これは補足の説明です。" * 3000            # ~33,000 characters, no marker
    goal = FIRST + filler + S_TIME + filler + "締めの一文です。"
    assert len(goal) > 30000
    anchor = _anchor_of(_worker(goal), "next")
    assert len(anchor) <= F.LEDGER_MAX_CHARS
    assert S_TIME in anchor
    assert FIRST in anchor
    # the size does not grow with the goal
    longer = _anchor_of(_worker(goal + filler * 3), "next")
    assert abs(len(longer) - len(anchor)) < 300


def test_many_constraints_never_push_the_ledger_over_the_cap():
    goal = FIRST + "".join("項目%d は必ず守ること。" % i for i in range(300))
    anchor = _anchor_of(_worker(goal), "next")
    assert len(anchor) <= F.LEDGER_MAX_CHARS
    assert "必ず守ること" in anchor


def test_an_english_goal_keeps_its_constraints():
    goal = ("Plan a two-day hike for beginners. Pick scenic routes and keep the text friendly. "
            "The budget must stay under $300 per person. Never book anything that needs a car. "
            "Return by 4:00 pm on Oct 4. Add a short packing list at the end.")
    anchor = _anchor_of(_worker(goal), "next")
    assert "Plan a two-day hike for beginners." in anchor
    assert "The budget must stay under $300 per person." in anchor
    assert "Never book anything that needs a car." in anchor
    assert "Return by 4:00 pm on Oct 4." in anchor
    assert "Pick scenic routes" not in anchor
    assert len(anchor) <= F.LEDGER_MAX_CHARS


def test_a_fanout_child_keeps_its_scope_block():
    kids = fanout.child_goals(GOAL, ["1月分", "2月分", "3月分"])
    anchor = _anchor_of(_worker(kids[1]), "next")
    assert "全体の 2/3" in anchor
    assert "2月分" in anchor
    assert "手を出さないこと" in anchor
    assert S_TIME in anchor
    assert len(anchor) <= F.LEDGER_MAX_CHARS
    # the fan-out closing instruction is protocol, not a constraint of the task
    assert "DONE" not in anchor


def test_a_fanout_child_with_a_long_scope_still_fits_the_cap():
    kids = fanout.child_goals(GOAL, ["範囲" * 400, "b"])
    anchor = _anchor_of(_worker(kids[0]), "next")
    assert len(anchor) <= F.LEDGER_MAX_CHARS
    assert "全体の 1/2" in anchor and "手を出さないこと" in anchor


def test_the_pointer_names_the_job_when_the_record_knows_it():
    anchor = _anchor_of(_worker({"text": GOAL, "task_id": "camp-7-2"}), "n")
    assert "camp-7-2" in anchor
    assert "最初のメッセージ" in anchor


def test_a_non_coding_goal_gets_neutral_wording():
    job = _worker()._task_anchor("next")
    assert "修正中" not in job
    assert "修正" not in job.replace("next", "")


def test_a_goal_with_checks_keeps_the_working_tree_wording(tmp_path):
    goal = {"text": GOAL, "cwd": str(tmp_path),
            "checks": [{"type": "file_exists", "path": str(tmp_path / "out.txt")}]}
    w = _worker(goal)
    assert w.checks, "fixture must carry a verification card"
    anchor = _anchor_of(w, "next")
    assert S_TIME in anchor and str(tmp_path) in anchor
    assert "修正中" in anchor
    assert len(anchor) <= F.LEDGER_MAX_CHARS
    assert GOAL not in anchor


def test_an_empty_goal_behaves_as_before_and_never_raises():
    assert _worker("")._task_anchor("next") == "next"
    assert F.goal_ledger("") == ""


def test_the_ledger_of_a_recovery_wrapped_goal_uses_only_the_real_goal():
    anchor = _anchor_of(_worker(F.UNLOCK_PREFIX % PW + GOAL), "n")
    assert S_TIME in anchor
    assert PW not in anchor and "【要解錠】" not in anchor


def test_no_continuation_job_is_assigned_without_the_anchor():
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    bad = re.findall(r"self\.job = (?:VERIFY_FIX_JOB|_refute_fix_job\()", src)
    assert not bad, "a continuation prompt bypasses _task_anchor: %r" % bad


# ---- recovery / recycle / replay ------------------------------------------------------

def _unlock_text():
    return F.UNLOCK_PREFIX % PW


def test_a_recovery_steer_with_an_empty_goal_section_gets_the_ledger_back():
    w = _worker()
    job = w._steer_job(_unlock_text())
    assert F.SYSTEM_RECOVERY_PREFIX in job
    tail = job.split("--- 元のゴール ---", 1)[1]
    assert tail.strip()
    for sentence in CONSTRAINTS:
        assert sentence in tail
    assert GOAL not in job                      # a ledger, not the whole goal
    assert len(tail) <= F.LEDGER_MAX_CHARS + len(F.CLOSING_INSTRUCTION) + 200


def test_a_recovery_steer_that_already_has_the_goal_is_not_duplicated():
    w = _worker()
    job = w._steer_job(_unlock_text() + GOAL)
    assert job.count(GOAL) == 1
    assert job.count(S_TIME) == 1


def test_a_human_steer_is_unchanged():
    w = _worker()
    job = w._steer_job("追加指示: ログも出力して")
    assert job.startswith("【ユーザーからの追加指示】追加指示: ログも出力して")
    assert F.SYSTEM_RECOVERY_PREFIX not in job


def test_begin_send_uses_the_filled_recovery_job():
    w = _worker()
    w.steer(_unlock_text())
    w._begin_send()
    assert S_TIME in w.job and GOAL not in w.job


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


def test_a_fresh_conversation_still_opens_with_the_full_goal(pw):
    w = _worker()
    w._recycles = 1
    assert GOAL in w._recycle_job()
    assert GOAL in w._replay_job()


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


def test_the_follow_up_channel_puts_the_ledger_into_a_recovery_payload():
    class _W:
        name = "w0"
        goal = GOAL
        cwd = ""
    sent = []
    ok = fleet_runner._follow_up(_W(), _unlock_text(), sent.append, lambda m: None)
    assert ok
    text = sent[0]["text"]
    tail = text.split("--- 元のゴール ---", 1)[1]
    for sentence in CONSTRAINTS:
        assert sentence in tail
    assert GOAL not in text
    assert sent[0]["follow_up_to"] == GOAL

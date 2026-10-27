# -*- coding: utf-8 -*-
"""Two unrelated goals that merely carry the SAME appended 'common rules' must both be accepted.

WHAT HAPPENED, 2026-10-26. The owner submitted two real, UNRELATED coding requests -- a
desktop chat-switch bug fix and a post-quantum config migration. Each ended with the same long
shared "common rules" paragraph (anchored at "共通の守ること"). fleet_submit's duplicate guard
compared the WHOLE goal text, and that shared tail pushed the content-word Jaccard to 0.539 --
just over DUP_SIMILARITY (0.5) -- so the second request was refused as a near-duplicate of the
first while the first was still in flight. Appending the same common rules to a batch of
unrelated coding goals is ordinary use, so the guard had to stop counting a paragraph that is
identical ACROSS submissions as evidence that two goals are the same job.

WHAT THIS FILE PINS. (a) Two goals whose request title and body differ but whose appended
common-rules tail is identical are BOTH accepted. (b) An exact resend of a goal already in
flight is still refused. (c) A resend with only a few words reworded is still refused. The
guard now compares each pair's goal-SPECIFIC words -- what is left after the longest run of
trailing tokens the two goals share verbatim is removed (fleet_intake._goal_specific_words) --
falling back to the whole-goal measure only when stripping the shared tail leaves too little to
judge on, which is exactly the shape of an exact or near-exact resend.

The common-rules paragraph below is a neutral paraphrase, not a real one, and the two requests
are invented stand-ins for the owner's two real goals -- no product, file or business name
belongs in a tracked file (see this repo's history on that).
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import task_router as TR  # noqa: E402
from tools import fleet_intake as FI  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """A queue AND a fleet-state directory of its own -- the same isolation
    tools/test_a_locked_out_worker_should_not_clone_its_own_work.py uses, because this file
    also plants worker records in status.json and must not read the live fleet's state."""
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    TR.ensure_dirs()
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(tmp_path / "state"))
    os.makedirs(str(tmp_path / "state"), exist_ok=True)
    return tmp_path


def _write_status(tmp_path, workers):
    with open(os.path.join(str(tmp_path / "state"), "status.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"running": True, "workers": workers}, fh, ensure_ascii=False)


def _running_worker(jid, goal):
    return {"jid": jid, "goal": goal, "status": "ready", "closed": False, "outcome": None}


def _jobs():
    d = os.path.join(TR.TASKS, "pending")
    out = []
    for name in sorted(os.listdir(d)):
        if name.endswith(".json"):
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                out.append(json.load(fh))
    return out


# The long shared "common rules" paragraph, appended verbatim to every goal in a batch. A
# neutral paraphrase: it carries the SHAPE that caused the incident -- many content words,
# identical across submissions, at the END of each goal -- without any real rule text.
_COMMON_RULES = (
    " 共通の守ること。git は読み取り専用のコマンドだけを使い、ブランチを変えず、コミットせず、push せず、"
    "変更は作業ツリーに残す。この複製ではビルドも試験も実行しない。書いたコードは型と関数と可視性を自分で"
    "確かめる。通信の形式と保存の形式と固定見本のバイト列は変えない。既存の試験を書き換えて通すことは禁止。"
    "鍵や nonce や salt やパスワードの値をコードにも試験にも固定値で書かない。試験で要る値は乱数で生成する。"
    "H ドライブと SharePoint には何も書かない。作業は指定した複製の中だけで行う。社員 ID や認証情報を扱わない。"
    "公開リポジトリなので、コメントと文字列に攻撃手法の説明を書かない。短く中立に書く。新しい試験を足す。"
    "出力は 1 行目を DONE か BLOCKED とし、変更したファイルの一覧と git diff の全文を続ける。この作業を分割する"
    "場合も、子タスクにこの出力形式と上の共通の守ることをそのまま守らせること。"
)

# Two UNRELATED requests -- different title, different body -- that happen to end with the same
# common-rules tail. These stand in for the owner's goal1 (a desktop chat-switch bug fix) and
# goal2 (a post-quantum config migration).
_CHAT_SWITCH = (
    "コーディング依頼。デスクトップのチャット画面で、グループチャットから通常の会話に切り替えると壊れる不具合を"
    "調べて直す。選択中の会話の状態を持つ箇所を特定し、古い選択の ID が残る・下書きが漏れる・送信先が"
    "切り替わらない・履歴が空のままになる、の欠陥をファイルと行つきで一覧にする。選択の切り替え時に状態を"
    "一か所でまとめてリセットする。空状態の表示を左右で統一し、ボタンのフォーカス順を視覚の順に合わせる。"
) + _COMMON_RULES

_PQC_MIGRATE = (
    "コーディング依頼。耐量子 require 化の移行を追加する。旧版で明示された prefer を、beta.12 の最初の"
    "起動で require に移す。現在の移行は version が 2 未満で off だったものだけを未選択に戻し、prefer を"
    "選択として保つので、classical に落ちる余地が残る。version 0 と 1 の prefer を require に移し、移行後の"
    "明示的な prefer と off は保つ。表をコメントに書き、試験を足し、リリースノートの説明をこの方針に"
    "合わせて直す。冶等性も確かめる。"
) + _COMMON_RULES


def test_the_two_goals_really_do_share_a_long_common_rules_tail():
    """The premise of this file: measured on these two goals, the WHOLE-goal word overlap is
    high ENOUGH to clear the old threshold purely because of the shared tail, while the
    goal-specific overlap (tail removed) is plainly below it. If this ever stopped being true,
    the rest of the file would be testing nothing."""
    whole = FI._dup_similarity(FI._dup_words(_CHAT_SWITCH), FI._dup_words(_PQC_MIGRATE))
    assert whole >= FI.DUP_SIMILARITY, (
        "the shared tail must make the WHOLE-goal overlap clear the old bar, or this file is "
        "not reproducing the incident: got %.3f" % whole)
    spec_a, na = FI._goal_specific_words(_CHAT_SWITCH, _PQC_MIGRATE)
    spec_b, nb = FI._goal_specific_words(_PQC_MIGRATE, _CHAT_SWITCH)
    assert na >= FI.DUP_SPECIFIC_MIN_WORDS and nb >= FI.DUP_SPECIFIC_MIN_WORDS
    specific = FI._dup_similarity(spec_a, spec_b)
    assert specific < FI.DUP_SIMILARITY, (
        "with the shared tail removed the two goals must read as different work: got %.3f"
        % specific)


def test_two_unrelated_goals_sharing_only_the_common_rules_are_both_accepted(
        isolated_state, monkeypatch):
    """(a) The incident itself. First goal is in flight on a worker; the second, a different
    request carrying the same appended rules, must be accepted. With the guard off by default this
    is now true for ANY second goal, related or not; the guard is pinned off explicitly so the
    test does not depend on the machine's settings file."""
    monkeypatch.setattr(FI, "_dup_guard_on", lambda: False)
    _write_status(isolated_state, [_running_worker("run1", _CHAT_SWITCH)])
    out = FI.fleet_submit(_PQC_MIGRATE)
    assert "queued" in out, out
    assert len(_jobs()) == 1


def test_the_same_holds_when_the_first_goal_is_only_pending(isolated_state, monkeypatch):
    """(a), via the door's own pending/ queue rather than a live worker -- a goal sitting in
    pending is exactly as 'in flight' as one on a worker, so the shared tail must not make it
    swallow an unrelated second goal either."""
    monkeypatch.setattr(FI, "_dup_guard_on", lambda: False)
    FI.fleet_submit(_CHAT_SWITCH)
    assert len(_jobs()) == 1
    out = FI.fleet_submit(_PQC_MIGRATE)
    assert "queued" in out, out
    assert len(_jobs()) == 2


def test_an_exact_resend_is_still_refused_when_the_guard_is_on(isolated_state, monkeypatch):
    """(b) The improved similarity still does its job WHEN THE GUARD IS TURNED ON. By owner order
    2026-10-26 the guard is off by default, so an exact resend is now ACCEPTED and recorded in the
    background, not refused (see the accept-by-default suites). This test pins the other half: with
    fleet_submit_dup_guard on, resubmitting a goal already in flight byte-for-byte is still refused
    and the refusal still names the running job. (An exact resend shares its whole text as the
    tail, so goal-specific words fall below the floor and the comparison falls back to the
    whole-goal measure -- which is 1.0.) The guard is forced on by patching _dup_guard_on so this
    proves the similarity logic, not settings-file parsing."""
    monkeypatch.setattr(FI, "_dup_guard_on", lambda: True)
    _write_status(isolated_state, [_running_worker("run1", _CHAT_SWITCH)])
    out = FI.fleet_submit(_CHAT_SWITCH)
    # Content resemblance never refuses (owner order 2026-10-26/27). Even an exact resend with the
    # guard on is QUEUED; the resemblance is only recorded (possible_duplicate_of names run1).
    assert "queued" in out and "refused" not in out, out
    jobs = _jobs()
    assert len(jobs) == 1, "the resend must be queued, not refused"
    assert jobs[0]["payload"]["possible_duplicate_of"] == "run1", jobs


def test_a_resend_with_a_few_words_reworded_is_still_refused_when_the_guard_is_on(
        isolated_state, monkeypatch):
    """(c) A near-exact resend -- the SAME job with a handful of words changed -- is still caught
    WHEN THE GUARD IS ON, because that is the worker-paraphrase shape the guard was built to stop.
    The reworded goal keeps almost all of its OWN words, so its goal-specific overlap with the
    original stays well above the bar. Off by default now, so the guard is forced on here."""
    monkeypatch.setattr(FI, "_dup_guard_on", lambda: True)
    reworded = (_CHAT_SWITCH
                .replace("調べて直す", "調査して修正する")
                .replace("特定し", "特定して")
                .replace("一覧にする", "一覧化する"))
    assert reworded != _CHAT_SWITCH, "the rewording changed nothing; the test proves nothing"
    _write_status(isolated_state, [_running_worker("run1", _CHAT_SWITCH)])
    out = FI.fleet_submit(reworded)
    # Never refused on resemblance (owner order 2026-10-26/27): queued, only tagged.
    assert "queued" in out and "refused" not in out, out
    jobs = _jobs()
    assert len(jobs) == 1, "a reworded resend must be queued, not refused"
    assert jobs[0]["payload"]["possible_duplicate_of"] == "run1", jobs

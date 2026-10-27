# -*- coding: utf-8 -*-
"""`resume_conv` が、**3本ある配送経路のどれを通っても**ゴール項目まで届く。

チャット窓は会話の持続IDをトランスクリプトから読み、`fleet_submit` → `fleet_handoff` に
渡す。フリート側 (`relay/fleet_runner.goals_from_command` と goals-file 経路) は昔から
`resume_conv` を尊重していた -- だが `fleet_handoff` が3本の経路のうち一部でしか欄を
通しておらず、**コックピットの経路では効き、チャット窓の経路では当て推量**だった。これは
`priority` が通し忘れられていたのと同じ形で、同じ規律 (3本すべてで運ぶ) で閉じる。

goal がここを出る経路は3本:

  1. 走行中のフリートに合流する   -> `add_goal_to_live_fleet` の add_goal 項目
  2. フリートを起こす             -> `autostart_fleet` の goals dict
  3. 走行が無いので待つ           -> `for_fleet/<jid>.txt` に JSON で待機

3本目がテキストファイルだったので、欄を足して1と2だけ通せば「走行中だけ効く」機能になる。
だからこのファイルは**経路ごとに1本ずつ**検査し、待機から再配送までの往復も押さえる。
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


@pytest.fixture()
def router(tmp_path, monkeypatch):
    import relay.task_router as T

    monkeypatch.setattr(T, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(T, "FLEET_STATE_DIR", str(tmp_path))
    T.ensure_dirs()
    return T


def _commands(router, state_dir):
    """このパスで書かれたコマンドを、フリートの読み手そのもので読む。"""
    from relay import fleet_runner as FR

    return FR.read_commands(str(state_dir))


def _goal_items(router, state_dir):
    """add_goal 項目を、フリートが実際に組み立てるゴール dict にしてから返す -- 欄が
    『コマンドには載ったがゴールには届かない』隙間まで含めて検査するため。"""
    from relay import fleet_runner as FR

    out = []
    for cmd in _commands(router, state_dir):
        out.extend(FR.goals_from_command(cmd))
    return out


GOAL = "\u7d9a\u304d\u3092\u66f8\u304f"
CONV = "sess:conv-42"


# -- route 1: joining a live run --------------------------------------------------------------

def test_a_live_fleet_receives_the_resume_conv(router, tmp_path, monkeypatch):
    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: True)
    status, _ = router.fleet_handoff(GOAL, "j1", str(tmp_path), resume_conv=CONV)
    assert status == "dispatched"
    items = _goal_items(router, tmp_path)
    assert items and items[0]["resume_conv"] == CONV


def test_a_live_goal_without_a_conversation_carries_no_field(router, tmp_path, monkeypatch):
    """既定は『新規に始める』。空の id を項目に載せてはいけない -- 読み手が偽の継続と
    見なさないように。"""
    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: True)
    router.fleet_handoff(GOAL, "j2", str(tmp_path))
    items = _goal_items(router, tmp_path)
    assert items and "resume_conv" not in items[0]


# -- route 2: starting a fleet ----------------------------------------------------------------

def test_an_autostarted_fleet_is_handed_the_resume_conv(router, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: False)
    monkeypatch.setattr(router, "AUTOSTART", True)
    monkeypatch.setattr(router, "autostart_status", lambda *a, **k: (True, ""))
    monkeypatch.setattr(router, "autostart_fleet",
                        lambda goals, sd=None: seen.update(goals=goals) or {"ok": True, "pid": 1})
    status, _ = router.fleet_handoff(GOAL, "j3", str(tmp_path), resume_conv=CONV)
    assert status == "dispatched"
    assert seen["goals"][0]["resume_conv"] == CONV


def test_an_autostarted_goal_without_a_conversation_carries_no_field(router, tmp_path,
                                                                     monkeypatch):
    seen = {}
    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: False)
    monkeypatch.setattr(router, "AUTOSTART", True)
    monkeypatch.setattr(router, "autostart_status", lambda *a, **k: (True, ""))
    monkeypatch.setattr(router, "autostart_fleet",
                        lambda goals, sd=None: seen.update(goals=goals) or {"ok": True, "pid": 1})
    router.fleet_handoff(GOAL, "j3b", str(tmp_path))
    assert "resume_conv" not in seen["goals"][0]


# -- route 3: waiting on disk, then redelivered -----------------------------------------------

def test_a_waiting_goal_keeps_its_resume_conv(router, tmp_path, monkeypatch):
    """**この経路が落ちていたら、機能は『走行中だけ効く』ことになる。** 待機ファイルに
    会話idが残り、フリートが現れたときにゴール項目まで届く。"""
    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: False)
    monkeypatch.setattr(router, "AUTOSTART", False)
    status, _ = router.fleet_handoff(GOAL, "j4", str(tmp_path), resume_conv=CONV)
    assert status == "awaiting_fleet"

    parked = open(os.path.join(router.TASKS, "for_fleet", "j4.txt"), encoding="utf-8").read()
    assert router.read_for_fleet(parked) == (GOAL, False, CONV)

    # ...and the conversation id is still there when a fleet finally appears.
    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: True)
    router._deliver_waiting_goals(state_dir=str(tmp_path))
    items = _goal_items(router, tmp_path)
    assert items and items[0]["resume_conv"] == CONV


def test_a_waiting_priority_continuation_keeps_both_fields(router, tmp_path, monkeypatch):
    """priority と resume_conv は同じ JSON 形で同居する -- 片方が他方を消してはいけない。"""
    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: False)
    monkeypatch.setattr(router, "AUTOSTART", False)
    router.fleet_handoff(GOAL, "j5", str(tmp_path), priority=True, resume_conv=CONV)
    parked = open(os.path.join(router.TASKS, "for_fleet", "j5.txt"), encoding="utf-8").read()
    assert router.read_for_fleet(parked) == (GOAL, True, CONV)


def test_a_plain_parked_file_still_reads_as_no_conversation(router, tmp_path, monkeypatch):
    """移行前に置かれた素のテキストは、会話idなしとして読めなければならない。"""
    p = os.path.join(router.TASKS, "for_fleet", "old.txt")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("a goal parked before this existed")
    assert router.read_for_fleet(open(p, encoding="utf-8").read()) == (
        "a goal parked before this existed", False, "")


# -- the door writes it onto the payload the router reads --------------------------------------

def test_the_payload_resume_conv_reaches_a_live_fleet_end_to_end(router, tmp_path, monkeypatch):
    """扉 (fleet_submit) が payload に書いた会話idが、ドレインパスを通って走行中フリートの
    ゴール項目まで届く -- 書き手と読み手の継ぎ目そのものを検査する。

    fleet_intake はこのワークツリーのものを使う。同名の別クローンが先に import 済みだと
    旧版 (resume_conv 引数なし) を掴むので、このファイルのディレクトリから明示的に読み込む。
    """
    import importlib.util
    fi_path = os.path.join(REPO, "tools", "fleet_intake.py")
    spec = importlib.util.spec_from_file_location("fleet_intake_worktree", fi_path)
    FI = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(FI)

    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: True)
    FI.fleet_submit("\u3053\u306e\u4f1a\u8a71\u306e\u7d9a\u304d\u3092\u5b9f\u884c\u3059\u308b", resume_conv=CONV)
    router.dispatch_once()
    items = _goal_items(router, tmp_path)
    assert items and items[0]["resume_conv"] == CONV

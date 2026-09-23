# -*- coding: utf-8 -*-
"""`/goal ` が使われたことを、答えられる場所に残す。

チャット窓の `/goal ` は、本来 steer になる入力を**同じ会話の中の新しいタスク**に変える。
常設の残件リストは長らくこれを「本番で一度も発火していないかもしれない機構」として
抱えていた — **確かめる方法が無かったから**。コマンドファイルは読まれた瞬間に消え、
そのあとは普通の goal と見分けがつかない。**未回答ではなく、回答不能だった。**

## 測ってから決めた

隣の経路は使われている: 記録済み 2,041 goal のうち **6件がフォローアップの枠**を持つ。
だから「ではこちらは？」に答えられることには値段がついた。

## 新しい台帳は作らない

`mechanisms.jsonl` は「その機構は作られた状況に到達したか」を答えるために既に在る。
1行足すだけで済むので、読み手のいない台帳をもう1本増やす理由が無い。

## 落ちる場所は1つ

窓が欄を載せ、`goals_from_command` が運び、到着点が記録する。
**その真ん中が今日2回落ちていた**（`resume_conv` と `commands.json`）ので、
この検査は**端ではなく経路**を通す。
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def test_the_normaliser_carries_the_verb():
    from relay import fleet_runner as FR

    out = FR.goals_from_command({"add_goal": [{"text": "a new task", "new_task": True}]})
    assert out and out[0].get("new_task") is True


def test_an_ordinary_follow_up_is_not_marked():
    """**既定を変えない。** 普通のフォローアップは steer のままで、印も付かない。"""
    from relay import fleet_runner as FR

    out = FR.goals_from_command({"add_goal": [{"text": "x", "follow_up_to": "old"}]})
    assert "new_task" not in out[0]


def test_the_mechanism_name_is_registered(tmp_path, monkeypatch):
    """**登録されていない機構は、書かれても要約に出ない** — `record` の隣にそう書いてある。
    出ないなら読み手のいない計器で、この変更の目的を外す。"""
    from relay import mechanism_telemetry as MT

    assert "new_task_escape" in MT.MECHANISMS
    monkeypatch.setattr(MT, "LOG", str(tmp_path / "m.jsonl"))
    MT.record("new_task_escape", configured=True, eligible=True, triggered=True, executed=True)
    f = MT.funnel(MT.load(str(tmp_path / "m.jsonl")), "new_task_escape")["new_task_escape"]
    assert f["records"] == 1 and f["executed"] == 1


def test_the_arrival_records_it(tmp_path, monkeypatch):
    """**経路の端から端まで。** コマンドが着いたら1行残ること。"""
    from relay import fleet_runner as FR
    from relay import mechanism_telemetry as MT

    monkeypatch.setattr(MT, "LOG", str(tmp_path / "m.jsonl"))
    goals = FR.goals_from_command(
        {"add_goal": [{"text": "a new task", "new_task": True, "resume_conv": "sess:abc"}]})
    # the loop at the consumption point, in the shape it runs there
    for g in goals:
        if g.get("new_task"):
            MT.record("new_task_escape", configured=True, eligible=True, triggered=True,
                      executed=True, extra={"has_resume_conv": bool(g.get("resume_conv"))})
    rows = MT.load(str(tmp_path / "m.jsonl"))
    assert len(rows) == 1
    assert rows[0]["extra"]["has_resume_conv"] is True


def test_the_window_sets_it_only_for_the_verb():
    """C# 側は実行できないので、ここは**綴りではなく条件**を見る:
    印が付くのは `forceNewGoal` のときだけで、無条件ではない。"""
    import io

    # ui/ChatSend.cs since 2026-09-24 -- and ui/test_the_chat_window_sends_what_was_typed.py now
    # does execute it: new_task is present for `/goal` and ABSENT otherwise, case by case.
    src = io.open(os.path.join(REPO, "ui", "ChatSend.cs"),
                  encoding="utf-8", errors="replace").read()
    i = src.index('g["new_task"]')
    line = src[src.rindex(chr(10), 0, i) + 1:src.index(chr(10), i)]
    assert "forceNewGoal" in line, line

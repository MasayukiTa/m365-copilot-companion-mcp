# -*- coding: utf-8 -*-
"""`fleet_submit(priority=True)` が、**3本ある配送経路のどれを通っても**届く。

フリート側は昔から `priority` を尊重していた (`relay/relay_fleet.py` の保留キューで
前に出る) が、`fleet_submit` には**引数自体が無かった**。だから足すのは「落ちている欄を
拾う」ではなく「新しい欄を通す」作業 — **そして今日ずっと直していたのは、通し忘れた欄**。

goal がここを出る経路は3本ある:

  1. 走行中のフリートに合流する   -> `add_goal_to_live_fleet`
  2. フリートを起こす             -> `autostart_fleet` の goals
  3. 走行が無いので待つ           -> `for_fleet/<jid>.txt`

**3本目がテキストファイルだった。** 欄を足して1と2だけ通せば、「フリートが走っていれば
効き、走っていなければ黙って効かない」機能ができる。`resume_conv` がまさにそれで、
コックピットの経路では動きチャット窓の経路では当て推量だった。

だからこのファイルは**経路ごとに1本ずつ**検査する。
"""
from __future__ import annotations

import json
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


# ── route 1: joining a live run ───────────────────────────────────────────────

def test_a_live_fleet_receives_the_priority(router, tmp_path, monkeypatch):
    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: True)
    status, _ = router.fleet_handoff("tidy the docs", "j1", str(tmp_path), priority=True)
    assert status == "dispatched"
    cmds = _commands(router, tmp_path)
    assert cmds and cmds[0]["add_goal"][0]["priority"] is True


def test_the_default_is_still_false(router, tmp_path, monkeypatch):
    """**扉の全部が緊急なら、優先度は存在しない。** 既定は変えない。"""
    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: True)
    router.fleet_handoff("tidy the docs", "j2", str(tmp_path))
    assert _commands(router, tmp_path)[0]["add_goal"][0]["priority"] is False


# ── route 2: starting a fleet ─────────────────────────────────────────────────

def test_an_autostarted_fleet_is_handed_the_priority(router, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: False)
    monkeypatch.setattr(router, "AUTOSTART", True)
    monkeypatch.setattr(router, "autostart_status", lambda *a, **k: (True, ""))
    monkeypatch.setattr(router, "autostart_fleet",
                        lambda goals, sd=None: seen.update(goals=goals) or {"ok": True, "pid": 1})
    status, _ = router.fleet_handoff("tidy the docs", "j3", str(tmp_path), priority=True)
    assert status == "dispatched"
    assert seen["goals"][0]["priority"] is True


# ── route 3: waiting on disk ──────────────────────────────────────────────────

def test_a_goal_that_has_to_wait_keeps_its_priority(router, tmp_path, monkeypatch):
    """**この経路が落ちていたら、機能は「走行中だけ効く」ことになる。**"""
    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: False)
    monkeypatch.setattr(router, "AUTOSTART", False)
    status, _ = router.fleet_handoff("tidy the docs", "j4", str(tmp_path), priority=True)
    assert status == "awaiting_fleet"

    parked = open(os.path.join(router.TASKS, "for_fleet", "j4.txt"), encoding="utf-8").read()
    assert router.read_for_fleet(parked) == ("tidy the docs", True)

    # ...and it is still priority when a fleet finally appears.
    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: True)
    router._deliver_waiting_goals(state_dir=str(tmp_path))
    items = [g for c in _commands(router, tmp_path) for g in c.get("add_goal", [])]
    assert items and items[0]["priority"] is True


def test_a_plain_parked_file_still_reads(router, tmp_path, monkeypatch):
    """**移行前に置かれたファイルは素のテキスト。** 読めなくなってはいけない。"""
    p = os.path.join(router.TASKS, "for_fleet", "old.txt")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("a goal parked before this existed")
    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: True)
    router._deliver_waiting_goals(state_dir=str(tmp_path))
    items = [g for c in _commands(router, tmp_path) for g in c.get("add_goal", [])]
    assert items and items[0]["text"] == "a goal parked before this existed"
    assert items[0]["priority"] is False


def test_a_goal_whose_text_starts_with_a_brace_is_not_misread(router):
    """JSON かどうかを**形で**判定しているので、境界を押さえる。"""
    assert router.read_for_fleet("{not json at all") == ("{not json at all", False)
    assert router.read_for_fleet('{"priority": true}') == ('{"priority": true}', False)


# ── the door ──────────────────────────────────────────────────────────────────

def test_the_door_records_it(tmp_path, monkeypatch):
    import relay.task_router as T
    from tools import fleet_intake as FI

    monkeypatch.setattr(T, "TASKS", str(tmp_path / "tasks"))
    T.ensure_dirs()
    jid = FI.fleet_submit("tidy the docs", priority=True)
    assert "refused" not in jid, jid
    rows = [json.load(open(os.path.join(T.TASKS, "pending", n), encoding="utf-8"))
            for n in os.listdir(os.path.join(T.TASKS, "pending"))]
    assert rows and rows[0]["payload"]["priority"] is True


def test_the_router_reads_what_the_door_wrote(router, tmp_path, monkeypatch):
    """**書き手と読み手が別モジュール。** 欄名がずれても、片方だけ見る検査は通ってしまう。"""
    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: True)
    job = {"id": "j9", "type": "fleet_goal",
           "payload": {"goal": "tidy the docs", "priority": True}}
    router.run_job(job)
    items = [g for c in _commands(router, tmp_path) for g in c.get("add_goal", [])]
    assert items and items[0]["priority"] is True

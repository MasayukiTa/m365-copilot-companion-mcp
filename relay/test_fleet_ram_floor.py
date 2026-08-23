"""RAM の下限は1つ。auto 系がそれより上の数字を自前で持っていたのが不具合だった。

3つのゲートが 2048 / 1400 / 2000 という別々のリテラルを抱えていて、どれも運用者が
定めた 512 を読んでいなかった。症状は無言で、空き 2454MB の端末で
`auto_concurrency` が 1 を返し、自動設定のフリートは全部を1件ずつ走らせていた。
"""
import ast
import inspect

import pytest

from relay import relay_fleet as RF


def test_the_floor_the_operator_set_is_the_floor_the_fleet_uses():
    """評価器と艦隊で別々の数字を持てば、また黙って離れる。
    ここが落ちたら、離れたほうではなく『どちらが正か』を決め直すこと。"""
    from relay.selfimprove import planner_evaluator as PE
    from relay.selfimprove import route_evaluator as RE
    assert RF.FLEET_RAM_FLOOR_MB == RE.MIN_FREE_MB == PE.MIN_FREE_MB


@pytest.mark.parametrize("floor", [512.0, 2048.0])
def test_every_gate_moves_with_the_one_setting(monkeypatch, floor):
    """3つとも同じ設定を読んでいること。1つでも自前の数字に戻れば、
    下限を上げたつもりの操作が一部にしか効かない。"""
    monkeypatch.setattr(RF, "FLEET_RAM_FLOOR_MB", floor)
    monkeypatch.setattr(RF, "FLEET_PER_TAB_MB", 700.0)
    monkeypatch.setattr(RF, "avail_phys_mb", lambda: floor + 1400.0)

    # 空きは 下限+1400。1タブ700なので、ちょうど2枚ぶん。
    assert RF.auto_concurrency(8) == 2
    assert RF.ram_target_cap(0, 1, 8) == 2        # 現在値1から1枚だけ増える
    # RAM が潤沢でも1回に1枚。下限を読んでいなければこの段差は出ない。
    monkeypatch.setattr(RF, "avail_phys_mb", lambda: floor + 700.0 * 20)
    assert RF.ram_target_cap(0, 1, 8) == 2
    monkeypatch.setattr(RF, "avail_phys_mb", lambda: floor + 1400.0)
    assert RF.ram_room_for_tab() is True

    monkeypatch.setattr(RF, "avail_phys_mb", lambda: floor + 100.0)
    assert RF.auto_concurrency(8) == 1            # 下限は割らない
    assert RF.ram_room_for_tab() is False         # 開けば下限を割る


def test_the_side_page_gate_is_derived_and_not_a_fourth_literal():
    """側ページの閾値は『残すべき下限 + これから開くタブ1枚』。
    そこに置かれていた 2000 は、そのどちらでもなかった。"""
    assert RF.ram_room_for_tab.__defaults__ == (None,)
    src = inspect.getsource(RF.ram_room_for_tab)
    assert "FLEET_RAM_FLOOR_MB + FLEET_PER_TAB_MB" in src


def test_no_gate_carries_its_own_ram_number_as_a_default():
    """既定値に数字が戻っていないこと。戻っていれば、設定を変えてもそこだけ効かない。"""
    tree = ast.parse(inspect.getsource(RF).lstrip())
    checked = 0
    for fn in ("auto_concurrency", "ram_target_cap", "ram_room_for_tab"):
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == fn)
        names = [a.arg for a in node.args.args]
        for arg, default in zip(names[len(names) - len(node.args.defaults):],
                                node.args.defaults):
            if arg in ("per_tab_mb", "headroom_mb", "floor_mb"):
                checked += 1
                assert isinstance(default, ast.Constant) and default.value is None, \
                    "%s の %s に数字が戻っている" % (fn, arg)
    assert checked >= 5, checked

"""伸びしろのあるゴール集合。実業務の「形」だけを写し、中身は一切写さない。

旧集合は 111ゴール中 96.4% が1ターンで終わり 98.2% が DONE だった。
turns の下限は1、完了の上限は全部なので、どんな計器も下方向しか検出できなかった。
較正では直らない -- 定規ではなく作業負荷の問題。
"""
import csv
import os

import pytest

from scripts import workload_multiturn as W


@pytest.fixture
def built(tmp_path):
    d = str(tmp_path / "wl")
    return d, W.build(d)


# ---- 公開リポジトリに実業務が漏れないこと ----------------------------------------------------------

def test_no_organisation_or_product_identifier_appears():
    """このファイルは追跡対象で、リポジトリは公開。
    仕事の形は写してよく、名前は一切写せない。"""
    import io
    import re
    src = io.open(W.__file__, encoding="utf-8").read().lower()
    # 語境界で見る。素の部分一致だと "dic" が "dict" に当たり、
    # 守りたい不変条件が『通らない検査』として消される側に回る。
    for banned in ("resonac", "m118a8586", "shuttle", "kiyus"):
        assert not re.search(r"%s" % re.escape(banned), src), banned
    for banned in ("scm連携", "駒井", "川崎", "業務資料", "pap調査"):
        assert banned not in src, banned


def test_the_data_is_generated_not_copied():
    """実ファイルを読み込む経路が無いこと。形だけを真似る。"""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(W.build).lstrip())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "rng" in names, "生成していない"
    src = inspect.getsource(W.build)
    assert "Desktop" not in src and "Downloads" not in src


# ---- 答えが推測で当たらないこと -------------------------------------------------------------------

def test_the_answer_is_a_real_subset(built):
    """『全部』も『なし』も外れること。答えを出す近道があるゴールは何も測らない。"""
    _d, facts = built
    assert 0 < facts["n_over"] < 12
    assert 3 <= facts["n_over"] <= 9, facts["n_over"]


def test_the_answer_is_stable_across_builds(tmp_path):
    """固定シード。同じゴールが毎回同じ答えを持たないと、
    受入検証が正解を主張できない。"""
    a = W.build(str(tmp_path / "a"))
    b = W.build(str(tmp_path / "b"))
    assert a["over_limit"] == b["over_limit"]
    assert (a["worst_lot"], a["worst_day"]) == (b["worst_lot"], b["worst_day"])


def test_the_check_asserts_the_generated_answer_rather_than_recomputing_it():
    """検証がゴールと同じ手順で答えを出し直すと、間違った答えにも同意する。"""
    goals = W.goals()
    cmd = goals[0]["checks"][1]["cmd"]
    assert "expected=" in cmd
    assert "csv" not in cmd.split("expected=")[0], "検証側が入力を読み直している"


# ---- 複数ターンを要する形になっていること ----------------------------------------------------------

def test_the_first_goal_needs_two_files_joined(built):
    """readings だけでも limits だけでも答えが出ないこと。
    それが『1ターンで終わらない』の実体で、水増しではない。"""
    d, facts = built
    readings = list(csv.DictReader(open(os.path.join(d, "readings.csv"), encoding="utf-8")))
    limits = list(csv.DictReader(open(os.path.join(d, "limits.csv"), encoding="utf-8")))
    assert {r["lot"] for r in readings} == {r["lot"] for r in limits}
    assert "limit" not in readings[0] and "value" not in limits[0]


def test_a_decoy_file_is_present(built):
    """『2つの CSV』と言われたフォルダに3つある、は実務の普通の姿。
    間違ったものを読んだハーネスは、エラーではなく自信のある誤答を出す。"""
    d, _facts = built
    assert os.path.exists(os.path.join(d, "notes.csv"))
    assert W.goals(d)[0]["text"].count("notes.csv") == 1


# ---- 前回の残骸で通ってしまわないこと --------------------------------------------------------------

def test_clean_removes_the_inputs_too(built):
    """`file_exists` は昨日の出力でも通る。入力も作り直さないと、
    古い表を読んだ答えが今日の答えとして採点される。"""
    d, _facts = built
    open(os.path.join(d, "over_limit.txt"), "w", encoding="utf-8").write("stale")
    W.clean(d)
    for name in ("over_limit.txt", "readings.csv", "limits.csv"):
        assert not os.path.exists(os.path.join(d, name)), name


# ---- クラス分割がそのまま効くこと -----------------------------------------------------------------

def test_the_set_still_splits_into_two_classes():
    """受入検証の有無でクラスを分ける仕組みは、この集合でもそのまま働く。"""
    from relay.selfimprove import planner_evaluator as PE
    from relay.relay_fleet import goal_fields
    goals = W.goals()
    seen = {PE.class_of(goal_fields(g)[0], goals) for g in goals}
    assert seen == {PE.VERIFIED, PE.UNVERIFIED}


def test_the_headroom_claim_is_marked_as_unmeasured():
    """『何ターンかかるか』はまだ測っていない。
    設計上そう作った、というだけでは主張にならない。"""
    assert "HEADROOM IS A CLAIM UNTIL IT IS MEASURED" in (W.__doc__ or "")

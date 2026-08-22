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
    # TWO SEPARATE FAULTS LIVED IN THESE FOUR LINES.
    #
    # The word boundaries were not word boundaries. The pattern reached this file as a
    # plain string rather than a raw one, so \b became a literal backspace and the regex
    # searched for control-character-delimited words. Nothing matches that, so the
    # assertion could not fail -- the comment above warns in as many words that losing the
    # boundary turns the invariant into a check that never fires, and that is exactly what
    # it had become. Measured before the fix: the broken pattern finds nothing in a string
    # that openly contains the word.
    #
    # And the guard was the leak. This file is tracked and the repository is public, so a
    # plaintext list of the identifiers that may not appear published them here: a grep of
    # the remote tree for any of the nine found exactly one file, and it was this one. Two
    # of them are real people's names. Assembled from fragments and escapes now; the
    # comparison is byte-identical and test_this_file_is_not_itself_the_leak keeps it so.
    for banned in _banned_ascii():
        assert not re.search(_word_pattern(banned), src), banned
    for banned in _banned_terms():
        assert banned not in src, banned


def _word_pattern(banned):
    """The pattern the guard actually uses, so the tests below exercise IT and not a copy.

    A copy is how this broke: the boundaries were lost here as literal backspace characters
    -- most likely a plain string where a raw one was meant, which is a mistake this very
    session reproduced twice while editing the file -- and no test was looking at the guard's
    own pattern, so a check that could never fire read exactly like a check that passed."""
    import re
    return r"\b%s\b" % re.escape(banned)

def _banned_ascii():
    """Organisation, employee and host identifiers, split so this file does not hold them."""
    return ("res" + "onac", "m118" + "a8586", "shu" + "ttle", "ki" + "yus")


def _banned_terms():
    """Personal names and business-document words, escaped for the same reason."""
    return ("scm\u9023\u643a", "\u99d2\u4e95", "\u5ddd\u5d0e", "\u696d\u52d9\u8cc7\u6599", "pap\u8abf\u67fb")


def test_the_boundary_is_a_boundary():
    """The comment above says why: a bare substring match puts "dic" inside "dict", and an
    invariant that fires on innocent text is removed rather than fixed. Pinned because the
    boundary has already been lost once -- as literal backspace characters, which made the
    check unable to fail at all."""
    import re
    for banned in _banned_ascii():
        pat = _word_pattern(banned)
        assert not re.search(pat, "xx" + banned + "xx"), banned
        assert re.search(pat, "xx " + banned + " xx"), banned


def test_the_boundary_pattern_can_actually_fail():
    """The assertion above is only worth having if it can fire. It stopped being able to,
    and nothing noticed, because a check that never fails looks like a check that passes."""
    import re
    for banned in _banned_ascii():
        assert re.search(_word_pattern(banned), "x " + banned + " y")


def test_this_file_is_not_itself_the_leak():
    """The check above reads another module; nothing read THIS one, which is how a tracked,
    public file came to hold every forbidden word in plaintext."""
    import io
    src = io.open(__file__, encoding="utf-8").read().lower()
    for banned in tuple(_banned_ascii()) + tuple(_banned_terms()):
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
    for name in ("over_limit.txt", "worst.txt", "agenda.txt",
                 "readings.csv", "limits.csv"):
        assert not os.path.exists(os.path.join(d, name)), name


# ---- 独立ワーカーで走ることが文面に織り込まれていること ----------------------------------------

def test_no_goal_points_outside_its_own_text(tmp_path):
    """各ゴールは自分専用のワーカーで走り、前のゴールの会話は存在しない。
    「同じフォルダ」と書いたゴールは、誰も教えていないフォルダを13ターンかけて
    ディスク全体から探し、その空回りが turns_gain 2.25 として記録された。
    実測 2026-08-22 の帰無走行。"""
    d = str(tmp_path / "wl")
    for g in W.goals(d):
        text = g["text"]
        assert d in text, "フォルダを名指ししていない: %s" % text[:60]
        for pointer in ("同じフォルダ", "同フォルダ", "上記", "先ほど", "さきほど", "前のターン"):
            assert pointer not in text, (pointer, text[:60])


def test_every_goal_carries_an_acceptance_check(tmp_path):
    """検査の無いゴールは落ちようがないので、足場を失ったハーネスは
    ターン上限まで回り続け、その空回りが測定値として記録される。"""
    for g in W.goals(str(tmp_path / "wl")):
        assert g.get("checks"), g["text"][:60]


def test_the_class_split_collapses_and_the_test_says_so(tmp_path):
    """代償の記録。`class_of` は「検査の有無」でクラスを分けるので、
    全ゴールに検査を付けたこの集合は単一クラスになり、
    planner_evaluator の相殺検出はこの集合では働かない。
    別の軸(ローカル資料 / Work IQ)で分け直すまで、それは使えない。"""
    from relay.selfimprove import planner_evaluator as PE
    from relay.relay_fleet import goal_fields
    goals = W.goals(str(tmp_path / "wl"))
    seen = {PE.class_of(goal_fields(g)[0], goals) for g in goals}
    assert seen == {PE.VERIFIED}, seen


# ---- 伸びしろが実測で語られていること --------------------------------------------------------------

def test_the_docstring_records_the_measurement_not_the_intention():
    """『複数ターンかかるように作った』は主張であって測定ではない。
    実測では4ゴール中3つが両腕とも1ターンで、しかも正解だった。"""
    doc = W.__doc__ or ""
    assert "THREE OF FOUR FINISHED IN ONE TURN IN BOTH ARMS" in doc
    assert "NO GOAL MAY POINT OUTSIDE ITS OWN TEXT" in doc
    assert "EVERY GOAL CARRIES AN ACCEPTANCE CHECK" in doc


# ---- 腕どうしが独立した単位であること --------------------------------------------------------------

def test_reset_outputs_removes_the_answers_and_keeps_the_inputs(built):
    """腕2は腕1と同じゴールを同じフォルダで走らせるので、これが無いと
    腕1の完成品を見て開始する。`file_exists` も内容一致も通ってしまい、
    やっていない仕事が完了として記録される。偏りは常に後攻に有利で、
    それは処置ではなく腕の順序。"""
    d, _facts = built
    for name in W.ANSWERS:
        open(os.path.join(d, name), "w", encoding="utf-8").write("arm1 の成果")
    W.reset_outputs(d)
    for name in W.ANSWERS:
        assert not os.path.exists(os.path.join(d, name)), name
    for name in ("readings.csv", "limits.csv", "notes.csv"):
        assert os.path.exists(os.path.join(d, name)), "入力まで消している: %s" % name


def test_the_campaign_ships_that_reset_with_the_multiturn_set():
    """フックがあっても渡していなければ何も起きない。"""
    from scripts.run_route_campaign import active_goals
    _g, name, reset = active_goals(["run", "--multiturn"])
    assert name == "multiturn"
    assert reset is W.reset_outputs
    _g2, name2, reset2 = active_goals(["run"])
    assert (name2, reset2) == ("saturated-v1", None)


def test_the_evaluator_calls_the_reset_at_the_top_of_every_arm():
    """構造検査。腕を1本走らせるには実ブラウザが要るので挙動では確かめられない。
    見ているのは2点: 腕の関数の中で呼ばれていること、そして
    `isolate_memory` の分岐の中に入っていないこと(記憶隔離を切ると
    リセットも黙って消える、という壊れ方をさせない)。"""
    import ast
    import inspect
    from relay.selfimprove import scheduler as S

    assert "arm_reset" in inspect.signature(S.route_evaluator_for).parameters
    tree = ast.parse(inspect.getsource(S.route_evaluator_for).lstrip())
    run = next(n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_run")
    calls = [n for n in ast.walk(run)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "arm_reset"]
    assert calls, "_run が arm_reset を呼んでいない"
    guarded = [n for n in ast.walk(run)
               if isinstance(n, ast.If)
               and any(isinstance(x, ast.Name) and x.id == "isolate_memory"
                       for x in ast.walk(n.test))
               and any(c in ast.walk(n) for c in calls)]
    assert not guarded, "記憶隔離の分岐の中に入っている"


def test_the_goals_never_hand_over_an_8_3_short_path():
    """`TEMP` はこの端末では 8.3 短縮形を返す。短縮名を含むパスを渡すと、
    測っているのは作業ではなくパス表記の扱いになる。2026-08-23 の帰無走行で、
    同一ハーネスの片腕が3ターン使い、その理由が『短縮名で書いたので
    対象フォルダに実体が残らなかった』だった。"""
    assert "~" not in W.WORKDIR, W.WORKDIR
    for g in W.goals():
        assert "~" not in g["text"], g["text"][:80]

"""レビュアー配分。既存の adaptive 実装を「名前」から「所見」に変えるための実験。

このファイルの中心は1つ: **選ばなかったレンズが何と言ったかを知らずに false accept は
測れない**。部分集合方策を、それが実際に回したレンズだけで採点すると「回したレンズ同士が
一致したか」を測ることになり、これは情報がほぼゼロで、しかも所見のように見える。

だから `simulate` は欠けたセルを拒否する。欠損を「反証されなかった」と読む既定は、
測っていない場所でちょうど安い方策を良く見せる方向へ倒れる。
"""
import pytest

from relay.selfimprove import reviewer_allocation as A


LENSES = ("correctness", "security", "perf", "repro", "style")


def _row(cid, bad, refuting, *, security=A.SECURITY_PASS, unclear=()):
    """`refuting` は反証するレンズの集合。全レンズ分の判定を必ず埋める。

    `bad` はグレーダーの形。単一 bool に潰すと、security だけを取り逃す方策が
    集計上は Pareto 優位に見え、しかも security の正しい反証が false reject に
    数えられて frontier が security レンズへの支出を罰する。"""
    return {"candidate_id": cid,
            "bad": {"functional": not bad, "security": security},
            "features": {"kind": "code"},
            "verdicts": {lens: (A.UNCLEAR if lens in unclear
                                else A.REFUTED if lens in refuting else A.UPHELD)
                         for lens in LENSES}}


def _corpus():
    return [
        _row("c1", True, {"correctness"}),          # 安いパネルが取り逃しうる
        _row("c2", True, {"style"}),                # 最後のレンズだけが捕まえる
        _row("c3", True, {"correctness", "perf"}),
        _row("c4", False, set()),                   # 良い候補、誰も反証しない
        _row("c5", False, {"style"}),               # 良い候補を style が誤って反証
    ]



def _point(policy, *, catchable_fa, sec_fa, fr, calls, candidates=5, bad=8, catchable=8):
    """A frontier point in the shape `simulate` now returns.

    `false_accept_catchable` は headline 軸 -- どのレンズも捕まえない候補は全方策を
    等しく押し下げ、frontier を「どれも同じだから安い方策で」に潰す。それは
    弱いパネルが、誰も測っていない問いに答えたふりをする形。
    """
    return {"policy": policy, "false_accept": catchable_fa + 1,
            "false_accept_catchable": catchable_fa, "false_accept_security": sec_fa,
            "false_reject": fr, "review_calls": calls,
            "candidates": candidates, "bad_candidates": bad, "catchable_bad": catchable}


# ---- 中心: 全レンズ走行でなければ採点できない --------------------------------------------------

def test_a_corpus_missing_a_lens_verdict_is_refused():
    """欠損を『反証なし』と読むと、測っていない場所で安い方策が良く見える。"""
    rows = _corpus()
    del rows[0]["verdicts"]["security"]
    with pytest.raises(A.AllocationError) as exc:
        A.simulate(rows, A.FIXED, k=2)
    assert "skipped" in str(exc.value)


def test_a_corpus_without_ground_truth_is_refused():
    """レビュアーに正解を供給させたら、レビュアーを自分で採点させることになる。"""
    rows = _corpus()
    rows[0]["bad"] = "yes"
    with pytest.raises(A.AllocationError) as exc:
        A.simulate(rows, A.FIXED, k=2)
    assert "ground truth" in str(exc.value)


def test_an_empty_corpus_compares_nothing():
    with pytest.raises(A.AllocationError):
        A.simulate([], A.ALL, k=2)


# ---- 4方策 -------------------------------------------------------------------------------------

def test_all_lenses_never_falsely_accepts_within_this_corpus():
    """全部回せば、どのレンズが捕まえる候補も捕まる -- 上限としての基準線。"""
    got = A.simulate(_corpus(), A.ALL, k=2)
    assert got["false_accept"] == 0
    assert got["review_calls"] == len(LENSES) * 5


def test_a_fixed_panel_misses_what_its_first_k_do_not_see():
    """先頭2枚は correctness と perf。style だけが捕まえる c2 を取り逃す。"""
    got = A.simulate(_corpus(), A.FIXED, k=2)
    assert got["false_accept"] == 1
    assert got["review_calls"] == 10


def test_the_random_policy_refuses_to_run_unseeded():
    """再現できないアームは、p値付きの逸話。"""
    with pytest.raises(A.AllocationError) as exc:
        A.choose(A.RANDOM, LENSES, k=2)
    assert "seed" in str(exc.value)


def test_the_same_seed_gives_the_same_run_twice():
    a = A.simulate(_corpus(), A.RANDOM, k=2, seed_base=7)
    b = A.simulate(_corpus(), A.RANDOM, k=2, seed_base=7)
    assert a["false_accept"] == b["false_accept"]
    c = A.simulate(_corpus(), A.RANDOM, k=2, seed_base=99)
    assert isinstance(c["false_accept"], int)


def test_the_adaptive_policy_without_its_memory_is_the_fixed_one_relabelled():
    with pytest.raises(A.AllocationError) as exc:
        A.choose(A.ADAPTIVE, LENSES, k=2, features={})
    assert "wearing a different label" in str(exc.value)


def test_the_adaptive_policy_is_asked_exactly_once_per_candidate(tmp_path):
    """`select_lenses` は探索カウンタを進めてディスクに書く。判定用とレイテンシ用で
    二度呼ぶと探索周期が倍になり、測っている当の挙動が変わる。"""
    calls = []

    class _Mem:
        def select_lenses(self, features, cands, k):
            calls.append(k)
            return list(cands)[:k]

    A.simulate(_corpus(), A.ADAPTIVE, k=2, memory=_Mem(), allow_cold_start=True)
    assert len(calls) == 5, "候補5件に対して %d 回呼ばれた" % len(calls)


def test_an_unknown_policy_is_an_error_not_a_default():
    with pytest.raises(A.AllocationError):
        A.choose("cheapest", LENSES, k=2)


# ---- 指標の形 ------------------------------------------------------------------------------------

def test_counts_are_returned_not_only_rates():
    """分母の無い率は、後から束ねることも区間を付けることもできない。"""
    got = A.simulate(_corpus(), A.FIXED, k=2)
    for key in ("false_accept", "false_reject", "true_accept", "true_reject",
                "candidates", "bad_candidates", "good_candidates"):
        assert isinstance(got[key], int), key
    assert got["true_accept"] + got["true_reject"] + got["false_accept"] + \
        got["false_reject"] == got["candidates"]


def test_latency_is_the_slowest_lens_not_their_sum():
    """レンズは別ページで並行に走る。和を課すと『2枚は1枚の倍の実時間』と主張する。"""
    costs = {lens: 1.0 for lens in LENSES}
    costs["security"] = 5.0
    got = A.simulate(_corpus(), A.ALL, k=5, lens_cost=costs)
    assert got["latency_per_candidate"] == 5.0
    assert got["cost"] == 5 * (4 * 1.0 + 5.0), "コストのほうは和で正しい"


def test_a_good_candidate_refuted_by_a_chosen_lens_is_a_false_reject():
    got = A.simulate(_corpus(), A.ALL, k=5)
    assert got["false_reject"] == 1     # c5 は style に誤って反証される


# ---- Pareto -------------------------------------------------------------------------------------

def test_a_dominated_policy_is_named_as_dominated():
    results = [
        _point("a", catchable_fa=0, sec_fa=0, fr=0, calls=10),
        _point("b", catchable_fa=1, sec_fa=1, fr=1, calls=20),
    ]
    got = A.frontier(results)
    assert got["frontier"] == ["a"] and got["dominated"] == ["b"]


def test_a_genuine_trade_off_keeps_both_points():
    """安いが取り逃す方策と、高いが取り逃さない方策は、どちらも支配されない。"""
    results = [
        _point("cheap", catchable_fa=2, sec_fa=0, fr=0, calls=10),
        _point("thorough", catchable_fa=0, sec_fa=0, fr=0, calls=25),
    ]
    assert A.frontier(results)["frontier"] == ["cheap", "thorough"]


def test_no_winner_is_declared():
    """frontier 上のどれを採るかは『見逃し1件はレビュー何回分か』という判断であって、
    データについての事実ではない。"""
    results = [
        _point("cheap", catchable_fa=2, sec_fa=0, fr=0, calls=10),
        _point("thorough", catchable_fa=0, sec_fa=0, fr=0, calls=25),
    ]
    got = A.frontier(results)
    # 判断を担うのはフィールドであって散文ではない。全文を文字列検索すると、
    # 「best な frontier を選んで報告すると過適合する」という**警告**にまで反応して
    # しまい、テストが本来の主張から離れる。
    assert set(got) <= {"frontier", "dominated", "note", "bad_candidates",
                        "does_not_support"}, "推奨を運べる新しいフィールドが増えている"
    assert got["frontier"] == ["cheap", "thorough"], "片方を落として一つに絞っている"
    for key in ("winner", "best_policy", "recommended", "choose"):
        assert key not in got, "推奨フィールド %r がある" % key


def test_a_thin_corpus_says_so_rather_than_drawing_a_confident_frontier():
    """悪い候補が3件なら、false accept は1件のブレで動く。"""
    results = [_point("a", catchable_fa=0, sec_fa=0, fr=0, calls=10, catchable=8,
                      bad=8)]
    assert "FEWER THAN TWENTY" in A.frontier(results)["note"]


def test_a_thick_corpus_does_not_carry_the_warning():
    results = [_point("a", catchable_fa=3, sec_fa=0, fr=1, calls=400, candidates=200,
                      bad=60, catchable=55)]
    assert "FEWER THAN TWENTY" not in A.frontier(results)["note"]


# ---- 入力の厳格さ（レビュー指摘） ---------------------------------------------------------------

def test_a_non_boolean_verdict_is_refused_not_coerced():
    """`bool("false")` は True。この継ぎ目では「反証しなかったレンズ」が
    「反証した」に化ける。"""
    rows = _corpus()
    rows[0]["verdicts"]["security"] = "false"
    with pytest.raises(A.AllocationError) as exc:
        A.simulate(rows, A.ALL, k=5)
    assert "must be one of REFUTED, UPHELD, UNCLEAR" in str(exc.value)


def test_a_duplicated_candidate_is_refused():
    """繰り返しは2件目の観測ではない。1件として数えると、後で計算する区間が狭くなる。"""
    rows = _corpus() + [_row("c1", True, {"correctness"})]
    with pytest.raises(A.AllocationError) as exc:
        A.simulate(rows, A.ALL, k=5)
    assert "appears twice" in str(exc.value)


def test_a_candidate_without_an_id_is_refused():
    rows = _corpus()
    rows[0]["candidate_id"] = ""
    with pytest.raises(A.AllocationError):
        A.simulate(rows, A.ALL, k=5)


def test_the_random_draw_is_stable_when_the_corpus_grows():
    """行位置で種を作ると、候補を1件足すだけで以降の割付が全部変わり、
    同じ名前の別実験になる。"""
    base = _corpus()
    grown = [_row("c0", False, set())] + base
    a = {r["candidate_id"]: A.choose(A.RANDOM, LENSES, k=2,
                                     seed=A._candidate_seed(5, r["candidate_id"]))
         for r in base}
    b = {r["candidate_id"]: A.choose(A.RANDOM, LENSES, k=2,
                                     seed=A._candidate_seed(5, r["candidate_id"]))
         for r in grown if r["candidate_id"] in a}
    assert a == b


# ---- 供給された方策オブジェクトを信用しない -----------------------------------------------------

def test_a_policy_that_selects_nothing_is_refused():
    """空選択は全候補をゼロ費用で受理する -- frontier 上で最安に見え、最も誤っている。"""
    class _Empty:
        def select_lenses(self, features, cands, k):
            return []
    with pytest.raises(A.AllocationError) as exc:
        A.simulate(_corpus(), A.ADAPTIVE, k=2, memory=_Empty(), allow_cold_start=True)
    assert "zero cost" in str(exc.value)


def test_a_policy_that_selects_outside_the_panel_is_refused():
    class _Wild:
        def select_lenses(self, features, cands, k):
            return ["telepathy"]
    with pytest.raises(A.AllocationError) as exc:
        A.simulate(_corpus(), A.ADAPTIVE, k=2, memory=_Wild(), allow_cold_start=True)
    assert "not in the panel" in str(exc.value)


def test_a_policy_that_repeats_a_lens_is_refused():
    """重複は呼び出し数と費用を水増しする -- 費用軸が効いている実験では効く。"""
    class _Dup:
        def select_lenses(self, features, cands, k):
            return [cands[0], cands[0]]
    with pytest.raises(A.AllocationError):
        A.simulate(_corpus(), A.ADAPTIVE, k=2, memory=_Dup(), allow_cold_start=True)


def test_a_policy_that_exceeds_its_budget_is_refused():
    class _Greedy:
        def select_lenses(self, features, cands, k):
            return list(cands)
    with pytest.raises(A.AllocationError) as exc:
        A.simulate(_corpus(), A.ADAPTIVE, k=2, memory=_Greedy(), allow_cold_start=True)
    assert "budget" in str(exc.value)


# ---- 同じ方策が2点として現れない -----------------------------------------------------------------

def test_case_does_not_split_one_policy_into_two_frontier_points():
    a = A.simulate(_corpus(), "ALL", k=5)
    b = A.simulate(_corpus(), "all", k=5)
    assert a["policy"] == b["policy"] == A.ALL


# ---- クラスタと、支えられない主張の明示 -----------------------------------------------------------

def test_declared_clusters_are_counted_and_absence_is_not_invented():
    rows = _corpus()
    for i, r in enumerate(rows):
        r["cluster"] = "task%d" % (i % 2)
    assert A.simulate(rows, A.ALL, k=5)["clusters"] == 2
    assert A.simulate(_corpus(), A.ALL, k=5)["clusters"] is None, (
        "宣言されていないクラスタを1件1クラスタと決めつけている")


def test_the_output_names_what_it_cannot_support():
    """読者が『支えられている』と誤解しうる主張は、発見されるのを待たずに書く。

    捕捉可能な悪い候補が十分ある場合の出力を見る -- 足りない場合は frontier 自体を
    拒否するので、そちらは別のテスト。"""
    # `_corpus() * 3` は同じ dict を3回参照するので、id を書き換えても重複が残る。
    rich = [dict(row, candidate_id="r%d" % i)
            for i, row in enumerate(_corpus() + _corpus() + _corpus())]
    got = A.frontier([A.simulate(rich, A.ALL, k=5)])
    joined = " ".join(got["does_not_support"]).lower()
    for topic in ("generalisation", "severity", "label certainty", "tuning"):
        assert topic in joined, topic


# ---- 冷えた adaptive は fixed である（実測に基づく） --------------------------------------------

def test_an_untrained_adaptive_arm_is_refused_because_it_is_the_fixed_arm():
    """本リポジトリの実際の store は空で、その状態で adaptive は fixed と
    10/10 候補で同一の選択をした。名前を2つ持つ1つの方策を frontier に並べると、
    方策を自分自身と比べたことになる -- 同じ形の失敗がこのリポジトリには既に
    記録されている（両アームが同じプログラムで、p値はノイズを記述していた）。"""
    class _Cold:
        data = {"cells": {}, "selects": 0}

        def select_lenses(self, features, cands, k):
            return list(cands)[:k]

    with pytest.raises(A.AllocationError) as exc:
        A.simulate(_corpus(), A.ADAPTIVE, k=2, memory=_Cold())
    assert "one policy under two names" in str(exc.value)


def test_a_warmed_memory_is_accepted():
    class _Warm:
        data = {"cells": {"b|correctness": {"refute": 3, "total": 9}}, "selects": 9}

        def select_lenses(self, features, cands, k):
            return list(cands)[:k]

    got = A.simulate(_corpus(), A.ADAPTIVE, k=2, memory=_Warm())
    assert got["adaptive_observations"] == 9


def test_the_cold_start_can_be_measured_deliberately():
    """冷えた挙動そのものを測りたい走行は正当。ただし明示的に言わせる。"""
    class _Cold:
        data = {"cells": {}}

        def select_lenses(self, features, cands, k):
            return list(cands)[:k]

    got = A.simulate(_corpus(), A.ADAPTIVE, k=2, memory=_Cold(), allow_cold_start=True)
    assert got["adaptive_observations"] == 0


def test_the_observation_count_is_carried_so_a_reader_need_not_infer_it():
    """『誰も触れなかった』から『学習していた』を推測させない。"""
    got = A.simulate(_corpus(), A.FIXED, k=2)
    assert got["adaptive_observations"] is None


# ---- レビューが指摘したこと（Fable, 2026-08-19） -------------------------------------------------

def test_a_security_only_miss_is_visible_rather_than_averaged_away():
    """security だけを取り逃す方策は、集計上どの軸でも優位に見える。
    しかもそれは**最も高くつく取り逃し**で、正解ラベルが最も弱いクラス。"""
    rows = [
        _row("s1", False, {"security"}, security=A.SECURITY_VIOLATION),
        _row("s2", True, {"correctness"}),
    ]
    # 先頭2枚は correctness / security... パネル順に依存しないよう明示的に確認
    cheap = A.simulate(rows, A.FIXED, k=1)      # correctness のみ
    assert cheap["false_accept_security"] >= 1, cheap
    full = A.simulate(rows, A.ALL, k=3)
    assert full["false_accept_security"] == 0


def test_an_unevaluable_security_row_is_excluded_rather_than_passed():
    """`GradeResult` が既に拒否した崩壊。『見なかった』は『起きなかった』ではない。"""
    rows = [_row("u1", False, set(), security=A.SECURITY_UNEVALUABLE)]
    got = A.simulate(rows, A.ALL, k=3)
    assert got["security_unevaluable"] == 1
    assert got["false_accept_security"] == 0, "評価不能な行を security の失敗に数えている"


def test_a_bare_boolean_ground_truth_is_refused():
    """3つの主張（誤り・脆い・危険）を1ビットに潰すと、どれを取り逃したか消える。"""
    rows = [{"candidate_id": "b1", "bad": True, "features": {},
             "verdicts": {lens: A.UPHELD for lens in LENSES}}]
    with pytest.raises(A.AllocationError) as exc:
        A.simulate(rows, A.ALL, k=3)
    assert "grader's shape" in str(exc.value)


def test_unclear_is_carried_and_coerced_where_it_can_be_seen():
    """`parse_verdict` は三値で、フリートは UNCLEAR を『阻止しない』と扱う。
    コーパスを bool にした時点でその強制は済んでおり、誰も見ていない。"""
    rows = [_row("q1", True, set(), unclear={"correctness"})]
    lenient = A.simulate(rows, A.ALL, k=3)
    assert lenient["false_accept"] == 1, "UNCLEAR が捕捉として数えられている"
    strict = A.simulate(rows, A.ALL, k=3, unclear_refutes=True)
    assert strict["false_accept"] == 0
    assert strict["unclear_counted_as_refutation"] is True


def test_false_accept_is_reported_against_the_panel_ceiling():
    """どのレンズも捕まえない候補は全方策を等しく押し下げ、frontier を潰す。
    弱いパネルが『どれも同じ、安いのでよい』に化ける形。"""
    rows = [
        _row("n1", True, set()),          # 誰も捕まえない -- 天井の側
        _row("n2", True, {"correctness"}),
    ]
    got = A.simulate(rows, A.FIXED, k=1)
    assert got["false_accept"] == 1 and got["false_accept_catchable"] == 0
    assert got["catchable_bad"] == 1


def test_a_frontier_over_too_few_catchable_failures_is_refused():
    """5件の事象で描いた frontier は、結論の形をした5件の事象。"""
    got = A.frontier([_point("a", catchable_fa=0, sec_fa=0, fr=0, calls=10,
                             bad=3, catchable=2)])
    assert got["frontier"] == []
    assert "nothing here to separate policies" in got["note"]


# ---- 「捕まえた」と「なぜ捕まえたか」は別 ---------------------------------------------------------

def _twin_rows(bad_verdicts, good_verdicts, twin="t1"):
    lenses = ("correctness", "edge", "security")
    def mk(cid, functional, verdicts):
        return {"candidate_id": cid, "twin_of": twin,
                "bad": {"functional": functional, "security": A.SECURITY_PASS},
                "verdicts": dict(zip(lenses, verdicts))}
    return [mk("good", True, good_verdicts), mk("bad", False, bad_verdicts)]


def test_a_lens_that_refutes_both_twins_detected_the_style_not_the_defect():
    """最初の種まきコーパスで実際に起きたこと: 黙る違反が3倍捕まったのは、
    パネルが違反でなく**空の返信**を検知していたから。双子は返信様式を揃え、
    欠陥の有無だけを変えるので、両方を反証したレンズは共通部分に反応している。"""
    rows = _twin_rows((A.REFUTED,) * 3, (A.REFUTED,) * 3)
    bad = [r for r in rows if r["candidate_id"] == "bad"][0]
    assert A.distinguishing_lenses(bad, rows) == set()


def test_a_lens_that_separates_the_twins_is_a_real_catch():
    rows = _twin_rows((A.REFUTED, A.UPHELD, A.UPHELD), (A.UPHELD,) * 3)
    bad = [r for r in rows if r["candidate_id"] == "bad"][0]
    assert A.distinguishing_lenses(bad, rows) == {"correctness"}


def test_a_row_without_a_twin_keeps_every_refutation():
    """実候補には双子が無い。そこまで割り引くと、本物の観測を静かに捨てる。"""
    solo = {"candidate_id": "s", "bad": {"functional": False, "security": A.SECURITY_PASS},
            "verdicts": dict(zip(("correctness", "edge", "security"),
                                 (A.REFUTED, A.UPHELD, A.UPHELD)))}
    assert A.distinguishing_lenses(solo, [solo]) == {"correctness"}


def test_style_only_catches_do_not_inflate_the_catchable_count():
    """catchable が frontier の品質軸の分母。ここが水増しされると、
    『5件あるので線を引ける』が偽になる。"""
    rows = (_twin_rows((A.REFUTED,) * 3, (A.REFUTED,) * 3, twin="t1")
            + _twin_rows((A.REFUTED, A.UPHELD, A.UPHELD), (A.UPHELD,) * 3, twin="t2"))
    for i, r in enumerate(rows):
        r["candidate_id"] = "c%d" % i
    got = A.simulate(rows, A.ALL, k=3)
    assert got["bad_candidates"] == 2
    assert got["catchable_bad"] == 1, (
        "様式検知だけの双子を捕捉可能に数えている: %s" % got)

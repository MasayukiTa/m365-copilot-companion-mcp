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


def _row(cid, bad, refuting):
    """`refuting` は反証するレンズの集合。全レンズ分の判定を必ず埋める。"""
    return {"candidate_id": cid, "bad": bad, "features": {"kind": "code"},
            "verdicts": {lens: (lens in refuting) for lens in LENSES}}


def _corpus():
    return [
        _row("c1", True, {"correctness"}),          # 安いパネルが取り逃しうる
        _row("c2", True, {"style"}),                # 最後のレンズだけが捕まえる
        _row("c3", True, {"correctness", "perf"}),
        _row("c4", False, set()),                   # 良い候補、誰も反証しない
        _row("c5", False, {"style"}),               # 良い候補を style が誤って反証
    ]


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
        {"policy": "a", "false_accept": 0, "false_reject": 0, "review_calls": 10,
         "candidates": 5, "bad_candidates": 3},
        {"policy": "b", "false_accept": 1, "false_reject": 1, "review_calls": 20,
         "candidates": 5, "bad_candidates": 3},
    ]
    got = A.frontier(results)
    assert got["frontier"] == ["a"] and got["dominated"] == ["b"]


def test_a_genuine_trade_off_keeps_both_points():
    """安いが取り逃す方策と、高いが取り逃さない方策は、どちらも支配されない。"""
    results = [
        {"policy": "cheap", "false_accept": 2, "false_reject": 0, "review_calls": 10,
         "candidates": 5, "bad_candidates": 3},
        {"policy": "thorough", "false_accept": 0, "false_reject": 0, "review_calls": 25,
         "candidates": 5, "bad_candidates": 3},
    ]
    assert A.frontier(results)["frontier"] == ["cheap", "thorough"]


def test_no_winner_is_declared():
    """frontier 上のどれを採るかは『見逃し1件はレビュー何回分か』という判断であって、
    データについての事実ではない。"""
    results = [
        {"policy": "cheap", "false_accept": 2, "false_reject": 0, "review_calls": 10,
         "candidates": 5, "bad_candidates": 3},
        {"policy": "thorough", "false_accept": 0, "false_reject": 0, "review_calls": 25,
         "candidates": 5, "bad_candidates": 3},
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
    results = [{"policy": "a", "false_accept": 0, "false_reject": 0, "review_calls": 10,
                "candidates": 5, "bad_candidates": 3}]
    assert "FEWER THAN TWENTY" in A.frontier(results)["note"]


def test_a_thick_corpus_does_not_carry_the_warning():
    results = [{"policy": "a", "false_accept": 3, "false_reject": 1, "review_calls": 400,
                "candidates": 200, "bad_candidates": 60}]
    assert "FEWER THAN TWENTY" not in A.frontier(results)["note"]


# ---- 入力の厳格さ（レビュー指摘） ---------------------------------------------------------------

def test_a_non_boolean_verdict_is_refused_not_coerced():
    """`bool("false")` は True。この継ぎ目では「反証しなかったレンズ」が
    「反証した」に化ける。"""
    rows = _corpus()
    rows[0]["verdicts"]["security"] = "false"
    with pytest.raises(A.AllocationError) as exc:
        A.simulate(rows, A.ALL, k=5)
    assert "must be a bool" in str(exc.value)


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
    """読者が『支えられている』と誤解しうる主張は、発見されるのを待たずに書く。"""
    got = A.frontier([A.simulate(_corpus(), A.ALL, k=5)])
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

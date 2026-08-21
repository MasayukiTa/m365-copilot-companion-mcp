"""transport が routing でないこと、そして「進化させてよい部分」の線が守られること。

この線は概念ではなくコードの性質に乗っている。socket 側にだけ近道が1つ入れば
transport 選択は routing 選択に変わる。だからテストは名前ではなく不変条件を見る。
"""
import pytest

from relay.selfimprove import manifest as M
from relay.transport_policy import (SOCKET, TAB, TRANSPORT_VERSIONS, WORKIQ_MARKERS,
                                    _policy_v1, _policy_v2, classify_fallback,
                                    evolvable_fields, needs_workiq)


# ---- 版が実際に分岐すること -----------------------------------------------------------------------

def test_the_two_versions_answer_differently_for_the_same_goal():
    """両腕が同じ答えを返すなら A/B は同じプログラムの二腕。
    このリポジトリが4つのコンポーネントで見つけた欠陥。"""
    goal = "パーサのリファクタリング"
    assert _policy_v1(goal) == TAB
    assert _policy_v2(goal) == SOCKET


def test_v1_is_what_the_fleet_did_before_the_route_existed():
    for goal in ("何でも", "Outlook を整理", ""):
        assert _policy_v1(goal) == TAB


# ---- 固定された述語 -------------------------------------------------------------------------------

def test_workiq_goals_never_go_over_a_socket():
    for goal in ("Outlook の受信トレイを整理して", "SharePoint の資料をまとめて",
                 "Teams の会議メモを要約", "add a workiq connector"):
        assert needs_workiq(goal), goal
        assert _policy_v2(goal) == TAB, goal


def test_the_workiq_predicate_is_not_in_the_evolvable_set():
    """誤りが静かな次元を進化させると、『Work IQ 必要性の過少予測』が
    検知不能な形で開く。socket 経由で Work IQ 抜きの尤もらしい答えが返ると、
    フォールバックは鳴らず DONE に達し、ラベルは『socket で問題なし』と嘘をつく。"""
    fields = evolvable_fields()
    for banned in ("workiq", "WORKIQ_MARKERS", "needs_workiq"):
        assert not any(banned in f for f in fields), fields


def test_the_predicate_errs_toward_tabs():
    """偽の『Work IQ が要る』はタブ1枚分のメモリ。偽の『要らない』は
    必要なデータ抜きの答えで、ここでは誰も気づかない。非対称なので広く取る。"""
    assert len(WORKIQ_MARKERS) >= 10
    assert needs_workiq("メールの添付を確認して")


# ---- ラベルは3値 ---------------------------------------------------------------------------------

def test_route_caused_fallbacks_are_not_evidence_about_the_goal():
    """トークン失効や切断で学習すると『この時間帯のタスクはタブが要る』を学ぶ。"""
    for reason in ("token refresh failed", "ConnectionClosed: 1006",
                   "401 unauthorized", "handshake timeout"):
        assert classify_fallback(reason) == "route", reason


def test_task_caused_fallbacks_are_the_only_evidence_about_a_classification():
    for reason in ("the turn completed but carried no text (a card the tab can show?)",
                   "consent card appeared", "添付が必要"):
        assert classify_fallback(reason) == "task", reason


def test_an_unread_reason_stays_unknown_rather_than_being_folded_in():
    """route に寄せれば分類機を黙って免罪し、task に寄せれば黙ってノイズを学ぶ。
    第3の値のまま溜めて、人が読んでリストを延ばす -- それが固定述語の保守経路。"""
    assert classify_fallback("something nobody has read yet") == "unknown"
    assert classify_fallback("") == "unknown"


# ---- 片側ラベル問題 -------------------------------------------------------------------------------

def test_exploration_can_send_a_tab_prediction_over_a_socket():
    """タブに送ったタスクからは『タブは不要だった』というラベルが永遠に出ない。
    誤分類が安価なこの設定でだけ、探索が安全にできる。"""
    knobs = {"transport_eligible_kinds": ["code"]}
    assert _policy_v2("何かする", kind="other", knobs=knobs) == TAB
    assert _policy_v2("何かする", kind="other", knobs=knobs, explore=True) == SOCKET


def test_exploration_never_overrides_the_fixed_predicate():
    """探索が安全なのは誤分類の代償がフォールバックだから。Work IQ の欠落は
    フォールバックにならないので、そこへ探索を伸ばしてはいけない。"""
    assert _policy_v2("Outlook を開いて", kind="other",
                      knobs={"transport_eligible_kinds": ["code"]}, explore=True) == TAB


def test_the_policy_itself_does_not_draw_the_random_number():
    """1つの目標に2つの答えを返す成分は測定できない。探索の判断は呼び出し側。"""
    # docstring の中の "random" を拾わないこと。前にこれで一度落とした。
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(_policy_v2).lstrip())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not (names & {"random", "randrange", "choice", "uniform", "secrets"}), names


# ---- routing との境界 ----------------------------------------------------------------------------

def test_routing_stays_forbidden_while_transport_is_evolvable():
    assert "routing" in M.FORBIDDEN_COMPONENTS
    assert "transport" in M.EVOLVABLE_COMPONENTS
    assert "transport" not in M.FORBIDDEN_COMPONENTS


def test_the_invariant_is_written_beside_the_component_not_left_to_the_name():
    """『似ているから禁止』でも『名前が違うから可』でもなく、
    満たすべき条件が書いてあること。"""
    import io
    import re
    src = io.open(M.__file__, encoding="utf-8").read()
    i = src.index('"transport",')
    # コメント記号と改行を潰してから見る。行の折り方が変わるだけでこの検査が落ちると、
    # 次の人は不変条件を確かめるのをやめて assert を消す。
    block = re.sub(r"\s*#\s*", " ", src[max(0, i - 2000):i])
    block = re.sub(r"\s+", " ", block)
    assert "identical in permissions and checks" in block
    assert "route_evaluator" in block and "socket_route" in block
    assert "Work IQ predicate is code" in block, "固定した理由が書かれていない"


def test_the_grader_and_the_guards_are_frozen():
    """分類機を進化可能にしてよい論拠が丸ごとこれに乗っている。"""
    from relay.selfimprove import frozen as F
    assert "relay/selfimprove/route_evaluator.py" in F.FROZEN_MANIFEST
    assert "relay/socket_route.py" in F.FROZEN_MANIFEST


def test_a_genome_may_not_name_routing_even_now():
    with pytest.raises(M.ManifestError) as exc:
        M.apply_genome(M.base_manifest(), {"components": {"routing": "routing/v2"}})
    assert "forbidden" in str(exc.value)


def test_the_fleet_asks_the_policy_before_it_asks_for_a_socket():
    """宣言する前に到達性を追跡する。quality_cards はこれを怠って
    『ターゲットが読まないフィールド』として正当に拒否された。"""
    import inspect

    from bench.companionbench.agents import FLEET_FIELDS
    from relay import relay_fleet as RF
    src = inspect.getsource(RF.RelayWorker)
    assert "transport_policy import" in src, "フリートが方策を読んでいない"
    assert src.index("transport_policy import") < src.index("driver_for(self.name)"), (
        "経路に頼んだ後で方策を聞いている")
    assert "components.transport" in FLEET_FIELDS


def test_a_policy_that_cannot_answer_does_not_cost_a_goal():
    """経路は速度であって能力ではない、という socket_route の原則は
    その手前の分類にも及ぶ。方策が壊れてもゴールは落ちてはいけない。"""
    import inspect
    from relay import relay_fleet as RF
    src = inspect.getsource(RF.RelayWorker)
    i = src.index("transport_policy import")
    assert "except Exception" in src[i:i + 400]

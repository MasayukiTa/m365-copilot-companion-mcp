"""transport が routing でないこと、そして「進化させてよい部分」の線が守られること。

この線は概念ではなくコードの性質に乗っている。socket 側にだけ近道が1つ入れば
transport 選択は routing 選択に変わる。だからテストは名前ではなく不変条件を見る。
"""
import pytest

from relay.selfimprove import manifest as M
from relay.transport_policy import (SOCKET, TAB, TRANSPORT_VERSIONS, ATTACHMENT,
                                    _policy_v1, _policy_v2, classify_fallback,
                                    choose, evolvable_fields, needs_tab)


# ---- 版が実際に分岐すること -----------------------------------------------------------------------

def test_the_two_versions_answer_differently_for_the_same_goal():
    """両腕が同じ答えを返すなら A/B は同じプログラムの二腕。
    このリポジトリが4つのコンポーネントで見つけた欠陥。

    分岐点が変わった。以前は Work IQ 判定式が v1/v2 を分けていたが、その前提は
    実測で否定され（Work IQ は socket に乗る）、判定式ごと外れた。残る差は
    eligible-kinds ノブだけなので、**ノブが設定されていない既定では両版は一致する**。
    キャンペーンの2腕はこのノブで作る必要がある。"""
    knobs = {"transport_eligible_kinds": ["code"]}
    goal, kind = "何かする", "other"
    assert _policy_v1(goal, kind=kind, knobs=knobs) == SOCKET
    assert _policy_v2(goal, kind=kind, knobs=knobs) == TAB


def test_without_that_knob_the_two_versions_now_agree():
    """外した判定式がしていた仕事を、他の何かがしているつもりにならないための明示。
    既定ノブでの一致は欠陥ではなく、差分が1箇所しか無いという事実。"""
    for goal in ("Outlook の受信トレイを整理して", "42 を答えて"):
        assert _policy_v1(goal) == _policy_v2(goal) == SOCKET


def test_v1_preserves_todays_behaviour_rather_than_yesterdays():
    """最初の版は v1 を『全部タブ』にし、socket 経路のテストが即座に落ちた。
    経路はもう存在し独自のスイッチで gate されているので、『常にタブ』は
    現状維持ではなく**機能の無効化**。コンポーネントの v1 は今日の挙動でなければ、
    昇格そのものが、実験を頼んでいない人の挙動を変える。"""
    for goal in ("何でも", "Outlook を整理", ""):
        assert _policy_v1(goal) == SOCKET


# ---- 固定された述語 -------------------------------------------------------------------------------

def test_an_attachment_never_goes_over_a_socket():
    """socket にファイルの置き場は無い。方針の good/bad ではなく物理。"""
    assert needs_tab(r"C:\data\sales.csv") is True
    assert choose("このCSVを分析して", upload_path=r"C:\data\sales.csv") == TAB


def test_the_structural_rule_is_applied_before_the_version_is_even_chosen():
    """版表は『2つの意見を比べる』ための仕組み。ファイルをどこに置けるかに
    第2の意見は存在しないので、どの版にも判断させない。"""
    for impl in TRANSPORT_VERSIONS.values():
        assert impl("このCSVを分析して") == SOCKET      # 版そのものは知らない
    assert choose("このCSVを分析して", upload_path="x.csv") == TAB


def test_workiq_goals_are_no_longer_diverted():
    """実測 2026-08-21: Work IQ は socket に乗る。socket 実ターン20回・8クラスで
    fallback 0件。しかも尤もらしさで通らない検査をした -- 同日に作成し独立に検証した
    予定について両経路に尋ね、**両方**がそれを名指しした。見たことのない予定を
    発明することはできない。"""
    for goal in ("Outlook の受信トレイを整理して", "SharePoint の資料をまとめて",
                 "Teams の会議メモを要約", "予定表に登録して"):
        assert needs_tab() is False
        assert choose(goal) == SOCKET, goal


def test_the_structural_rule_is_not_in_the_evolvable_set():
    """誤りが静かな次元は進化させない -- この原則は残す。
    変わったのは対象で、いま固定されているのは『添付』。しかもこれは
    呼び出し側が持つ値なので、静かに間違えようがない。"""
    fields = evolvable_fields()
    for banned in ("attachment", "needs_tab", "upload"):
        assert not any(banned in f for f in fields), fields


def test_the_rule_reads_a_parameter_and_never_the_goal_text():
    """テキストを読み始めた瞬間、実測が否定した前提に戻る。"""
    assert needs_tab("") is False
    assert choose("添付ファイルを見て", upload_path="") == SOCKET


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


def test_exploration_never_overrides_the_fixed_rule():
    """探索が安全なのは誤分類の代償がフォールバックだから。
    添付はフォールバックにならない -- socket にファイルの置き場が無いのは
    確率の話ではないので、探索を伸ばす先ではない。"""
    assert choose("このCSVを分析して", kind="other",
                  knobs={"transport_eligible_kinds": ["code"]},
                  explore=True, upload_path=r"C:\d.csv") == TAB


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
    # 呼び出しの綴りではなく順序を見る。以前は "driver_for(self.name)" を丸ごとアンカーに
    # していたため、引数が1つ増えただけでこの不変条件の検査が消えた。
    assert src.index("transport_policy import") < src.index("driver_for("), (
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


def test_the_fixed_rule_cannot_be_given_goal_text_at_all():
    """『テキストは読まない』は約束では守れない。引数として存在しないことで守る。

    実測が否定したのは「M365 に触れる目標は socket に乗らない」という文字列判定であって、
    同じ判定を別の語で書き直せば同じ誤りが戻る。だから signature を見張る --
    このファイルが乱数について既にやっているのと同じ理由、同じやり方。"""
    import inspect

    from relay import transport_policy as TP
    params = list(inspect.signature(TP.needs_tab).parameters)
    assert params == ["upload_path"], params
    src = inspect.getsource(TP.needs_tab)
    body = src.split('"""')[-1]          # docstring を除いた本体だけを見る
    for text_ish in ("goal", "lower()", "in text", "MARKERS"):
        assert text_ish not in body, "本体がテキストを読み始めている: %s" % text_ish


def test_the_first_real_fallback_reason_is_classified(monkeypatch):
    """実測 2026-08-21、初の本物の fallback:
    'ChatHubError: turn deadline exceeded before a completion frame'

    これを task-caused に数えると、分類機は『請求書レビューにはタブが要る』を
    時計から学ぶ。時計は目標について何も語らない。"""
    from relay.transport_policy import classify_fallback
    assert classify_fallback(
        "ChatHubError: turn deadline exceeded before a completion frame") == "route"
    assert classify_fallback("ChatHubError: the socket went silent") == "route"


def test_a_card_is_still_about_the_goal():
    """経路語を足したせいで、目標起因の理由まで route に流れてはいけない。"""
    from relay.transport_policy import classify_fallback
    assert classify_fallback("the turn completed but carried no text "
                             "(a card the tab can show?)") == "task"
    assert classify_fallback("添付が必要です") == "task"


# ---- the two reasons that were never read ---------------------------------------------------
#
# Both classified as `unknown`, which was correct until somebody read them, and between them
# they were the two commonest fallback reasons on record -- 19 and 11 occurrences. `unknown`
# is never retried, so every one of them opened a tab instead of reconnecting. On 2026-08-27 a
# corporate proxy refused the websocket upgrade for about a minute, three of those in a row
# closed the route permanently, and the remaining forty minutes of an hour-long run ran on
# tabs at roughly 900 MB of browser instead of 390.

def test_a_proxy_refusing_the_upgrade_is_transport():
    """It could not open the socket at all. That is transport by construction."""
    assert classify_fallback(
        "ChatHubError: could not open the socket: InvalidProxyStatus: "
        "proxy rejected connection: HTTP 502") == "route"


def test_any_failure_to_open_the_socket_is_transport():
    """The route raises this whenever the connection cannot be established, whatever refused
    it -- so the class is covered rather than the one proxy that happened to appear first."""
    assert classify_fallback("ChatHubError: could not open the socket: timed out") == "route"


def test_the_backend_declining_a_request_is_transport():
    """A tab is a different client talking to the SAME backend, so it fixes nothing. Measured:
    of four such fallbacks on record, three were followed by a worker completing over the
    socket -- the tab was not what recovered them."""
    assert classify_fallback(
        "ChatHubError: the backend declined the request: InternalError") == "route"


def test_a_card_still_needs_a_tab():
    """The extension must not swallow the reasons a tab genuinely fixes."""
    assert classify_fallback("consent card shown") == "task"
    assert classify_fallback("the reply carried no text") == "task"
    assert classify_fallback("添付が必要です") == "task"


def test_an_unread_reason_is_still_unknown():
    """Widening the route list must not turn the classifier into a catch-all: an unread reason
    has to keep accumulating visibly so it can be read and added deliberately."""
    assert classify_fallback("something nobody has seen before") == "unknown"

"""常時層の文字数予算。超える追記は、置換するか遅延層へ。

## なぜ予算が要るか

指示は**事故1件ごとに1文**増える。increments はどれも正当で、どれも実測に基づいている ——
だから誰も止められないまま、MCP server instructions は RULE 1〜7・4,308字に育った。

止め方は「足すな」ではなく「**予算を超えたら、足す代わりに置換するか、関連する瞬間へ移す**」。
関連する瞬間とは、カタログ応答・ツールの signature 応答・エラーメッセージ・skill 本体・
局面別テキストを指す。

前例がある。RULE 5 は「数え方を指示する」のをやめ、`glob` に**1行目で件数を返させた**。
プロンプトで「自分で数えるな」と言い続けるより、ツールが答えを持って返るほうが安定した。

## 最急勾配は毎ターン再送

`OUTPUT_DISCIPLINE` への +100字は、会話あたり +100 × ターン数で効く。
実測(2026-08-28): 20ターン走行の再送累計 2,627字 = 開始 PROTOCOL の 2.1倍。

## この試験が守るもの

数字そのものではなく、**超えたときに気づくこと**。予算を上げる判断自体は人がしてよいが、
黙って超えることはできない。
"""
import pytest

#: ワーカーへの唯一の保証チャネル。ここが埋没すると、最初のツール呼び出しが起きなくなる。
PROTOCOL_BUDGET = 1500

#: 毎ターン再送されるので、ここへの追記だけがターン数倍で効く。
DISCIPLINE_BUDGET = 400

#: Claude 系クライアント専用層(ワーカーには届かない)。
INSTRUCTIONS_BUDGET = 5000


def test_the_worker_prompt_is_inside_its_budget():
    from relay.copilot_autopilot_relay import PROTOCOL

    assert len(PROTOCOL) <= PROTOCOL_BUDGET, (
        "PROTOCOL が %d 字で予算 %d を超えた。足すのではなく、既存の1文を置換するか、"
        "『関連する瞬間』(カタログ応答・ツール signature・エラー文・skill)へ移すこと"
        % (len(PROTOCOL), PROTOCOL_BUDGET))


def test_the_per_turn_discipline_is_inside_its_budget():
    from relay.copilot_autopilot_relay import OUTPUT_DISCIPLINE

    assert len(OUTPUT_DISCIPLINE) <= DISCIPLINE_BUDGET, (
        "OUTPUT_DISCIPLINE が %d 字で予算 %d を超えた。ここは毎ターン再送されるので、"
        "+N字は会話あたり +N×ターン数で効く" % (len(OUTPUT_DISCIPLINE), DISCIPLINE_BUDGET))


def test_the_worker_prompt_names_the_approved_procedures():
    """スキルの存在をワーカーに伝えているか。

    MCP server instructions にはそのルールがあるが、**フリートのワーカーには届かない**
    (2026-08-28、対照2本つきで実測)。この1文が無いと、`skills/` にある手順は
    フリート経路から事実上使われない。
    """
    from relay.copilot_autopilot_relay import PROTOCOL

    assert "skill_match" in PROTOCOL, (
        "ワーカーにスキルの存在を伝える文が無い —— server instructions は届かないので、"
        "これが唯一の経路")


def test_the_wire_protocol_markers_are_still_there():
    """マーカーは指示ではなく relay のパーサと対になった約束。削れない。"""
    from relay.copilot_autopilot_relay import PROTOCOL

    for marker in ("CONTINUE", "DONE", "STUCK", "RESEARCH:", "ANALYZE:"):
        assert marker in PROTOCOL, "%s が消えている(パーサと対の約束)" % marker


def test_the_gateway_sentence_comes_before_the_discipline():
    """順序も実測で決まっている。規律文が先だと、ツールを探す前に『不可』で切り上げる。"""
    from relay.copilot_autopilot_relay import OUTPUT_DISCIPLINE, PROTOCOL

    assert PROTOCOL.index("call_tool") < PROTOCOL.index(OUTPUT_DISCIPLINE[:20]), (
        "規律文がゲートウェイの説明より前に来ている")

"""定型の無回答には原因が2つ以上あり、検出器は1つしか知らなかった。

この検出器は 2026-07-03 に、headless の窓詰まりで `?titleId=` のカスタムエージェントが
解決できず、MCPコネクタの無い既定 Copilot に落ちる事象のために書かれた。そしてそれ以降、
定型文を見たら常にその原因を主張していた。

2026-08-25 の実測: goal 4本を2回。同じ goal が2回とも定型文を引き、同じ走行の隣の
ワーカーは正しく答え、うち1つは CloseIntentTool を呼んでいた -- つまりカスタム
エージェントは解決しており、コネクタは在った。返ってきたのは「その指示に対する拒否」で、
それが INFRA_STUCK として、しかも「Edge をヘッドフルで再起動せよ」という案内つきで
報告された。8 goal 中3本、運用者をブラウザ調査へ送り出していた。
"""
import time

from relay import relay_fleet as F


def test_a_plausible_reply_is_not_proof(monkeypatch):
    """既定 Copilot も『東京』とは答えられる。文面が正しく見えることは、
    どちらのエージェントが答えたかについて何も証明しない。"""
    import inspect
    src = inspect.getsource(F.connector_proven)
    assert "last_inbound_ts" in src, "証拠が文字列照合に戻っている"


def test_an_arriving_tool_call_is_proof(monkeypatch):
    """カスタムエージェントにしかできないのは MCP コネクタに届くこと。
    届けば、この機械自身のサーバが刻む。"""
    monkeypatch.setattr(F, "_PROCESS_START", 1000.0)

    class _Probe:
        @staticmethod
        def last_inbound_ts():
            return 2000.0

    monkeypatch.setitem(__import__("sys").modules, "tools.tool_probe", _Probe)
    assert F.connector_proven() is True


def test_no_tool_call_since_the_run_began_is_not_proof(monkeypatch):
    monkeypatch.setattr(F, "_PROCESS_START", 3000.0)

    class _Probe:
        @staticmethod
        def last_inbound_ts():
            return 2000.0          # 走行より前の呼び出し

    monkeypatch.setitem(__import__("sys").modules, "tools.tool_probe", _Probe)
    assert F.connector_proven() is False


def test_unknowable_is_not_proven(monkeypatch):
    """判定できないときは従来の診断を残す。保守的な向きはこちら --
    ブラウザを見に行かせて時間を無駄にするほうが、壊れたコネクタを『問題なし』と
    言って走行を失うよりまし。"""
    class _Broken:
        @staticmethod
        def last_inbound_ts():
            raise RuntimeError("probe unavailable")

    monkeypatch.setitem(__import__("sys").modules, "tools.tool_probe", _Broken)
    assert F.connector_proven() is False


def test_the_two_causes_get_different_outcomes():
    """コネクタが証明済みなら REFUSED、そうでなければ INFRA_STUCK。
    同じ結末に丸めると、言い換えれば済む話にブラウザ再起動を勧め続けることになる。"""
    import inspect
    src = inspect.getsource(F.RelayWorker._decide)
    assert "connector_proven()" in src, "判定が原因を分けていない"
    assert '"REFUSED"' in src, "拒否という結末が無い"
    i = src.index("connector_proven()")
    near = src[i:i + 900]
    assert "REFUSED" in near and "INFRA_STUCK" in near, "分岐の両側が揃っていない"

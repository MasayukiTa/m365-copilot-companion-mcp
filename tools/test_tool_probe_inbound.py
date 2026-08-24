"""プローブの tool 呼び出しがこのサーバに着弾したかを、経路に依存せず記録する。

このプローブが生まれた障害は「コネクタの同意が失効し、呼び出しが Copilot の
web UI の中で死ぬ」もので、こちらのプロセスには何も届かないので、どの計数器も
動かず、どのドットも緑のままだった。逆に成功はこちら側で完全に見える -- プローブは
このサーバしか名前を知らないディレクトリを1つ列挙させる。だから着弾が信号で、
プローブ窓の中で着弾が無いことが警報になる。ページでも socket でも同じに読める。
"""
import ast
import inspect
import json

import pytest

from tools import tool_probe as TP


@pytest.fixture
def inbound(tmp_path, monkeypatch):
    p = tmp_path / "probe_inbound.json"
    monkeypatch.setattr(TP, "_INBOUND_PATH", p)
    return p


def test_an_unrelated_tool_call_writes_nothing(inbound):
    """ゲートウェイの全呼び出しが通る場所なので、関係ない呼び出しで書いてはいけない。"""
    assert TP.note_inbound("read_file", {"path": "C:/tmp/whatever.txt"}) is False
    assert not inbound.exists()


def test_the_probes_own_call_is_stamped(inbound):
    assert TP.note_inbound("list_directory", {"path": str(TP._CHALLENGE_DIR)}) is True
    rec = json.loads(inbound.read_text(encoding="utf-8"))
    assert rec["tool"] == "list_directory" and rec["ts"] > 0


def test_never_seen_reads_as_zero_not_none(inbound):
    """None を返すと、窓の開始時刻との比較で『いま着弾した』に化ける経路ができる。"""
    assert TP.last_inbound_ts() == 0.0


def test_it_cannot_fail_a_tool_call(inbound, monkeypatch):
    """ホットパス上にある。記録の失敗で tool 呼び出しを落としてはいけない。"""
    monkeypatch.setattr(TP, "_INBOUND_PATH", tmp := TP.Path("Z:/nonexistent/x.json"))
    assert TP.note_inbound("list_directory", {"path": str(TP._CHALLENGE_DIR)}) is False
    assert TP.last_inbound_ts() == 0.0
    assert tmp  # 参照して未使用警告を避ける


def test_the_gateway_stamps_before_it_dispatches():
    """呼んでいなければ何も記録されない。ディスパッチの前であること。"""
    import io
    src = io.open("main.py", encoding="utf-8").read()
    i = src.index("_probe.note_inbound(name, _args)")
    j = src.index("_out = fn(**_args)")
    assert i < j, "ディスパッチの後に置かれている"


def test_the_probe_compares_the_window_and_records_the_bit():
    """窓を開く前の刻印と比べなければ、前回の残りを今回の着弾と読む。"""
    import io
    src = io.open("bridge/copilot_bridge.py", encoding="utf-8").read()
    assert "_inbound_before = tool_probe.last_inbound_ts()" in src
    assert "tool_probe.last_inbound_ts() > _inbound_before" in src
    assert "inbound=_inbound" in src


def test_the_summary_carries_it_separately_from_alive():
    """text が返ることと、呼び出しが届いたことは別。片方だけでは
    コネクタ経路の障害と応答の障害を切り分けられない。"""
    fn = ast.parse(inspect.getsource(TP.record_probe).lstrip()).body[0]
    names = [a.arg for a in fn.args.args]
    assert "inbound" in names and "alive" in names
    src = inspect.getsource(TP.get_summary)
    assert '"tool_inbound"' in src and '"tool_alive"' in src

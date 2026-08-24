"""会話リサイクルの効果を、測れる形で残す。

上限(BRIDGE_CONVERSATION_MAX_TURNS)を下げればメモリは戻るが、リサイクルは
利用者の文脈を捨てるので、下げ幅は「1回で何MB戻るか」を知ってから決めたい。
これまでその証拠はログ1行で、流れて消えていた。
"""
import ast
import inspect
import json

import bridge.copilot_bridge as B


def test_the_browser_measured_is_this_bridges_own():
    """端末上の Edge を全部足すのは、この測定の粗い版ではなく別の測定。
    2026-08-24 実測: msedge.exe 45プロセス 6181MB のうち、この bridge の
    ブラウザは 1002MB、フリートのは 1559MB で、59% はどちらでもなかった。"""
    src = inspect.getsource(B._edge_working_set_mb)
    assert "_bridge_edge_root_pid()" in src
    assert "children(recursive=True)" in src
    root = ast.parse(inspect.getsource(B._bridge_edge_root_pid).lstrip()).body[0]
    assert not [n for n in ast.walk(root) if isinstance(n, ast.Raise)]


def test_an_unresolvable_port_reads_as_none_not_as_a_guess():
    import os
    old = os.environ.get("MCP_BRIDGE_CDP_PORT")
    os.environ["MCP_BRIDGE_CDP_PORT"] = "1"
    try:
        assert B._bridge_edge_root_pid() is None
    finally:
        if old is None:
            os.environ.pop("MCP_BRIDGE_CDP_PORT", None)
        else:
            os.environ["MCP_BRIDGE_CDP_PORT"] = old


def test_each_recycle_appends_one_row(tmp_path, monkeypatch):
    p = tmp_path / "recycle_samples.jsonl"
    monkeypatch.setattr(B, "RECYCLE_SAMPLES_PATH", str(p))
    monkeypatch.setattr(B, "_CONVERSATION_TURNS", 42)
    B._append_recycle_sample(1300.0, 900.0)
    B._append_recycle_sample(1100.0, None)
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["freed_mb"] == 400.0 and rows[0]["turns"] == 42
    assert rows[1]["freed_mb"] is None, "測れなかった回を 0 と書くと平均が薄まる"


def test_writing_a_sample_cannot_break_the_probe(monkeypatch):
    """プローブの経路上にある。記録の失敗で生存確認を落としてはいけない。"""
    monkeypatch.setattr(B, "RECYCLE_SAMPLES_PATH", "Z:/nonexistent/dir/x.jsonl")
    B._append_recycle_sample(1.0, 2.0)     # 例外が出ないこと


def test_the_cap_is_not_lowered_on_a_guess():
    """Fable の助言は 120 -> 30 だったが、1回のリサイクルが何MB戻すかは
    まだ1件も記録されていない。記録が貯まるまで既定値は動かさない。"""
    assert B.BRIDGE_CONVERSATION_MAX_TURNS == 120

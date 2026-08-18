"""キルスイッチ。止める側と解除する側は別の問いであり、別の答えを持つ。

見つかった経緯: `relay/test_relay_loop.py` の killswitch シナリオが、スイッチを立てたのに
5ターン走り切って STUCK で終わっていた。原因は `stop_request` が `require_unlocked()` を
通ること -- これは「この**リモート呼び出し元**は変更操作をしてよいか」を答える述語で、
HTTP リクエスト文脈が無ければ常に拒否する。プロセス内の安全系呼び出しが、自分ではない
呼び出し元についての認可検査に落とされていた。

実害は `tools/contract_gate.py` に出ていた。破壊的な op_class を検出して停止を試み、
戻り値を `except Exception: pass` の中で捨て、利用者には「このフリートランは停止します」と
表示していた。止まっていたのはメッセージだけで、他のワーカーは走り続けていた。
"""
import json
import os

import pytest

from tools import gate_ops as G


@pytest.fixture(autouse=True)
def _clean_switch():
    """どのテストもスイッチを残さない。残すと以降が全部 ABORTED になり、
    レポートは無関係な失敗の山に見える -- 実際にそうなった。"""
    G.STOP_FILE.unlink(missing_ok=True)
    yield
    G.STOP_FILE.unlink(missing_ok=True)


# ---- 誰が訊いているか ---------------------------------------------------------------------------

def test_an_in_process_safety_caller_can_stop_without_an_http_context():
    """止まる理由が最も切迫している場面でちょうど失敗する認可検査を、通させない。"""
    assert G.stop_request_internal("destructive op detected", source="test") == G.STOP_ENGAGED
    assert G.stop_check().startswith("STOP")


def test_the_model_facing_tool_still_requires_authorisation():
    """緩めていないこと。停止は latch するので、任意のリモート呼び出し元が
    フリートを無期限に駐車できてはいけない。"""
    got = G.stop_request("from a tool call")
    assert "locked" in got
    assert G.stop_check() == "RUN", "拒否したのにスイッチが立っている"


def test_releasing_the_switch_stays_the_privileged_direction():
    """非対称であることが要点。停止は安全方向、解除は特権方向。"""
    G.stop_request_internal("x", source="test")
    assert "locked" in G.stop_clear()
    assert G.stop_check().startswith("STOP"), "認可されない解除が通っている"


def test_reading_the_switch_never_requires_anything():
    """止まるべきか読む操作が失敗しうるなら、それは止まらない経路。"""
    assert G.stop_check() == "RUN"
    G.stop_request_internal("y", source="test")
    assert "y" in G.stop_check()


# ---- 報告した結果は起きた結果か -------------------------------------------------------------

def test_the_engaged_answer_is_verified_rather_than_assumed():
    """旧実装は書き込みの次の行で「STOP requested」を返していた。
    何もしていない関数に「フリートが停止します」と言わせられる。"""
    assert G.stop_request_internal("z", source="test") == G.STOP_ENGAGED
    assert G.stop_check().startswith("STOP")


def test_a_failure_to_engage_is_distinguishable_from_success(monkeypatch):
    """呼び出し元が prose を解析せずに成否を判定できること。"""
    monkeypatch.setattr(G, "stop_check", lambda: "RUN")
    got = G.stop_request_internal("w", source="test")
    assert got != G.STOP_ENGAGED and "NOT engaged" in got


def test_a_write_failure_is_reported_not_swallowed(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(G.Path, "write_text", boom)
    got = G.stop_request_internal("v", source="test")
    assert got != G.STOP_ENGAGED and "OSError" in got


# ---- 読めないものは全部 STOP 側へ倒す ---------------------------------------------------------

def test_an_unparseable_switch_file_still_stops():
    G.STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    G.STOP_FILE.write_text("{not json", encoding="utf-8")
    assert G.stop_check().startswith("STOP")


def test_a_path_that_exists_but_is_not_a_regular_file_stops():
    """`is_file()` だけを見ていたので、ディレクトリが置かれていると RUN を返した --
    キルスイッチが誤って返してよい唯一の答えでない答え。"""
    G.STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    G.STOP_FILE.mkdir()
    try:
        assert G.stop_check().startswith("STOP")
    finally:
        os.rmdir(str(G.STOP_FILE))


def test_the_switch_records_who_asked():
    """3時に読む人には、誰が止めたかが必要。"""
    G.stop_request_internal("contract stop_when", source="contract_gate")
    data = json.loads(G.STOP_FILE.read_text(encoding="utf-8"))
    assert data["source"] == "contract_gate" and data["reason"] == "contract stop_when"


def test_no_temp_file_is_left_behind():
    """書き込みは同一ディレクトリの一時ファイル経由。残すとゲートディレクトリが
    読めなくなっていく。"""
    G.stop_request_internal("t", source="test")
    leftovers = [p.name for p in G.STOP_FILE.parent.glob(G.STOP_FILE.name + ".tmp-*")]
    assert leftovers == []


# ---- 契約ゲートが本当のことを言うか -----------------------------------------------------------

def test_the_contract_gate_says_the_fleet_stopped_only_when_it_did(monkeypatch, tmp_path):
    from tools import contract_gate as CG
    monkeypatch.setattr(CG, "load_contract",
                        lambda: {"active": True, "stop_when": ["delete_many"]})

    msg = CG.check_op("delete_many", "rm -rf /data")
    assert "NOT executed" in msg
    assert "kill-switch is engaged" in msg
    assert G.stop_check().startswith("STOP")


def test_the_contract_gate_warns_when_the_switch_did_not_engage(monkeypatch):
    """止められなかったことは、隠さずに言う。旧実装はここで
    「このフリートランは停止します」と書いていた -- 止まっていたのはメッセージだけ。"""
    from tools import contract_gate as CG
    monkeypatch.setattr(CG, "load_contract",
                        lambda: {"active": True, "stop_when": ["delete_many"]})
    monkeypatch.setattr(G, "stop_request_internal",
                        lambda *_a, **_k: "[stop_request error: nope]")

    msg = CG.check_op("delete_many", "rm -rf /data")
    assert "NOT executed" in msg, "操作の拒否は依然として起きねばならない"
    assert "NOT engaged" in msg and "other workers keep running" in msg

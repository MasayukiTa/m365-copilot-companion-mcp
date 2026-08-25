"""会話の削除。3箇所すべてから消えること、そして復活しないこと。

報告された症状: メインから削除してもフリートに残り、クリックすると復活する。

原因は単純だった。会話は3箇所に在る -- Copilot 自身の rail、ローカルの保存層、
そして .fleet/conversations.json -- のに、/delete は1箇所しか消していなかった。
保存層には個別削除の関数すら無く(一括の prune だけ)、フリートの行はそのまま残り、
そこを開くと再登録されて会話がチャットに戻ってきた。
"""
import io
import json
import os
import pathlib

import pytest

from bridge import session_store as ss

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_base_dir", lambda: str(tmp_path))
    ss._IMPORTED.discard(os.path.abspath(str(tmp_path)))
    yield tmp_path


URL = "https://m365.cloud.microsoft/chat/agent/T_x/conversation/9f1e2c3d-1111-2222-3333-444455556666"


def test_deleting_removes_the_row_the_turns_and_both_files(box):
    sess = ss.new_session(title="doomed")
    sid = sess["sid"]
    ss.append_turn(sid, "user", "hello")
    ss.touch(sid, conv_url=URL)
    assert os.path.isfile(ss._transcript_path(sid))

    assert ss.delete_session(sid) is True
    assert ss.load(sid) is None
    assert ss.all_turns(sid) == []
    assert not os.path.isfile(ss._transcript_path(sid)), "台帳を消して本文を残している"
    assert not os.path.isfile(ss._sess_path(sid))


def test_deleting_something_already_gone_is_not_an_error(box):
    assert ss.delete_session("s0000000000abcd") is False


def test_a_conversation_is_found_by_its_guid_not_by_string_equality(box):
    """同じ会話が複数の URL 形で届く。末尾のクエリ1つで取り逃がせば、
    まさに消えないゾンビが残る。"""
    sess = ss.new_session(title="x")
    ss.touch(sess["sid"], conv_url=URL)
    assert ss.find_by_conv_url(URL) == sess["sid"]
    assert ss.find_by_conv_url(URL + "?tab=1") == sess["sid"]
    assert ss.find_by_conv_url(
        "https://other.host/conversation/9F1E2C3D-1111-2222-3333-444455556666") == sess["sid"]
    assert ss.find_by_conv_url("https://x/conversation/deadbeef-0000-0000-0000-000000000000") is None
    assert ss.find_by_conv_url("") is None


def test_the_fleet_row_is_dropped_too(tmp_path, monkeypatch):
    """フリートの行こそが復活の経路。ここを残せば cockpit が開き、再登録される。"""
    import bridge.copilot_bridge as CB

    convs = tmp_path / "conversations.json"
    rows = [
        {"name": "s0101010101aaaa", "url": URL, "title": "doomed", "source": "chat"},
        {"name": "s0202020202bbbb", "url": "https://x/conversation/keep-me", "title": "keep"},
    ]
    convs.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(CB, "FLEET_CONVS_PATH", convs)

    dropped = CB.unregister_bridge_session_from_fleet_convs(sid="s0101010101aaaa", conv_url=URL)
    assert dropped == 1
    left = json.loads(convs.read_text(encoding="utf-8"))
    assert [r["name"] for r in left] == ["s0202020202bbbb"], "残すべき行まで消している"


def test_the_fleet_row_matches_on_guid_when_the_sid_is_unknown(tmp_path, monkeypatch):
    """保存層に無い会話(取り込み前・別端末由来)でも、GUID が一致すれば落とせること。"""
    import bridge.copilot_bridge as CB

    convs = tmp_path / "conversations.json"
    convs.write_text(json.dumps([{"name": "unknown", "url": URL + "?x=1", "title": "z"}]),
                     encoding="utf-8")
    monkeypatch.setattr(CB, "FLEET_CONVS_PATH", convs)
    assert CB.unregister_bridge_session_from_fleet_convs(sid="", conv_url=URL) == 1


def test_a_corrupt_registry_does_not_break_the_delete(tmp_path, monkeypatch):
    """レジストリの整理に失敗しても、利用者が求めた削除を落としてはいけない。"""
    import bridge.copilot_bridge as CB

    convs = tmp_path / "conversations.json"
    convs.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(CB, "FLEET_CONVS_PATH", convs)
    assert CB.unregister_bridge_session_from_fleet_convs(sid="s1", conv_url=URL) == 0


def test_the_endpoint_clears_all_three_places():
    """遠隔の削除が失敗しても、ローカルは消す。利用者は『この会話を消せ』と言っている。
    残して復活させるのが、いま直しているバグそのもの。"""
    src = (ROOT / "bridge" / "copilot_bridge.py").read_text(encoding="utf-8", errors="replace")
    i = src.index("def _do_delete(")
    body = src[i:i + 2600]
    assert "_try_delete_conversation" in body, "Copilot 側を消していない"
    assert "S.delete_session" in body, "保存層から消していない"
    assert "unregister_bridge_session_from_fleet_convs" in body, "フリートの行を残している"
    # 遠隔削除の成否で分岐していないこと
    assert "if ok" not in body.split("S.find_by_conv_url")[0].split("_try_delete_conversation")[1]


def test_the_default_delete_mode_also_forgets_locally():
    """既定は モード1「このアプリからのみ削除」で、/delete は モード3 でしか呼ばれない。
    つまり普段の削除は、直したはずの経路を通らない。

    モード1はアプリ自身のファイルだけを消し、保存層とフリートの行を残していた --
    「最も安全」と説明されているモードが、ゾンビを残す唯一のモードだった。"""
    chat = (ROOT / "ui" / "CopilotChat.cs").read_text(encoding="utf-8-sig", errors="replace")
    i = chat.index("void CommitPendingDelete()")
    body = chat[i:i + 1800]
    assert "/forget?url=" in body, "確定時にローカルの記録を消していない"
    assert "sid=" in body, "sid を渡していない(URL 不明の会話が消せない)"
    # Undo が効かなくなるので DeleteLocal ではなく確定時であること
    j = chat.index("void DeleteLocal(")
    assert "/forget" not in chat[j:j + 1200], "確定前に忘れており Undo が壊れる"


def test_forget_touches_nothing_remote_and_needs_no_page():
    """会話を忘れる操作が、飛行中のターンに待たされたり拒否されたりしてはいけない。"""
    src = (ROOT / "bridge" / "copilot_bridge.py").read_text(encoding="utf-8", errors="replace")
    i = src.index('if parsed.path == "/forget":')
    # 次のエンドポイントの手前で切る。固定幅で取ると隣のハンドラを読み込み、
    # そちらの PAGE_LOCK を自分のものと取り違える。
    j = src.index('if parsed.path == "/agent_conversations":', i)
    # コメントは除く。なぜロックを取らないのかを説明する行が、ロックを取っている証拠に
    # 化けてしまう -- 今夜すでに一度やった間違い。
    code = [ln for ln in src[i:j].splitlines() if not ln.strip().startswith("#")]
    block = chr(10).join(code)
    assert "PAGE_LOCK" not in block, "ページロックを取っている"
    assert "run_on_page_thread" not in block, "ページスレッドを使っている"
    assert "_try_delete_conversation" not in block, "Copilot 側に触っている"
    assert "S.delete_session" in block and "unregister_bridge_session_from_fleet_convs" in block

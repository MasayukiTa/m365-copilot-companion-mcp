"""URL を持たない会話でも、sid があるなら続けられること。

## 何が起きていたか

ソケットで捕捉された会話は `sess:<guid>` という形で保存される。これは**辿れる URL ではない**ので、
`register_bridge_session_in_fleet_convs` は `url: ""` で登録する
(`copilot_bridge.py` の `url_field = conv_url if kind == "conv_url" else ""`)。

チャット UI の送信経路は `ConvUrl` が空だと `/switch` を使えず、
ローカルにメッセージがあると `send_unknown_conv` で**送信を中止**していた。
つまり **一覧には出るのに続けられない会話**ができていた。そしてソケットは今や通常の経路なので、
その多くがこれに当たる。

会話は sid を持っている(`name: sid` で登録されている)。`/resume?sid=` は sid を取り、
保存されている両方の形を知っている — sessref ならサイドバー行をクリックし、
実 URL なら遷移する。使える道具があるのに呼ばれていなかった。

## Name が多義であること

`Name` は chat 行では sid、fleet 行ではワーカー名(w0/w1)。だから `Source == "chat"` で
必ず絞る。fleet 行を "w0" で resume すると、存在しないセッションをストアに問い合わせることになる。
"""
import re
from pathlib import Path

RAW = (Path(__file__).with_name("ChatSend.cs")).read_text(encoding="utf-8-sig")


def _executable(cs):
    out, i, n = [], 0, len(cs)
    while i < n:
        if cs.startswith("//", i):
            j = cs.find(chr(10), i)
            i = n if j < 0 else j
        elif cs.startswith("/*", i):
            j = cs.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(cs[i]); i += 1
    return "".join(out)


SOURCE = _executable(RAW)


def _door_decision():
    """「ページを target に合わせる」扉の選択。2026-09-24 に ui/ChatSend.cs の DecideDoor へ移り、
    ui/test_the_chat_window_sends_what_was_typed.py が**実行して**確かめている(door_sid /
    door_unknown / door_new 等のケース)。ここに残るのは形の確認だけ。"""
    body = SOURCE[SOURCE.index("string DecideDoor("):]
    return body[:body.index("return DOOR_UNKNOWN;") + len("return DOOR_UNKNOWN;")]


def _door_opening():
    """SendText の中で、選ばれた扉を実際の GET に変える部分。"""
    body = SOURCE[SOURCE.index("void SendText(IChatSendEffects fx, string text)"):]
    return body[:body.index("fx.MarkSendInFlight();")]


def test_a_conversation_without_a_url_is_resumed_by_sid():
    assert "return DOOR_RESUME_SID;" in _door_decision(), (
        "URL を持たない会話を sid で再開する分岐が無い -- ソケットで捕捉した会話は"
        "一覧に出るのに続けられない")
    assert '"/resume?sid="' in _door_opening(), "sid の扉が /resume?sid= に繋がっていない"


def test_the_resume_branch_is_gated_on_a_chat_row():
    """fleet 行の Name はワーカー名なので、sid として使ってはいけない。"""
    blk = _door_decision()
    i = blk.index("return DOOR_RESUME_SID;")
    line = blk[blk.rindex(chr(10), 0, i):i]
    assert 'Source == "chat"' in line, (
        "Source で絞っていない -- fleet 行の Name(w0 など)を sid として resume してしまう")
    assert "IsNullOrEmpty(target.Name)" in line, "Name が空のときも resume しようとしている"


def test_the_refusal_still_exists_for_a_conversation_with_no_identity():
    """身元が何も無い会話は、やはり断ること。全部通すための変更ではない。"""
    assert "return DOOR_UNKNOWN;" in _door_decision()
    assert "send_unknown_conv" in _door_opening(), (
        "識別できない会話への送信を止める分岐が消えている")


def test_the_branch_order_puts_identity_before_starting_a_new_chat():
    """sid を持つ会話を、ローカルに履歴が無いという理由で /new に流さないこと。

    再起動直後はメッセージが読み込まれていないことがある。そこで /new を撃つと、
    続けたかった会話の代わりに新しい会話が始まる。
    """
    blk = _door_decision()
    assert blk.index("return DOOR_RESUME_SID;") < blk.index("return DOOR_NEW;"), (
        "/new の分岐が先にある -- 履歴未読込の既存会話が新規会話にすり替わる")

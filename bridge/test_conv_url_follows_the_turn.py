"""保存された会話参照が、実際にターンが入った会話を指し続けること。

## 何が起きていたか

`_persist_exchange` は「セッションが既に conv_url を持っていたら**絶対に上書きしない**」だった。
その理由(コメント)は妥当:「間違った resume は、resume しないより悪い」。
ただしそれは**推測で上書きするな**という意味であって、実測値には当てはまらない。

会話はセッションの下で動く。ソケットの driver が走行中に作り直されると別の会話が始まりうるし、
長い会話のリサイクルも会話を変える。そのとき保存された参照は古いまま固定される。

唯一の検知は「ペインの aria-current guid と保存値の突合」だが、
`_current_row_guid()` はページが解放されていると `""` を返し、**ページ解放は既定ON**。
つまり出荷される構成では、この不整合は**原理的に検知できなかった**。

結果として、後から `/resume` すると、そのセッションに記録されたターンを1つも持たない
会話が開く。台帳と実体が別々のものを指す。

## 直し方

`socket_conv_ref()` は「いま送ったターンが入った会話のID」であって推測ではない。
食い違ったら**測った側に合わせる**。タブ経路(ソケットが無い)では従来どおり警告のみ。
"""
import io
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _persist_source():
    with io.open(os.path.join(REPO, "bridge", "copilot_bridge.py"), encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.index("def _persist_exchange("):]
    return body[:body.index("\ndef ", 10)]


def _code_only(body):
    return "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))


def test_a_stale_reference_is_corrected_from_the_socket():
    code = _code_only(_persist_source())
    assert "socket_conv_ref()" in code, "ソケットに実際の会話を尋ねていない"
    assert re.search(r"S\.touch\(sid, conv_url=live", code), (
        "食い違っても保存値を直していない -- 台帳が実体と別の会話を指し続ける")


def test_it_only_corrects_on_a_real_disagreement():
    """一致しているとき、無い/読めないときに書き換えないこと。

    ソケットが答えない(タブ経路)状況で上書きすると、「聞けなかった」を
    「別の会話だった」と読むことになる。
    """
    code = _code_only(_persist_source())
    assert re.search(r"if live_guid and expected and live_guid != expected", code), (
        "書き換え条件が『両方あって食い違う』になっていない")


def test_the_dom_check_survives_for_the_tab_path():
    """ソケットが無い経路では従来どおり警告すること。消してはいない。"""
    code = _code_only(_persist_source())
    assert "_current_row_guid()" in code, "タブ経路の突合が消えている"
    assert "keeping stored conv_url" in _persist_source(), (
        "タブ経路で保存値を維持する旨の記録が消えている")


def test_the_socket_reference_shape_round_trips():
    """`sess:<guid>` から guid が取り出せること。取り出せなければ比較が常に不成立になる。"""
    from bridge.copilot_bridge import make_sessref, sessref_guid

    guid = "d870f6cd-4aa5-4d42-9626-ab690c041a1a"
    assert sessref_guid(make_sessref(guid)) == guid
    assert sessref_guid("") == ""

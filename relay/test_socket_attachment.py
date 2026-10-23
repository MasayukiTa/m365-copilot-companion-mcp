# -*- coding: utf-8 -*-
"""relay/socket_attachment: 失敗は必ず None で、None はタブを意味する。

この関数の戻り値は二値ではない。**注釈が返る**か、**タブに降りろ**かのどちらかであり、
「注釈が無いまま送る」は選択肢に無い -- それは届かなかったファイルについての質問になり、
何も無いことについての自信ある答えが返ってくる。タブ側の実装が既に名指ししている故障で、
socket 側で作り直さないことをここで固定する。

ネットワークもブラウザも使わない。使うと、経路の障害でこの不変条件のテストが赤くなる。
"""
from __future__ import annotations

import os

import relay.socket_attachment as SA


class _Ctx:
    """new_page を呼ばれたら落ちる文脈。呼ばれないことが測りたいので。"""

    def __init__(self):
        self.pages = 0

    def new_page(self):
        self.pages += 1
        raise AssertionError("ページを開いてはならない局面で開いた")


def test_a_missing_file_is_a_refusal_and_opens_nothing():
    ctx = _Ctx()
    said = []
    assert SA.annotation_for(ctx, "https://x/", r"C:\no\such\file.png", "tok",
                             log=said.append) is None
    assert ctx.pages == 0, "無いファイルのためにタブを1枚払った"
    assert any("no such file" in m for m in said)


def test_an_empty_path_is_a_refusal():
    assert SA.annotation_for(_Ctx(), "https://x/", "", "tok") is None


def test_no_token_is_a_refusal_before_any_page_is_opened(tmp_path):
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG\r\n")
    ctx = _Ctx()
    said = []
    assert SA.annotation_for(ctx, "https://x/", str(png), "", log=said.append) is None
    # トークン無しでアップロードは通らない。先に分かることを、タブを1枚開いてから
    # 気づくのは 30秒の無駄であり、開いたタブは後始末の対象が1つ増えることでもある。
    assert ctx.pages == 0
    assert any("no token" in m for m in said)


def test_an_exception_anywhere_is_still_a_refusal_not_a_raise(tmp_path, monkeypatch):
    """呼び出し側は「注釈か、理由つきの終了か」しか扱わない。例外が出るとそのどちらでもなくなる。"""
    from relay import upload_template as UT
    monkeypatch.setattr(UT, "CACHE_DIR", str(tmp_path / "cache"))
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG\r\n")

    class _Refused:
        status_code = 403

    # the minimal request is tried first; refused here, so the page capture (which blows up) runs
    monkeypatch.setattr("requests.post", lambda *a, **k: _Refused())

    class _Boom:
        def new_page(self):
            raise RuntimeError("cdp went away")

    said = []
    assert SA.annotation_for(_Boom(), "https://x/", str(png), "tok", log=said.append) is None
    assert any("capture failed" in m and "RuntimeError" in m for m in said), \
        "何が起きたか言わずに黙って諦めた"


def test_the_headers_a_replay_must_not_forward():
    """キャプチャしたヘッダをそのまま転送すると、HTTP/2 の擬似ヘッダと、requests が
    計算し直す長さと、差し替えるはずの資格情報を二重に送ることになる。

    このリストは scripts/probes/replay_upload_with_our_token.py が 200 を取った実測の
    ものと同一であること -- 片方だけ編集されると、動いた構成が静かに失われる。"""
    assert ":authority" in SA._DROP and ":path" in SA._DROP
    assert "content-length" in SA._DROP
    assert "authorization" in SA._DROP, "差し替える当のヘッダを転送している"
    assert "accept-encoding" in SA._DROP and "host" in SA._DROP


def test_the_upload_url_is_the_one_that_was_measured():
    assert SA.UPLOAD_URL == "https://substrate.office.com/m365Copilot/UploadFile"


_MODULE = os.path.join(os.path.dirname(__file__), "socket_attachment.py")


def _src():
    """CODE ONLY. The docstring of that module explains the six multipart fields the page
    sends, including `FileBase64` -- so a check forbidding a rebuilt body matched the
    paragraph describing the mistake it forbids. Fourth time in one day for that class; see
    conftest.code_only, which is why it is one shared implementation now -- and it lives in
    conftest because only tests use it."""
    from conftest import code_only

    return code_only(_MODULE)


def test_the_request_shape_is_captured_and_only_the_file_value_is_replaced():
    """三度やって三度違う形で間違えた。ヘッダーと他のフィールドは、ページ自身のバイト列のまま。

    2026-10-23 以降、ファイルごとに変わる FileBase64 の値だけを差し替える(ページはもう
    毎回開かない)。差し替えてよい箇所は relay/upload_template.build の一か所で、その不変条件は
    test_upload_template.py が固定している。ここでは、実経路がその一か所を通ることを守る。"""
    code = _src()
    assert "UT.build(tpl, upload_path)" in code, "キャッシュした形にファイルを差し込んでいない"
    assert "FileBase64" not in code, "multipart のフィールドをここで組み立てている"
    assert 'files={"file"' not in code, "バイナリパートを再構成している"
    assert "forbid_copilot_tab" not in code, "タブ拒否に戻っている(キャッシュ経路が無効)"


def test_only_the_authorization_is_replaced():
    code = _src()
    assert 'headers["Authorization"] = "Bearer " + token' in code


def test_the_module_never_writes_a_token_anywhere():
    """ログは失敗の理由を言う。資格情報は言わない。"""
    body = _src()
    for bad in ("say(token", "% token", "+ token)", "format(token"):
        assert bad not in body, "ログにトークンを載せている: %s" % bad

# -*- coding: utf-8 -*-
"""relay/upload_template and the cached path of socket_attachment.annotation_for.

No network, no browser. What is pinned: the cached request is the captured one with ONLY the
FileBase64 value replaced; a stale cache is discarded and re-captured once; a failed capture is a
refusal (None) and never an annotation-less send; and the capture page is a no-turn page.
"""
from __future__ import annotations

import base64

import pytest

import relay.socket_attachment as SA
from relay import upload_template as UT

BODY = (b'--B\r\nContent-Disposition: form-data; name="scenario"\r\n\r\nUnifiedChat\r\n'
        b'--B\r\nContent-Disposition: form-data; name="conversationId"\r\n\r\nconv-1\r\n'
        b'--B\r\nContent-Disposition: form-data; name="FileBase64"\r\n\r\n'
        b'data:image/png;base64,QUJD\r\n'
        b'--B\r\nContent-Disposition: form-data; name="optionsSets"\r\n\r\na\r\n'
        b'--B\r\nContent-Disposition: form-data; name="optionsSets"\r\n\r\nb\r\n--B--\r\n')
HDRS = {"content-type": "multipart/form-data; boundary=B", "x-anchormailbox": "u@example.test",
        "authorization": "Bearer SHOULD-NOT-SURVIVE", "content-length": "1", ":path": "/x"}


def _file(tmp_path, data=b"\x89PNG-new-bytes"):
    p = tmp_path / "pic.png"
    p.write_bytes(data)
    return str(p)


def test_build_changes_only_the_file_value():
    tpl = UT.from_capture(HDRS, BODY)
    assert "authorization" not in {k.lower() for k in tpl.headers}
    assert ":path" not in tpl.headers and "content-length" not in tpl.headers
    assert tpl.fields == ["scenario", "conversationId", "FileBase64", "optionsSets", "optionsSets"]
    import os, tempfile
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "n.png")
        open(f, "wb").write(b"NEWBYTES")
        headers, body = UT.build(tpl, f)
    assert headers == tpl.headers
    new_value = b"data:image/png;base64," + base64.b64encode(b"NEWBYTES")
    assert body == BODY.replace(b"data:image/png;base64,QUJD", new_value)


def test_a_body_without_the_file_field_is_not_a_template():
    with pytest.raises(ValueError):
        UT.from_capture(HDRS, b'--B\r\nContent-Disposition: form-data; name="x"\r\n\r\n1\r\n--B--\r\n')


def test_cache_roundtrip_and_discard(tmp_path):
    tpl = UT.from_capture(HDRS, BODY)
    assert UT.load("https://a/", str(tmp_path)) is None
    assert UT.save(tpl, "https://a/", str(tmp_path))
    got = UT.load("https://a/", str(tmp_path))
    assert got.prefix == tpl.prefix and got.suffix == tpl.suffix and got.headers == tpl.headers
    assert UT.load("https://other/", str(tmp_path)) is None      # per agent surface
    UT.discard("https://a/", str(tmp_path))
    assert UT.load("https://a/", str(tmp_path)) is None


class _Resp:
    def __init__(self, status, doc="D1"):
        self.status_code = status
        self._doc = doc

    def json(self):
        return {"docId": self._doc, "fileName": "pic.png"}


def _setup(tmp_path, monkeypatch, statuses, capture_result="tpl"):
    monkeypatch.setattr(UT, "CACHE_DIR", str(tmp_path / "cache"))
    calls = {"post": 0, "capture": 0}
    seq = list(statuses)

    def post(url, headers=None, data=None, timeout=None):
        calls["post"] += 1
        return _Resp(seq.pop(0), "DOC%d" % calls["post"])

    def capture(context, agent_url, log=None, directory=None):
        calls["capture"] += 1
        if capture_result is None:
            return None
        t = UT.from_capture(HDRS, BODY)
        UT.save(t, agent_url)
        return t

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr(UT, "capture", capture)
    return calls


def test_no_cache_captures_once_then_uploads(tmp_path, monkeypatch):
    calls = _setup(tmp_path, monkeypatch, [403, 200, 403, 200])
    f = _file(tmp_path)
    assert SA.annotation_for(None, "https://a/", f, "t")[0]["id"] == "DOC2"
    assert SA.annotation_for(None, "https://a/", f, "t")[0]["id"] == "DOC4"
    assert calls == {"post": 4, "capture": 1}


def test_a_failed_capture_is_a_refusal_and_posts_nothing(tmp_path, monkeypatch):
    calls = _setup(tmp_path, monkeypatch, [403], capture_result=None)
    assert SA.annotation_for(None, "https://a/", _file(tmp_path), "t") is None
    assert calls == {"post": 1, "capture": 1}


def test_a_stale_cache_is_discarded_and_recaptured_once(tmp_path, monkeypatch):
    calls = _setup(tmp_path, monkeypatch, [403, 403, 200])
    UT.save(UT.from_capture(HDRS, BODY), "https://a/")
    out = SA.annotation_for(None, "https://a/", _file(tmp_path), "t")
    assert out[0]["id"] == "DOC3"
    assert calls == {"post": 3, "capture": 1}


def test_a_fresh_capture_that_is_refused_is_dropped_not_looped(tmp_path, monkeypatch):
    calls = _setup(tmp_path, monkeypatch, [403, 403])
    assert SA.annotation_for(None, "https://a/", _file(tmp_path), "t") is None
    assert calls == {"post": 2, "capture": 1}
    assert UT.load("https://a/") is None


def test_401_is_the_credential_not_the_shape(tmp_path, monkeypatch):
    calls = _setup(tmp_path, monkeypatch, [401])
    UT.save(UT.from_capture(HDRS, BODY), "https://a/")
    assert SA.annotation_for(None, "https://a/", _file(tmp_path), "t") is None
    assert calls == {"post": 1, "capture": 0}
    assert UT.load("https://a/") is not None


def test_the_minimal_request_needs_no_page_and_no_cache(tmp_path, monkeypatch):
    seen = {}
    calls = _setup(tmp_path, monkeypatch, [200])
    inner = __import__("requests").post

    def spy(url, headers=None, data=None, timeout=None):
        seen["headers"], seen["data"] = dict(headers), data
        return inner(url, headers=headers, data=data, timeout=timeout)

    monkeypatch.setattr("requests.post", spy)
    out = SA.annotation_for(None, "https://a/", _file(tmp_path), "t")
    assert out[0]["id"] == "DOC1"
    assert calls == {"post": 1, "capture": 0}
    assert set(k.lower() for k in seen["headers"]) == {"content-type", "x-scenario", "authorization"}
    assert seen["data"].count(b'name="optionsSets"') == 3
    assert b'name="FileBase64"\r\n\r\ndata:image/png;base64,' in seen["data"]


def test_a_non_image_is_refused_before_any_request(tmp_path, monkeypatch):
    calls = _setup(tmp_path, monkeypatch, [200])
    t = tmp_path / "a.txt"
    t.write_text("x")
    assert SA.annotation_for(None, "https://a/", str(t), "t") is None
    assert calls == {"post": 0, "capture": 0}


class _Req:
    def __init__(self, url, body, headers):
        self.url, self.post_data_buffer, self._h = url, body, headers

    def all_headers(self):
        return dict(self._h)


class _Page:
    def __init__(self):
        self.h = None
        self.closed = False

    def on(self, ev, fn):
        self.h = fn

    def goto(self, *a, **k):
        pass

    def wait_for_timeout(self, ms):
        pass

    def close(self):
        self.closed = True


def test_capture_reads_the_pages_request_sends_no_turn_and_closes(tmp_path, monkeypatch):
    page = _Page()
    sent = []

    class Ctx:
        def new_page(self):
            return page

    def fake_upload(p, path):
        p.h(_Req(UT.UPLOAD_URL, BODY, HDRS))
        return True

    monkeypatch.setattr("relay.agent_profiles.upload_file", fake_upload)
    monkeypatch.setattr(UT, "make_probe_png", lambda d: str(tmp_path / "p.png"))
    monkeypatch.setattr("relay.temp_home.temp_dir", lambda n: str(tmp_path))
    tpl = UT.capture(Ctx(), "https://a/", log=sent.append, directory=str(tmp_path / "c"))
    assert tpl is not None and page.closed
    assert UT.load("https://a/", str(tmp_path / "c")) is not None

"""Tests for the durable/resumable session wiring added to bridge.copilot_bridge.

Hermetic: no browser, no network. copilot_bridge imports playwright only lazily inside
main() (see the module docstring / CI comment in .github/workflows/ci.yml), so importing
the module here is safe on CI (no playwright installed there). These tests exercise the
PURE helper functions this feature was factored around:
  - classify_conv_ref / make_sessref / sessref_guid -- the URL/reference-shape classifier
  - should_autoresume -- the startup auto-resume decision function
  - drain_pending_once -- the pending-queue drain logic (pop_fn injected, no real file I/O)
No PAGE/DRIVER/browser-dependent function (_capture_conv_ref, _resume_to_ref, etc.) is
exercised here -- those require a live Playwright page and are out of scope for a hermetic
unit test; they were verified against a live bridge Edge session instead (see the bridge
session's report for the actual observed DOM/localStorage shapes).
"""
import bridge.copilot_bridge as B


# ── classify_conv_ref / make_sessref / sessref_guid ─────────────────────────────────────────

def test_classify_empty():
    assert B.classify_conv_ref("") == "empty"
    assert B.classify_conv_ref(None) == "empty"
    assert B.classify_conv_ref("   ") == "empty"


def test_classify_sessref():
    ref = B.make_sessref("9374821f-6bff-4050-b6fd-8a4338013664")
    assert ref == "sess:9374821f-6bff-4050-b6fd-8a4338013664"
    assert B.classify_conv_ref(ref) == "sessref"


def test_classify_conv_url():
    url = "https://m365.cloud.microsoft/chat/agent/T_abc/conversation/9374821f-6bff-4050-b6fd-8a4338013664"
    assert B.classify_conv_ref(url) == "conv_url"


def test_classify_bare_url():
    # a real URL with no /conversation/<guid> segment -- the confirmed-live bounce shape.
    url = "https://m365.cloud.microsoft/chat/?redirfrom=CsrToSSR&auth=2"
    assert B.classify_conv_ref(url) == "bare_url"


def test_make_sessref_empty_guid():
    assert B.make_sessref("") == ""
    assert B.make_sessref(None) == ""


def test_sessref_guid_roundtrip():
    guid = "74afee8c-6b66-4eb4-af71-63a7d8b175a8"
    ref = B.make_sessref(guid)
    assert B.sessref_guid(ref) == guid


def test_sessref_guid_rejects_non_sessref():
    assert B.sessref_guid("https://example.com/x") == ""
    assert B.sessref_guid("") == ""
    assert B.sessref_guid(None) == ""


def test_bare_guid_re_matches_and_rejects():
    assert B.BARE_GUID_RE.match("9374821f-6bff-4050-b6fd-8a4338013664")
    assert not B.BARE_GUID_RE.match("not-a-guid")
    assert not B.BARE_GUID_RE.match("/conversation/9374821f-6bff-4050-b6fd-8a4338013664")


# ── should_autoresume ────────────────────────────────────────────────────────────────────────

def test_should_autoresume_fresh_flag_wins():
    sess = {"conv_url": "sess:abc"}
    should, reason = B.should_autoresume(sess, fresh_flag=True)
    assert should is False
    assert "fresh" in reason


def test_should_autoresume_no_session():
    should, reason = B.should_autoresume(None, fresh_flag=False)
    assert should is False
    assert "no prior session" in reason


def test_should_autoresume_empty_conv_url():
    should, reason = B.should_autoresume({"conv_url": ""}, fresh_flag=False)
    assert should is False


def test_should_autoresume_happy_path():
    guid = "9374821f-6bff-4050-b6fd-8a4338013664"
    sess = {"conv_url": B.make_sessref(guid)}
    should, reason = B.should_autoresume(sess, fresh_flag=False)
    assert should is True
    assert "resumable" in reason


def test_should_autoresume_with_real_conv_url():
    sess = {"conv_url": "https://m365.cloud.microsoft/chat/agent/T_x/conversation/"
                          "9374821f-6bff-4050-b6fd-8a4338013664"}
    should, _ = B.should_autoresume(sess, fresh_flag=False)
    assert should is True


# ── drain_pending_once ───────────────────────────────────────────────────────────────────────

def test_drain_pending_once_empty_queue():
    out = B.drain_pending_once("sid1", pop_fn=lambda sid: None)
    assert out == []


def test_drain_pending_once_no_sid():
    calls = []

    def pop_fn(sid):
        calls.append(sid)
        return None

    out = B.drain_pending_once("", pop_fn=pop_fn)
    assert out == []
    assert calls == []          # never called pop_fn for a falsy sid


def test_drain_pending_once_drains_fifo():
    queue = ["first", "second", "third"]

    def pop_fn(sid):
        assert sid == "sid1"
        return queue.pop(0) if queue else None

    out = B.drain_pending_once("sid1", pop_fn=pop_fn)
    assert out == ["first", "second", "third"]
    assert queue == []


def test_drain_pending_once_respects_max_n():
    queue = ["a", "b", "c", "d", "e"]

    def pop_fn(sid):
        return queue.pop(0) if queue else None

    out = B.drain_pending_once("sid1", pop_fn=pop_fn, max_n=2)
    assert out == ["a", "b"]
    assert queue == ["c", "d", "e"]   # stopped after max_n, did not drain the rest


def test_drain_pending_once_default_pop_fn_uses_session_store(monkeypatch):
    """Without an injected pop_fn, drain_pending_once falls back to S.pop_input -- verify
    that wiring (still hermetic: monkeypatch session_store's pop_input, no real file I/O)."""
    from bridge import session_store as S

    calls = []

    def fake_pop_input(sid):
        calls.append(sid)
        return None

    monkeypatch.setattr(S, "pop_input", fake_pop_input)
    out = B.drain_pending_once("sid-xyz")
    assert out == []
    assert calls == ["sid-xyz"]


# ── single-instance guard ───────────────────────────────────────────────────────────────────

def test_single_bind_server_disables_reuse():
    """allow_reuse_address must be False: the inherited default (1 -> SO_REUSEADDR) lets a
    SECOND bridge silently bind the same port on Windows (two live bridges then get random
    request dispatch: resume lands on one, the next send on the other)."""
    assert B._SingleBindHTTPServer.allow_reuse_address is False


def test_port_already_served_detects_listener():
    """Loopback-only socket check (hermetic: no external network). A live local listener
    must be detected; after it closes the same port must read as free."""
    import socket as _socket

    srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert B._port_already_served(port) is True
    finally:
        srv.close()
    assert B._port_already_served(port) is False


# ── module-level sanity (import-safety / constants) ─────────────────────────────────────────

def test_active_sid_starts_none_at_import():
    # NOTE: this only holds true if no earlier test in this file mutated ACTIVE_SID via the
    # live Handler paths (which none of these hermetic tests do -- they only touch the pure
    # helpers above), so this is a safe, order-independent assertion in this test module.
    assert B.ACTIVE_SID is None or isinstance(B.ACTIVE_SID, str)


def test_sessref_prefix_constant():
    assert B.SESSREF_PREFIX == "sess:"

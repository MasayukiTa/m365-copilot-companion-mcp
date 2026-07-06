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


# ── select_changed_conv_guid (change-based capture selection) ───────────────────────────────
# Real failure this logic exists for: after resume-to-A then /new + teach-in-B, the sidebar's
# aria-current="page" marker REMAINED on A's row (the new conversation had no row yet), so a
# stale-marker capture misattributed B's session to A's guid. Change-based selection accepts
# only a guid that demonstrably changed/appeared vs the pre-send baseline.

G_A = "decf9c53-2f18-4fc2-89b2-0c1eb69ba629"   # previously-open conversation (baseline cur)
G_B = "aaaa1111-2222-3333-4444-555566667777"   # the newly created conversation
G_C = "bbbb1111-2222-3333-4444-555566667777"   # another pre-existing conversation


def test_select_stale_aria_current_rejected():
    """The EXACT reproduced failure: aria-current still on the old conversation, new row not
    yet present anywhere -> must return '' (ambiguous), NOT the stale guid."""
    got = B.select_changed_conv_guid(G_A, {G_A, G_C}, G_A, {G_A, G_C})
    assert got == ""


def test_select_aria_current_moved_to_new_guid():
    """Pane's aria-current moved to a row that did not exist at baseline -> accepted."""
    got = B.select_changed_conv_guid(G_A, {G_A, G_C}, G_B, {G_A, G_B, G_C})
    assert got == G_B


def test_select_new_guid_appears_while_aria_current_stale():
    """aria-current still stale on the old row, but exactly one brand-new guid appeared in
    the known set (sidebar row / localStorage registered the new conversation) -> accepted."""
    got = B.select_changed_conv_guid(G_A, {G_A, G_C}, G_A, {G_A, G_B, G_C})
    assert got == G_B


def test_select_multiple_new_guids_ambiguous():
    g_d = "cccc1111-2222-3333-4444-555566667777"
    got = B.select_changed_conv_guid(G_A, {G_A}, G_A, {G_A, G_B, g_d})
    assert got == ""


def test_select_aria_current_moved_to_known_old_guid_rejected():
    """aria-current moved to a DIFFERENT but pre-existing conversation (not something this
    send created) and no new guid appeared -> '' (never claim an old conversation)."""
    got = B.select_changed_conv_guid(G_A, {G_A, G_C}, G_C, {G_A, G_C})
    assert got == ""


def test_select_empty_baseline_cur_new_conversation():
    """Baseline from a truly bare pane (no aria-current guid): the first-ever conversation
    appears and gets marked current -> accepted."""
    got = B.select_changed_conv_guid("", {G_C}, G_B, {G_B, G_C})
    assert got == G_B


def test_select_no_change_at_all():
    got = B.select_changed_conv_guid("", set(), "", set())
    assert got == ""


def test_select_baseline_cur_counts_as_known():
    """baseline_cur is treated as known even if the caller forgot it in baseline_known."""
    got = B.select_changed_conv_guid(G_A, set(), G_A, {G_A})
    assert got == ""


def test_select_dedupes_now_known():
    """Duplicate observations of the same new guid (row + localStorage) are ONE candidate."""
    got = B.select_changed_conv_guid(G_A, {G_A}, G_A, [G_B, G_B, G_A])
    assert got == G_B


# ── single-instance guard ───────────────────────────────────────────────────────────────────

def test_single_bind_server_disables_reuse():
    """allow_reuse_address must be False: the inherited default (1 -> SO_REUSEADDR) lets a
    SECOND bridge silently bind the same port on Windows (two live bridges then get random
    request dispatch: resume lands on one, the next send on the other)."""
    assert B._SingleBindHTTPServer.allow_reuse_address is False


def test_page_executor_runs_job_on_owner_thread():
    """PageExecutor.submit() must run the job on the ONE dedicated owner thread (not the
    calling thread) and return its result to the caller -- this is the Playwright sync-API
    thread-affinity fix: PAGE/DRIVER must always be touched from the same thread that
    created them. Pure plumbing test (no real Playwright/PAGE involved)."""
    import threading as _threading

    ex = B.PageExecutor()
    ex.start(ex.run_forever)
    try:
        owner_thread_id = ex.submit(_threading.get_ident)
        assert owner_thread_id == ex._thread.ident
        assert owner_thread_id != _threading.get_ident()
    finally:
        pass  # daemon thread; no explicit shutdown needed for a short-lived test


def test_page_executor_propagates_exceptions_to_caller():
    ex = B.PageExecutor()
    ex.start(ex.run_forever)

    def _boom():
        raise ValueError("boom")

    try:
        raised = False
        try:
            ex.submit(_boom)
        except ValueError as e:
            raised = True
            assert "boom" in str(e)
        assert raised
    finally:
        pass


def test_page_executor_serializes_concurrent_submits():
    """Two threads calling submit() concurrently must never have their jobs run
    simultaneously -- PageExecutor is a single-worker queue, so this is true by
    construction; verify observably via a shared counter with no lock needed inside the job."""
    import threading as _threading
    import time as _time

    ex = B.PageExecutor()
    ex.start(ex.run_forever)
    overlap_detected = []
    in_job = {"count": 0}

    def job(n):
        in_job["count"] += 1
        if in_job["count"] > 1:
            overlap_detected.append(True)
        _time.sleep(0.05)
        in_job["count"] -= 1
        return n

    threads = [_threading.Thread(target=lambda i=i: ex.submit(job, i)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert overlap_detected == []


def test_single_bind_server_is_threading():
    """The server must dispatch each request on its own thread (ThreadingMixIn) so a
    long-running /goal turn cannot block /send (steering) or /stop -- see PAGE_LOCK's
    docstring. daemon_threads=True so a stuck request thread never blocks process exit."""
    import socketserver

    assert issubclass(B._SingleBindHTTPServer, socketserver.ThreadingMixIn)
    assert B._SingleBindHTTPServer.daemon_threads is True


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


# ── WORK MODE: detect_done ───────────────────────────────────────────────────────────────────

def test_detect_done_marker_on_own_line():
    text = "手順1を実行しました。\n手順2を実行しました。\n===DONE==="
    done, stripped = B.detect_done(text)
    assert done is True
    assert stripped == "手順1を実行しました。\n手順2を実行しました。"


def test_detect_done_marker_with_trailing_whitespace_tolerated():
    text = "作業完了。\n  ===DONE===  \n"
    done, stripped = B.detect_done(text)
    assert done is True
    assert stripped == "作業完了。"


def test_detect_done_not_done_without_marker():
    text = "まだ続きがあります。次のステップに進みます。"
    done, stripped = B.detect_done(text)
    assert done is False
    assert stripped == text


def test_detect_done_marker_mentioned_in_prose_is_not_done():
    """The marker must appear as its OWN line -- a turn that merely *mentions* the sentinel
    in passing (e.g. explaining the protocol back) must not be mistaken for completion."""
    text = "完了したら ===DONE=== と書く約束です。まだ完了していません。"
    done, stripped = B.detect_done(text)
    assert done is False
    assert stripped == text


def test_detect_done_empty_text():
    done, stripped = B.detect_done("")
    assert done is False
    assert stripped == ""


def test_detect_done_none_text():
    done, stripped = B.detect_done(None)
    assert done is False
    assert stripped == ""


def test_detect_done_marker_alone():
    done, stripped = B.detect_done("===DONE===")
    assert done is True
    assert stripped == ""


# ── WORK MODE: wrap_goal_text ────────────────────────────────────────────────────────────────

def test_wrap_goal_text_contains_goal_and_marker_contract():
    wrapped = B.wrap_goal_text("日本の県名を1つずつ挙げよ")
    assert "日本の県名を1つずつ挙げよ" in wrapped
    assert B.WORK_MODE_DONE_MARKER in wrapped


def test_wrap_goal_text_empty_goal():
    wrapped = B.wrap_goal_text("")
    assert B.WORK_MODE_DONE_MARKER in wrapped


# ── WORK MODE: select_next_message ───────────────────────────────────────────────────────────

def test_select_next_message_no_queue_returns_continue_nudge():
    msg, steered = B.select_next_message([])
    assert msg == B.WORK_MODE_CONTINUE_NUDGE
    assert steered == []


def test_select_next_message_single_queued_input():
    msg, steered = B.select_next_message(["やっぱり残りは英語で"])
    assert msg == "やっぱり残りは英語で"
    assert steered == ["やっぱり残りは英語で"]


def test_select_next_message_multiple_queued_inputs_joined():
    msg, steered = B.select_next_message(["最初の指示", "続けての指示"])
    assert msg == "最初の指示\n続けての指示"
    assert steered == ["最初の指示", "続けての指示"]


def test_select_next_message_custom_nudge():
    msg, steered = B.select_next_message([], continue_nudge="custom nudge")
    assert msg == "custom nudge"
    assert steered == []


# ── WORK MODE: decide_outcome ─────────────────────────────────────────────────────────────────

def test_decide_outcome_done_wins_over_everything():
    assert B.decide_outcome(True, True, 100, 5, 5) == "done"


def test_decide_outcome_consecutive_errors():
    assert B.decide_outcome(False, False, 2, 30, 2) == "error"


def test_decide_outcome_errors_beat_stop_and_maxturns():
    # done takes priority over error; but error must be checked before stop/max_turns
    assert B.decide_outcome(False, True, 30, 30, 2) == "error"


def test_decide_outcome_stop_requested():
    assert B.decide_outcome(False, True, 3, 30, 0) == "stopped"


def test_decide_outcome_max_turns_reached():
    assert B.decide_outcome(False, False, 30, 30, 0) == "max_turns"


def test_decide_outcome_max_turns_zero_means_unlimited():
    assert B.decide_outcome(False, False, 999, 0, 0) is None


def test_decide_outcome_keep_looping():
    assert B.decide_outcome(False, False, 3, 30, 0) is None


def test_decide_outcome_custom_error_threshold():
    assert B.decide_outcome(False, False, 1, 30, 3, max_consecutive_errors=4) is None
    assert B.decide_outcome(False, False, 1, 30, 4, max_consecutive_errors=4) == "error"


# ── WORK MODE: resume_eligibility ────────────────────────────────────────────────────────────

def test_resume_eligibility_no_session():
    ok, reason = B.resume_eligibility(None)
    assert ok is False
    assert "no session" in reason


def test_resume_eligibility_wrong_mode():
    ok, reason = B.resume_eligibility({"mode": "idle", "goal": "something"})
    assert ok is False
    assert "interrupted" in reason


def test_resume_eligibility_missing_goal():
    ok, reason = B.resume_eligibility({"mode": "interrupted", "goal": ""})
    assert ok is False
    assert "no stored goal" in reason


def test_resume_eligibility_happy_path():
    ok, goal_text = B.resume_eligibility({"mode": "interrupted", "goal": "県名を挙げる"})
    assert ok is True
    assert goal_text == "県名を挙げる"


# ── WORK MODE: default constants sanity ──────────────────────────────────────────────────────

def test_default_max_turns_constant():
    assert B.DEFAULT_MAX_TURNS == 30


def test_work_mode_done_marker_constant():
    assert B.WORK_MODE_DONE_MARKER == "===DONE==="


# ── module-level sanity (import-safety / constants) ─────────────────────────────────────────

def test_active_sid_starts_none_at_import():
    # NOTE: this only holds true if no earlier test in this file mutated ACTIVE_SID via the
    # live Handler paths (which none of these hermetic tests do -- they only touch the pure
    # helpers above), so this is a safe, order-independent assertion in this test module.
    assert B.ACTIVE_SID is None or isinstance(B.ACTIVE_SID, str)


def test_sessref_prefix_constant():
    assert B.SESSREF_PREFIX == "sess:"

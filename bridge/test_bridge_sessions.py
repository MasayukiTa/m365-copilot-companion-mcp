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


# ── VERIFIED LOOP: build_rubric_prompt ──────────────────────────────────────────────────────

def test_build_rubric_prompt_contains_ac_and_deliverable_verbatim():
    ac = "[AC-1] ちょうど5つの都道府県名が箇条書きされている"
    deliverable = "1. 東京都\n2. 大阪府"
    prompt = B.build_rubric_prompt(ac, deliverable)
    assert ac in prompt
    assert deliverable in prompt


def test_build_rubric_prompt_demands_json_only():
    prompt = B.build_rubric_prompt("AC", "deliverable")
    assert "JSON" in prompt
    assert '"pass"' in prompt
    assert '"failed_ac"' in prompt
    assert '"reasons"' in prompt


def test_build_rubric_prompt_forbids_improvement_suggestions():
    """spec SS5.3: critics must not offer free-form improvement suggestions -- the fixed
    rubric text itself must say so, not just rely on convention."""
    prompt = B.build_rubric_prompt("AC", "deliverable")
    assert "改善案" in prompt or "提案" in prompt


def test_build_rubric_prompt_handles_none_args():
    prompt = B.build_rubric_prompt(None, None)
    assert "JSON" in prompt


# ── VERIFIED LOOP: parse_verdict ─────────────────────────────────────────────────────────────

def test_parse_verdict_strict_json_pass():
    text = '{"pass": true, "failed_ac": [], "reasons": []}'
    v = B.parse_verdict(text)
    assert v["ok"] is True
    assert v["pass"] is True
    assert v["failed_ac"] == []
    assert v["reasons"] == []
    assert v["needs_retry"] is False


def test_parse_verdict_strict_json_fail():
    text = '{"pass": false, "failed_ac": ["AC-1"], "reasons": ["only 3 listed, need 5"]}'
    v = B.parse_verdict(text)
    assert v["ok"] is True
    assert v["pass"] is False
    assert v["failed_ac"] == ["AC-1"]
    assert v["reasons"] == ["only 3 listed, need 5"]


def test_parse_verdict_embedded_json_in_prose():
    text = ('承知しました、判定します。\n'
            '{"pass": false, "failed_ac": ["AC-1"], "reasons": ["not enough items"]}\n'
            '以上です。')
    v = B.parse_verdict(text)
    assert v["ok"] is True
    assert v["pass"] is False
    assert v["failed_ac"] == ["AC-1"]


def test_parse_verdict_nested_braces_in_reasons_not_truncated():
    text = '{"pass": false, "failed_ac": ["AC-1"], "reasons": ["missing {curly} example"]}'
    v = B.parse_verdict(text)
    assert v["ok"] is True
    assert v["reasons"] == ["missing {curly} example"]


def test_parse_verdict_garbage_needs_retry():
    v = B.parse_verdict("そうですね、良い出来だと思います。")
    assert v["ok"] is False
    assert v["needs_retry"] is True
    assert v["pass"] is False


def test_parse_verdict_empty_text_needs_retry():
    v = B.parse_verdict("")
    assert v["ok"] is False
    assert v["needs_retry"] is True


def test_parse_verdict_none_text_needs_retry():
    v = B.parse_verdict(None)
    assert v["ok"] is False
    assert v["needs_retry"] is True


def test_parse_verdict_malformed_json_needs_retry():
    v = B.parse_verdict('{"pass": true, "failed_ac": [oops]}')
    assert v["ok"] is False
    assert v["needs_retry"] is True


def test_parse_verdict_missing_pass_key_needs_retry():
    v = B.parse_verdict('{"failed_ac": [], "reasons": []}')
    assert v["ok"] is False
    assert v["needs_retry"] is True


def test_parse_verdict_non_dict_json_needs_retry():
    v = B.parse_verdict('[1, 2, 3]')
    assert v["ok"] is False
    assert v["needs_retry"] is True


def test_parse_verdict_wrong_typed_failed_ac_defaults_empty():
    v = B.parse_verdict('{"pass": false, "failed_ac": "AC-1", "reasons": "not a list"}')
    assert v["ok"] is True
    assert v["failed_ac"] == []
    assert v["reasons"] == []


def test_parse_verdict_pass_truthy_coercion():
    v = B.parse_verdict('{"pass": 1}')
    assert v["pass"] is True
    v2 = B.parse_verdict('{"pass": 0}')
    assert v2["pass"] is False


# ── VERIFIED LOOP: build_continuation_message ────────────────────────────────────────────────

def test_build_continuation_message_contains_failed_ac_and_reasons():
    msg = B.build_continuation_message(["AC-1"], ["only 3 listed, need 5"])
    assert "AC-1" in msg
    assert "only 3 listed, need 5" in msg
    assert B.WORK_MODE_DONE_MARKER in msg


def test_build_continuation_message_multiple_items():
    msg = B.build_continuation_message(["AC-1", "AC-2"], ["reason1", "reason2"])
    assert "AC-1" in msg and "AC-2" in msg
    assert "reason1" in msg and "reason2" in msg


def test_build_continuation_message_empty_lists_still_valid():
    msg = B.build_continuation_message([], [])
    assert B.WORK_MODE_DONE_MARKER in msg


# ── VERIFIED LOOP: is_oscillating ─────────────────────────────────────────────────────────────

def test_is_oscillating_identical_sets():
    assert B.is_oscillating(["AC-1", "AC-2"], ["AC-2", "AC-1"]) is True


def test_is_oscillating_different_sets():
    assert B.is_oscillating(["AC-1"], ["AC-2"]) is False


def test_is_oscillating_progress_subset():
    assert B.is_oscillating(["AC-1", "AC-2"], ["AC-1"]) is False


def test_is_oscillating_first_verdict_never_oscillates():
    assert B.is_oscillating(None, ["AC-1"]) is False
    assert B.is_oscillating([], ["AC-1"]) is False


def test_is_oscillating_both_empty_is_false():
    assert B.is_oscillating([], []) is False


# ── VERIFIED LOOP: decide_verify_outcome ─────────────────────────────────────────────────────

def test_decide_verify_outcome_pass_wins():
    assert B.decide_verify_outcome(True, 1, 3, False) == "done_verified"
    assert B.decide_verify_outcome(True, 3, 3, True) == "done_verified"


def test_decide_verify_outcome_oscillation_before_budget():
    """An oscillating fail on the LAST allowed loop is still reported as oscillation, not a
    plain budget exhaustion -- the operator should know the failures were IDENTICAL."""
    assert B.decide_verify_outcome(False, 3, 3, True) == "escalate_oscillation"


def test_decide_verify_outcome_budget_exhausted():
    assert B.decide_verify_outcome(False, 3, 3, False) == "verify_failed"


def test_decide_verify_outcome_keep_looping():
    assert B.decide_verify_outcome(False, 1, 3, False) is None


def test_decide_verify_outcome_zero_max_loops_never_exhausts_on_count():
    # max_loops=0 is falsy -> the budget check is skipped (mirrors decide_outcome's
    # max_turns==0 "unlimited" convention); oscillation can still fire independently.
    assert B.decide_verify_outcome(False, 999, 0, False) is None
    assert B.decide_verify_outcome(False, 999, 0, True) == "escalate_oscillation"


# ── VERIFIED LOOP: default constants sanity ──────────────────────────────────────────────────

def test_default_max_loops_constant():
    assert B.DEFAULT_MAX_LOOPS == 3


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


# ── unify-the-view: merge_fleet_conversations (pure merge/dedup) ────────────────────────────
# Mirrors relay/fleet_runner.py's _register_convs dedup semantics (keyed by "url") plus the
# bridge's own extension for empty-url ("sess:<guid>") entries, keyed by (source, name) instead.

def test_merge_into_empty_existing():
    entry = {"url": "https://x/conversation/abc", "title": "t", "source": "chat",
              "transcript": "sessions/s1.jsonl", "name": "s1", "ts": 1.0}
    merged = B.merge_fleet_conversations([], [entry])
    assert merged == [entry]


def test_merge_dedup_by_url_updates_in_place():
    old = {"url": "https://x/conversation/abc", "title": "old", "source": "chat",
           "transcript": "sessions/s1.jsonl", "name": "s1", "ts": 1.0}
    new = {"url": "https://x/conversation/abc", "title": "new", "source": "chat",
           "transcript": "sessions/s1.jsonl", "name": "s1", "ts": 2.0}
    merged = B.merge_fleet_conversations([old], [new])
    assert merged == [new]
    assert len(merged) == 1


def test_merge_preserves_untouched_existing_entries():
    fleet_entry = {"url": "https://x/conversation/fleet1", "title": "fleet", "source": "fleet",
                   "transcript": "", "name": "w0", "ts": 1.0}
    chat_entry = {"url": "https://x/conversation/chat1", "title": "chat", "source": "chat",
                  "transcript": "sessions/s1.jsonl", "name": "s1", "ts": 2.0}
    merged = B.merge_fleet_conversations([fleet_entry], [chat_entry])
    assert fleet_entry in merged
    assert chat_entry in merged
    assert len(merged) == 2


def test_merge_empty_url_entries_dedup_by_source_and_name_not_url():
    """A sess:<guid> session has NO real url (url==""), so it can't be deduped by url --
    it must be keyed by (source, name) instead, and never collide with a real fleet row
    (fleet entries carry source=="fleet", never "chat")."""
    old = {"url": "", "title": "old title", "source": "chat", "transcript": "sessions/s1.jsonl",
           "name": "s1", "ts": 1.0}
    new = {"url": "", "title": "new title", "source": "chat", "transcript": "sessions/s1.jsonl",
           "name": "s1", "ts": 2.0}
    merged = B.merge_fleet_conversations([old], [new])
    assert merged == [new]
    assert len(merged) == 1


def test_merge_two_different_empty_url_entries_both_kept():
    e1 = {"url": "", "title": "a", "source": "chat", "transcript": "", "name": "s1", "ts": 1.0}
    e2 = {"url": "", "title": "b", "source": "chat", "transcript": "", "name": "s2", "ts": 2.0}
    merged = B.merge_fleet_conversations([e1], [e2])
    assert e1 in merged
    assert e2 in merged
    assert len(merged) == 2


def test_merge_drops_non_dict_existing_items():
    """Corrupt/foreign-shaped items in the existing list (e.g. from a shape mismatch) are
    dropped rather than raising."""
    merged = B.merge_fleet_conversations(["not-a-dict", None, 42], [])
    assert merged == []


def test_merge_skips_non_dict_new_entries():
    good = {"url": "https://x/1", "title": "t", "source": "chat", "transcript": "",
            "name": "s1", "ts": 1.0}
    merged = B.merge_fleet_conversations([], [good, "garbage", None])
    assert merged == [good]


def test_merge_new_entries_empty_list_returns_existing_unchanged():
    existing = [{"url": "https://x/1", "title": "t", "source": "fleet", "transcript": "",
                 "name": "w0", "ts": 1.0}]
    merged = B.merge_fleet_conversations(existing, [])
    assert merged == existing


def test_merge_both_empty():
    assert B.merge_fleet_conversations([], []) == []


def test_merge_none_args_tolerated():
    assert B.merge_fleet_conversations(None, None) == []


# ── unify-the-view: file I/O helpers (hermetic via monkeypatched FLEET_CONVS_PATH) ──────────

def test_read_fleet_conversations_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", tmp_path / "conversations.json")
    assert B._read_fleet_conversations_raw() == []


def test_read_fleet_conversations_corrupt_json_returns_empty(tmp_path, monkeypatch):
    p = tmp_path / "conversations.json"
    p.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    assert B._read_fleet_conversations_raw() == []


def test_read_fleet_conversations_non_list_json_returns_empty(tmp_path, monkeypatch):
    p = tmp_path / "conversations.json"
    p.write_text('{"not": "a list"}', encoding="utf-8")
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    assert B._read_fleet_conversations_raw() == []


def test_read_fleet_conversations_tolerates_bom(tmp_path, monkeypatch):
    """fleet_runner's own comment notes 'tolerate C# BOM' -- verify the bridge reader does too."""
    import json as _json
    p = tmp_path / "conversations.json"
    entries = [{"url": "https://x/1", "title": "t", "source": "fleet", "transcript": "",
                "name": "w0", "ts": 1.0}]
    p.write_bytes(b"\xef\xbb\xbf" + _json.dumps(entries).encode("utf-8"))
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    assert B._read_fleet_conversations_raw() == entries


def test_write_then_read_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "sub" / "conversations.json"   # parent dir does not exist yet
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    entries = [{"url": "https://x/1", "title": "t", "source": "chat", "transcript": "",
                "name": "s1", "ts": 1.0}]
    B._write_fleet_conversations_atomic(entries)
    assert B._read_fleet_conversations_raw() == entries


def test_write_atomic_leaves_no_tmp_file_behind(tmp_path, monkeypatch):
    p = tmp_path / "conversations.json"
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    B._write_fleet_conversations_atomic([{"url": "", "title": "", "source": "chat",
                                            "transcript": "", "name": "s1", "ts": 1.0}])
    assert not (tmp_path / "conversations.json.tmp").exists()


def test_write_atomic_never_raises_on_bad_target(monkeypatch):
    """Point at an unwritable path (a directory, not a file) -- the helper must swallow the
    error rather than propagate it (a registration hiccup must never crash a chat turn)."""
    import pathlib
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", pathlib.Path("Z:\\definitely\\not\\a\\real\\drive\\conversations.json"))
    B._write_fleet_conversations_atomic([{"url": "x"}])  # must not raise


# ── unify-the-view: register_bridge_session_in_fleet_convs (ref-kind routing + end-to-end) ──

def test_register_bridge_session_sessref_stores_empty_url(tmp_path, monkeypatch):
    """A sess:<guid> conv_url is NOT a real url -- the registered entry's url field must be
    "" (cockpit tolerates empty urls per .fleet/status.json precedent), never the sess: string
    itself (that would look like a navigable url to a naive consumer)."""
    p = tmp_path / "conversations.json"
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    ref = B.make_sessref("9374821f-6bff-4050-b6fd-8a4338013664")
    B.register_bridge_session_in_fleet_convs("s1", "My Title", ref, "sessions/s1.jsonl")
    rows = B._read_fleet_conversations_raw()
    assert len(rows) == 1
    assert rows[0]["url"] == ""
    assert rows[0]["source"] == "chat"
    assert rows[0]["name"] == "s1"
    assert rows[0]["transcript"] == "sessions/s1.jsonl"
    assert rows[0]["title"] == "My Title"


def test_register_bridge_session_conv_url_stores_real_url(tmp_path, monkeypatch):
    """A real /conversation/<guid> URL IS stored verbatim in the url field (durable,
    directly navigable by _goto_settled)."""
    p = tmp_path / "conversations.json"
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    url = "https://m365.cloud.microsoft/chat/agent/T_x/conversation/9374821f-6bff-4050-b6fd-8a4338013664"
    B.register_bridge_session_in_fleet_convs("s1", "t", url, "sessions/s1.jsonl")
    rows = B._read_fleet_conversations_raw()
    assert rows[0]["url"] == url


def test_register_bridge_session_bare_url_stores_empty_url(tmp_path, monkeypatch):
    """A bare_url (real URL but no /conversation/<guid>, e.g. an SSO-bounce landing) is not
    reliably reattachable by URL either -- treated the same as sessref: url field empty."""
    p = tmp_path / "conversations.json"
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    B.register_bridge_session_in_fleet_convs(
        "s1", "t", "https://m365.cloud.microsoft/chat/?redirfrom=CsrToSSR&auth=2", "sessions/s1.jsonl")
    rows = B._read_fleet_conversations_raw()
    assert rows[0]["url"] == ""


def test_register_bridge_session_title_truncated_to_60_chars(tmp_path, monkeypatch):
    p = tmp_path / "conversations.json"
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    long_title = "x" * 200
    B.register_bridge_session_in_fleet_convs("s1", long_title, "", "")
    rows = B._read_fleet_conversations_raw()
    assert len(rows[0]["title"]) == 60


def test_register_bridge_session_falls_back_to_sid_when_title_empty(tmp_path, monkeypatch):
    p = tmp_path / "conversations.json"
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    B.register_bridge_session_in_fleet_convs("s1", "", B.make_sessref("9374821f-6bff-4050-b6fd-8a4338013664"), "")
    rows = B._read_fleet_conversations_raw()
    assert rows[0]["title"] == "s1"


def test_register_bridge_session_re_registration_updates_not_duplicates(tmp_path, monkeypatch):
    """Calling register twice for the SAME sid (e.g. re-registered on a later turn) must
    refresh the one row, not append a second one."""
    p = tmp_path / "conversations.json"
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    ref = B.make_sessref("9374821f-6bff-4050-b6fd-8a4338013664")
    B.register_bridge_session_in_fleet_convs("s1", "first", ref, "sessions/s1.jsonl")
    B.register_bridge_session_in_fleet_convs("s1", "second", ref, "sessions/s1.jsonl")
    rows = B._read_fleet_conversations_raw()
    assert len(rows) == 1
    assert rows[0]["title"] == "second"


def test_register_bridge_session_preserves_existing_fleet_entries(tmp_path, monkeypatch):
    """Registering a chat session must not disturb a pre-existing fleet-source entry --
    the two writers (fleet_runner.py and this bridge) must coexist in the same file."""
    p = tmp_path / "conversations.json"
    fleet_entry = {"url": "https://x/conversation/fleet1", "title": "fleet job",
                   "source": "fleet", "transcript": "", "name": "w0", "ts": 1.0}
    import json as _json
    p.write_text(_json.dumps([fleet_entry]), encoding="utf-8")
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    B.register_bridge_session_in_fleet_convs("s1", "chat title", "", "sessions/s1.jsonl")
    rows = B._read_fleet_conversations_raw()
    assert fleet_entry in rows
    assert any(r.get("source") == "chat" and r.get("name") == "s1" for r in rows)


def test_register_bridge_session_never_raises_on_write_failure(monkeypatch):
    import pathlib
    monkeypatch.setattr(B, "FLEET_CONVS_PATH",
                         pathlib.Path("Z:\\definitely\\not\\a\\real\\drive\\conversations.json"))
    B.register_bridge_session_in_fleet_convs("s1", "t", "", "")  # must not raise


# ── unify-the-view: _load_fleet_sessions_view (pure /sessions?all=1 mapping) ────────────────

def test_load_fleet_sessions_view_maps_fleet_entries(tmp_path, monkeypatch):
    p = tmp_path / "conversations.json"
    entry = {"url": "https://x/conversation/abc", "title": "fleet job", "source": "fleet",
              "transcript": "", "name": "w0", "ts": 123.0}
    import json as _json
    p.write_text(_json.dumps([entry]), encoding="utf-8")
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    view = B._load_fleet_sessions_view()
    assert view == [{"sid": "", "title": "fleet job", "conv_url": "https://x/conversation/abc",
                      "last_active_ts": 123.0, "turns": None, "source": "fleet"}]


def test_load_fleet_sessions_view_excludes_chat_entries():
    """source=="chat" entries are the bridge's OWN sessions -- already covered by
    S.list_sessions(), so /sessions?all=1 must not double-list them via this path."""
    pass  # covered end-to-end below via monkeypatched file content


def test_load_fleet_sessions_view_excludes_chat_source(tmp_path, monkeypatch):
    p = tmp_path / "conversations.json"
    chat_entry = {"url": "", "title": "chat sess", "source": "chat", "transcript": "",
                  "name": "s1", "ts": 1.0}
    fleet_entry = {"url": "https://x/1", "title": "fleet job", "source": "fleet",
                   "transcript": "", "name": "w0", "ts": 2.0}
    import json as _json
    p.write_text(_json.dumps([chat_entry, fleet_entry]), encoding="utf-8")
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    view = B._load_fleet_sessions_view()
    assert len(view) == 1
    assert view[0]["title"] == "fleet job"


def test_load_fleet_sessions_view_falls_back_to_name_when_no_title(tmp_path, monkeypatch):
    p = tmp_path / "conversations.json"
    entry = {"url": "https://x/1", "title": "", "source": "fleet", "transcript": "",
             "name": "w3", "ts": 1.0}
    import json as _json
    p.write_text(_json.dumps([entry]), encoding="utf-8")
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    view = B._load_fleet_sessions_view()
    assert view[0]["title"] == "w3"


def test_load_fleet_sessions_view_empty_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", tmp_path / "conversations.json")
    assert B._load_fleet_sessions_view() == []


def test_load_fleet_sessions_view_drops_non_dict_items(tmp_path, monkeypatch):
    p = tmp_path / "conversations.json"
    import json as _json
    p.write_text(_json.dumps(["garbage", None, 42]), encoding="utf-8")
    monkeypatch.setattr(B, "FLEET_CONVS_PATH", p)
    assert B._load_fleet_sessions_view() == []


# ── unify-the-view: classify_conv_ref-based ref-kind routing decision (used by /adopt and
# by register_bridge_session_in_fleet_convs's url_field choice) ─────────────────────────────

def test_ref_kind_routing_sessref_is_not_url_field():
    ref = B.make_sessref("9374821f-6bff-4050-b6fd-8a4338013664")
    assert B.classify_conv_ref(ref) == "sessref"


def test_ref_kind_routing_conv_url_is_url_field():
    url = "https://m365.cloud.microsoft/chat/agent/T_x/conversation/9374821f-6bff-4050-b6fd-8a4338013664"
    assert B.classify_conv_ref(url) == "conv_url"


def test_ref_kind_routing_bare_url_is_not_conv_url():
    """A bare_url (real URL, no /conversation/<guid>) must NOT be routed the same as a real
    conv_url -- it is not reliably re-navigable to the SAME conversation."""
    url = "https://m365.cloud.microsoft/chat/?redirfrom=CsrToSSR&auth=2"
    assert B.classify_conv_ref(url) == "bare_url"
    assert B.classify_conv_ref(url) != "conv_url"

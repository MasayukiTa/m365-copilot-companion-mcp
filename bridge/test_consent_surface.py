"""Hermetic unit tests for the two consent-recovery bug fixes in bridge/copilot_bridge.py:

BUG 2 -- the foreground last-resort surface() used to be a single module bool
(_CONSENT_SURFACED) latched True UNCONDITIONALLY before surface() was even attempted, so any
transient failure permanently disabled surfacing for the rest of the process's lifetime. It is
now _consent_surface_attempt(): success-only-latching, bounded to CONSENT_SURFACE_RETRY_MAX
attempts per "episode", with the episode resettable via _reset_consent_surface_episode().

BUG 1 -- the idle tool-call self-probe (_run_tool_probe) used to only RECORD a consent_card
sighting; it now DRIVES recovery (auto-consent, then the bounded surface() last resort).

No browser/Playwright/CDP is touched: bridge.copilot_bridge only imports playwright lazily
inside main()/the page-owner thread (see bridge/test_bridge_sessions.py's docstring for the
same hermetic-import argument), and every Playwright-touching call this test exercises is
monkeypatched out (relay.edge_recover.surface, B._bridge_auto_consent, B._do_tool_probe_turn,
B._run_bounded_page_probe_call).
"""
import relay.edge_recover as edge_recover
import bridge.copilot_bridge as B


# ── fixtures / helpers ──────────────────────────────────────────────────────────────────────

def _reset_state(monkeypatch):
    """Put every piece of module-global state this suite touches into a known, isolated
    starting point. monkeypatch.setattr auto-restores each on test teardown."""
    monkeypatch.setattr(B, "_CONSENT_SURFACE_OK", False)
    monkeypatch.setattr(B, "_CONSENT_SURFACE_ATTEMPTS", 0)
    monkeypatch.setattr(B, "_CONSENT_SURFACE_TERMINAL_SENT", False)
    monkeypatch.setattr(B, "CONSENT_SURFACE_RETRY_MAX", 3)
    monkeypatch.setattr(B, "AGENT_URL", "https://example.invalid/agent")
    monkeypatch.setattr(B, "PAGE", object())  # non-None sentinel; AGENT_URL short-circuits .url
    monkeypatch.setattr(B, "_PAGE_UNREACHABLE_STREAK", 0)
    # Avoid a real 90s background rehide timer lingering past the test.
    monkeypatch.setattr(B, "_schedule_force_rehide", lambda *a, **kw: None)


def _patch_surface(monkeypatch, results):
    """relay.edge_recover.surface is imported LOCALLY inside _consent_surface_attempt (`from
    relay.edge_recover import surface`), so patching the attribute on the real module object is
    what a fresh `from ... import` picks up at call time. `results` is an iterable of return
    values, one per call; extra calls beyond the iterable raise AssertionError (never expected
    to run this many times in a single test)."""
    it = iter(results)
    calls = []

    def _fake_surface(*a, **kw):
        calls.append((a, kw))
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("surface() called more times than the test expected")

    monkeypatch.setattr(edge_recover, "surface", _fake_surface)
    return calls


# ── _consent_surface_attempt: success-only latch ────────────────────────────────────────────

def test_surface_success_latches_and_does_not_resurface(monkeypatch):
    _reset_state(monkeypatch)
    calls = _patch_surface(monkeypatch, [True])

    assert B._consent_surface_attempt("ep1") is True
    assert B._CONSENT_SURFACE_OK is True
    assert len(calls) == 1

    # A second call this episode must NOT call surface() again (window already up) -- it should
    # still report True (bounded, within CONSENT_SURFACE_RETRY_MAX) without re-yanking it.
    assert B._consent_surface_attempt("ep1") is True
    assert len(calls) == 1


def test_failed_attempt_does_not_latch_and_stays_retryable(monkeypatch):
    """BUG 2's core fix: a failed attempt must NOT permanently disable future attempts."""
    _reset_state(monkeypatch)
    calls = _patch_surface(monkeypatch, [False, True])

    assert B._consent_surface_attempt("ep1") is False
    assert B._CONSENT_SURFACE_OK is False   # NOT latched on failure
    assert len(calls) == 1

    # Next consent failure retries surface() again (was: permanently a no-op before this fix).
    assert B._consent_surface_attempt("ep1") is True
    assert B._CONSENT_SURFACE_OK is True
    assert len(calls) == 2


# ── _consent_surface_attempt: bounded retry + terminal-honesty marker ──────────────────────

def test_bounded_retries_then_terminal_marker(monkeypatch):
    _reset_state(monkeypatch)
    monkeypatch.setattr(B, "CONSENT_SURFACE_RETRY_MAX", 3)
    _patch_surface(monkeypatch, [False, False, False])
    recorded = []
    # **kw で受ける。呼び出し側に引数が1つ増えるたびに壊れるスタブにしない
    # （既定値つきの追加は本番の呼び出しには後方互換で、壊れるのはここだけだった）。
    monkeypatch.setattr(B.tool_probe, "record_probe",
                         lambda ok, kind, detail="", ts=None, alive=None: recorded.append((ok, kind, detail)))

    for _ in range(3):
        assert B._consent_surface_attempt("ep1") is False
    # Budget now exhausted -- a 4th call must not call surface() again (already asserted by
    # _patch_surface's StopIteration guard if it tried) and must report the terminal state.
    assert B._consent_surface_attempt("ep1") is False
    assert B._CONSENT_SURFACE_ATTEMPTS >= B.CONSENT_SURFACE_RETRY_MAX

    kinds = [r[1] for r in recorded]
    assert "consent_unrecoverable" in kinds
    # Fired exactly once (guarded by _CONSENT_SURFACE_TERMINAL_SENT) despite two exhausted calls.
    assert kinds.count("consent_unrecoverable") == 1


def test_terminal_marker_contains_manual_recovery_command(monkeypatch):
    _reset_state(monkeypatch)
    monkeypatch.setattr(B, "CONSENT_SURFACE_RETRY_MAX", 1)
    _patch_surface(monkeypatch, [False])
    warnings = []
    monkeypatch.setattr(B.logger, "warning",
                         lambda msg, *a, **kw: warnings.append(msg % a if a else msg))
    monkeypatch.setattr(B.tool_probe, "record_probe", lambda *a, **kw: None)

    assert B._consent_surface_attempt("ep1") is False
    assert any("python -m relay.edge_reconnect --cdp-url http://127.0.0.1:9223" in w
               for w in warnings)


# ── _reset_consent_surface_episode: new-episode boundary ───────────────────────────────────

def test_reset_episode_gives_a_fresh_budget(monkeypatch):
    _reset_state(monkeypatch)
    monkeypatch.setattr(B, "CONSENT_SURFACE_RETRY_MAX", 1)
    _patch_surface(monkeypatch, [False])
    monkeypatch.setattr(B.tool_probe, "record_probe", lambda *a, **kw: None)

    assert B._consent_surface_attempt("ep1") is False
    assert B._consent_surface_attempt("ep1") is False   # exhausted, no new surface() call

    # A NEW episode (e.g. a later turn answered normally / auto-consent succeeded) resets state.
    B._reset_consent_surface_episode()
    assert B._CONSENT_SURFACE_ATTEMPTS == 0
    assert B._CONSENT_SURFACE_OK is False
    assert B._CONSENT_SURFACE_TERMINAL_SENT is False

    _patch_surface(monkeypatch, [True])
    assert B._consent_surface_attempt("ep2") is True


# ── _run_tool_probe: probe-result -> action decision (BUG 1) ───────────────────────────────

def _patch_probe_plumbing(monkeypatch, page_thread_results, auto_consent_result=None,
                           challenge_token="TESTTOKEN", arrived=True):
    """Stub out everything _run_tool_probe touches besides the pure classify/record logic:
    _run_bounded_page_probe_call (bypasses the real page-owner-thread queue),
    _do_tool_probe_turn (consumed once per call from `page_thread_results`), and
    _bridge_auto_consent (returns `auto_consent_result`).

    Also stubs tool_probe.new_probe_challenge() (which _run_tool_probe now calls before every
    turn it sends -- see tools/tool_probe.py's new_probe_challenge) to a fixed, deterministic
    (instruction, `challenge_token`) pair, so each turn in `page_thread_results` runs against
    a token the test controls instead of a real random one.

    `arrived` stubs tool_probe.probe_arrived, which is what decides a probe's success now that
    the verdict is "did the call reach the server" rather than "did the reply quote the token
    back" (see tools/tool_probe.py's verify_probe_arrival). It defaults to True so these
    tests keep asking what they were written to ask -- which recovery ladder runs for which
    reply -- with a healthy tool path underneath. Pass False for the case where the agent
    talks but never actually calls anything."""
    results = iter(page_thread_results)

    def _fake_run_on_page_thread(fn, *a, **kw):
        if fn is B._bridge_auto_consent:
            return auto_consent_result
        return next(results)

    monkeypatch.setattr(B, "_run_bounded_page_probe_call", _fake_run_on_page_thread)
    monkeypatch.setattr(B.tool_probe, "new_probe_challenge",
                        lambda *a, **kw: ("test challenge instruction", challenge_token))
    monkeypatch.setattr(B.tool_probe, "probe_arrived", lambda *a, **kw: arrived)
    monkeypatch.setattr(B, "_LAST_USER_TURN_TS", 0.0)
    monkeypatch.setattr(B, "MCP_TOOL_PROBE_SEC", 600.0)
    monkeypatch.setattr(B, "TOOL_PROBE_MIN_IDLE_SEC", 30.0)


def test_probe_answer_no_recovery_needed(monkeypatch):
    _reset_state(monkeypatch)
    _patch_probe_plumbing(monkeypatch, [(True, "found file: probe_TESTTOKEN.txt", False)])
    surface_calls = _patch_surface(monkeypatch, [])
    recorded = []
    # **kw で受ける。呼び出し側に引数が1つ増えるたびに壊れるスタブにしない
    # （既定値つきの追加は本番の呼び出しには後方互換で、壊れるのはここだけだった）。
    monkeypatch.setattr(B.tool_probe, "record_probe",
                         lambda ok, kind, **kw: recorded.append((ok, kind)))

    B._run_tool_probe()

    assert recorded[-1] == (True, "answer")
    assert surface_calls == []   # no consent card -> surface() never touched


def test_probe_consent_card_auto_consent_succeeds(monkeypatch):
    """kind=consent_card -> auto-consent tried -> succeeds -> re-probe -> final state reflects
    the RECOVERED outcome, not the original consent_card sighting."""
    _reset_state(monkeypatch)
    _patch_probe_plumbing(
        monkeypatch,
        page_thread_results=[
            (True, "接続マネージャーを開く", False),   # first probe: consent card
            (True, "found file: probe_TESTTOKEN.txt", False),  # re-probe: recovered
        ],
        auto_consent_result=True,
    )
    surface_calls = _patch_surface(monkeypatch, [])
    recorded = []
    # **kw で受ける。呼び出し側に引数が1つ増えるたびに壊れるスタブにしない
    # （既定値つきの追加は本番の呼び出しには後方互換で、壊れるのはここだけだった）。
    monkeypatch.setattr(B.tool_probe, "record_probe",
                         lambda ok, kind, **kw: recorded.append((ok, kind)))

    B._run_tool_probe()

    assert recorded[-1] == (True, "answer")
    assert surface_calls == []   # auto-consent alone resolved it -- no surface() needed
    assert B._CONSENT_SURFACE_OK is False   # _reset_consent_surface_episode ran


def test_probe_consent_card_auto_consent_fails_falls_to_surface(monkeypatch):
    """kind=consent_card -> auto-consent fails -> the bounded last-resort surface() is driven,
    same recovery ladder an interactive turn gets (BUG 1's core fix)."""
    _reset_state(monkeypatch)
    _patch_probe_plumbing(
        monkeypatch,
        page_thread_results=[(True, "接続マネージャーを開く", False)],
        auto_consent_result=False,
    )
    surface_calls = _patch_surface(monkeypatch, [True])
    recorded = []
    # **kw で受ける。呼び出し側に引数が1つ増えるたびに壊れるスタブにしない
    # （既定値つきの追加は本番の呼び出しには後方互換で、壊れるのはここだけだった）。
    monkeypatch.setattr(B.tool_probe, "record_probe",
                         lambda ok, kind, **kw: recorded.append((ok, kind)))

    B._run_tool_probe()

    assert len(surface_calls) == 1        # surface() WAS driven, not just recorded-and-ignored
    assert recorded[-1] == (False, "consent_card")
    assert B._CONSENT_SURFACE_OK is True   # surfaced successfully this episode


def test_probe_timeout_skips_consent_recovery(monkeypatch):
    _reset_state(monkeypatch)
    _patch_probe_plumbing(monkeypatch, [(True, "", True)])
    surface_calls = _patch_surface(monkeypatch, [])
    recorded = []
    # **kw で受ける。呼び出し側に引数が1つ増えるたびに壊れるスタブにしない
    # （既定値つきの追加は本番の呼び出しには後方互換で、壊れるのはここだけだった）。
    monkeypatch.setattr(B.tool_probe, "record_probe",
                         lambda ok, kind, **kw: recorded.append((ok, kind)))

    B._run_tool_probe()

    assert recorded[-1] == (False, "timeout")
    assert surface_calls == []


def test_probe_startup_is_transitional_and_retries_quickly(monkeypatch):
    _reset_state(monkeypatch)
    monkeypatch.setattr(B, "PAGE", None)
    monkeypatch.setattr(B, "_LAST_USER_TURN_TS", 0.0)
    monkeypatch.setattr(B, "MCP_TOOL_PROBE_SEC", 600.0)
    recorded = []
    # **kw で受ける。呼び出し側に引数が1つ増えるたびに壊れるスタブにしない
    # （既定値つきの追加は本番の呼び出しには後方互換で、壊れるのはここだけだった）。
    monkeypatch.setattr(B.tool_probe, "record_probe",
                        lambda ok, kind, detail="", ts=None, alive=None: recorded.append((ok, kind, detail)))

    retry = B._run_tool_probe()

    assert retry == 15.0
    assert recorded[-1][0:2] == (False, "starting")


def test_probe_records_checking_before_the_real_turn(monkeypatch):
    _reset_state(monkeypatch)
    _patch_probe_plumbing(monkeypatch, [(True, "found file: probe_TESTTOKEN.txt", False)])
    _patch_surface(monkeypatch, [])
    recorded = []
    # **kw で受ける。呼び出し側に引数が1つ増えるたびに壊れるスタブにしない
    # （既定値つきの追加は本番の呼び出しには後方互換で、壊れるのはここだけだった）。
    monkeypatch.setattr(B.tool_probe, "record_probe",
                        lambda ok, kind, **kw: recorded.append((ok, kind)))

    B._run_tool_probe()

    assert recorded[0] == (False, "checking")
    assert recorded[-1] == (True, "answer")


def test_a_reply_that_claims_success_without_calling_anything_is_not_a_pass(monkeypatch):
    """WHAT THE OLD PROBE COULD NOT SEE. The verdict used to be "does the reply contain the
    token", so a reply that talked about the tool convincingly was the whole evidence. Now the
    server has to have watched the call arrive, and a confident sentence on its own is an
    "error" -- which is the honest reading of a turn where nothing was called."""
    _reset_state(monkeypatch)
    _patch_probe_plumbing(
        monkeypatch,
        [(True, "list_directory を呼び出し、正常に一覧できました。", False)],
        arrived=False,
    )
    _patch_surface(monkeypatch, [])
    recorded = []
    monkeypatch.setattr(B.tool_probe, "record_probe",
                        lambda ok, kind, **kw: recorded.append((ok, kind)))

    B._run_tool_probe()

    assert recorded[-1] == (False, "error")


def test_a_refusal_is_reported_as_a_failure_and_not_as_an_unreachable_agent(monkeypatch):
    """The live incident this change came from: the agent answered at length, declining the
    probe on security grounds, and called nothing. That is a failure -- but it is a REPLY
    failure, so it must not be filed as agent_unreachable/canned_fallback, which would send
    the recovery ladder after a connector that is fine."""
    _reset_state(monkeypatch)
    refusal = ("同じ診断依頼が5回目です。判断は変わりません。発行元と目的が確認できるまで"
               "この回は実行を保留します。")
    _patch_probe_plumbing(monkeypatch, [(True, refusal, False)], arrived=False)
    _patch_surface(monkeypatch, [])
    recorded = []
    monkeypatch.setattr(B.tool_probe, "record_probe",
                        lambda ok, kind, **kw: recorded.append((ok, kind)))

    B._run_tool_probe()

    assert recorded[-1] == (False, "error")

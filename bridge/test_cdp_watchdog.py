from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

from bridge import copilot_bridge as bridge


class _CdpHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/json/version":
            body = b'{"Browser":"Edge"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def test_cdp_watchdog_health_probe_distinguishes_live_and_dead_ports():
    server = HTTPServer(("127.0.0.1", 0), _CdpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        assert bridge._cdp_healthy("http://127.0.0.1:%d" % port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert not bridge._cdp_healthy("http://127.0.0.1:%d" % port, timeout=0.1)


def test_main_starts_watchdog_before_serving_forever():
    source = bridge.Path(bridge.__file__).read_text(encoding="utf-8")
    assert "_start_cdp_watchdog(cdp)" in source
    assert "os._exit(70)" in source
    assert 'tool_probe.record_probe(False, "starting"' in source


def test_page_probe_restart_requires_consecutive_unreachable_results(monkeypatch):
    monkeypatch.setattr(bridge, "PAGE_UNREACHABLE_FAILURES", 3)
    monkeypatch.setattr(bridge, "_PAGE_UNREACHABLE_STREAK", 0)

    assert bridge._page_probe_requires_restart("agent_unreachable") is False
    assert bridge._page_probe_requires_restart("agent_unreachable") is False
    assert bridge._page_probe_requires_restart("agent_unreachable") is True


def test_page_probe_recovery_streak_resets_after_any_reachable_classification(monkeypatch):
    monkeypatch.setattr(bridge, "PAGE_UNREACHABLE_FAILURES", 2)
    monkeypatch.setattr(bridge, "_PAGE_UNREACHABLE_STREAK", 1)

    assert bridge._page_probe_requires_restart("answer") is False
    assert bridge._PAGE_UNREACHABLE_STREAK == 0
    assert bridge._page_probe_requires_restart("agent_unreachable") is False


def test_page_probe_restart_hands_control_back_to_keepalive():
    source = bridge.Path(bridge.__file__).read_text(encoding="utf-8")
    assert "_page_probe_requires_restart(kind)" in source
    assert "PAGE_EXECUTOR.submit_bounded(" in source
    assert "os._exit(71)" in source


# -- a busy startup is not a wedge -------------------------------------------------------------
#
# MEASURED 2026-09-10. `_page_main` opens the agent tab, runs proactive auto-consent and startup
# auto-resume as ONE job on the owner thread, so the 10s liveness probe cannot be serviced while
# it runs. `_PAGE_THREAD_WEDGED` began ticking ~11s in, and at PAGE_THREAD_WEDGE_LIMIT_S (120s)
# wedge_escalation_step() exited the process -- before startup had finished. The keepalive
# restarted it, _find_or_open_agent opened ANOTHER agent tab, and the same 120s ran out again:
# an hour of that left 3 pages on the bridge's CDP port, 40 msedge processes and 4.3 GB. The log
# proved the thread was working the whole time ("startup proactive auto-consent: no consent card
# handled" printed while the watchdog reported "still wedged (20s)").
#
# These drive the real escalation decision with the exiter injected, so they exercise the choice
# of limit rather than asserting that some source line exists.

def _wedge(monkeypatch, serving, wedged_for):
    """Run one escalation decision. Returns True iff it would have exited the process."""
    if serving:
        monkeypatch.setattr(bridge._PAGE_SERVING, "is_set", lambda: True)
    else:
        monkeypatch.setattr(bridge._PAGE_SERVING, "is_set", lambda: False)
    calls = []
    bridge.wedge_escalation_step(exiter=lambda code: calls.append(code),
                                 wedged_for=wedged_for)
    return calls == [70]


def test_a_thread_still_starting_is_not_handed_back_on_the_serving_limit(monkeypatch):
    """The exact loop: past the serving limit, still inside startup. Must not exit."""
    over_serving = bridge.PAGE_THREAD_WEDGE_LIMIT_S + 10
    assert over_serving < bridge.PAGE_STARTUP_WEDGE_LIMIT_S, (
        "the startup budget must be larger than the serving one or this cannot be tested")
    assert not _wedge(monkeypatch, serving=False, wedged_for=over_serving)


def test_a_startup_that_really_hangs_is_still_handed_back(monkeypatch):
    """Bounded, not exempt: a startup that never finishes must still reach the supervisor."""
    assert _wedge(monkeypatch, serving=False,
                  wedged_for=bridge.PAGE_STARTUP_WEDGE_LIMIT_S + 1)


def test_once_serving_the_shorter_limit_applies_again(monkeypatch):
    """After run_forever() a missed probe really does mean the queue is blocked."""
    assert _wedge(monkeypatch, serving=True,
                  wedged_for=bridge.PAGE_THREAD_WEDGE_LIMIT_S + 10)


def test_the_startup_budget_can_never_be_stricter_than_the_serving_one():
    """MCP_PAGE_STARTUP_LIMIT_SEC set below the serving limit would restore the loop silently."""
    assert bridge.PAGE_STARTUP_WEDGE_LIMIT_S >= bridge.PAGE_THREAD_WEDGE_LIMIT_S


def test_marking_serving_raises_the_flag_the_limit_is_chosen_by():
    """Without the flag the startup budget would apply for the life of the process, and a real
    wedge would never be handed back at all."""
    was_set = bridge._PAGE_SERVING.is_set()
    try:
        bridge._PAGE_SERVING.clear()
        bridge.mark_serving()
        assert bridge._PAGE_SERVING.is_set()
    finally:
        if not was_set:
            bridge._PAGE_SERVING.clear()


def test_the_wedge_clock_does_not_carry_startup_time_into_serving(monkeypatch):
    """THE HOLE IN THE FIRST FIX. A wider startup budget is useless if the clock is carried
    over: startup misses probes for ~120s by design, so the moment the serving limit took over
    the process was handed back at once. Measured as "still wedged (121s)" three seconds after
    the startup page was released."""
    monkeypatch.setattr(bridge, "_PAGE_THREAD_WEDGED",
                        __import__("time").time() - (bridge.PAGE_THREAD_WEDGE_LIMIT_S + 60),
                        raising=False)
    assert bridge.page_thread_wedged_for_s() > bridge.PAGE_THREAD_WEDGE_LIMIT_S, (
        "the fixture did not actually put a stale wedge on the clock")
    was_set = bridge._PAGE_SERVING.is_set()
    try:
        bridge.mark_serving()
        assert bridge.page_thread_wedged_for_s() is None, (
            "the startup wedge survived into serving; the next probe escalates immediately")
        assert bridge._PAGE_SERVING.is_set()
    finally:
        if not was_set:
            bridge._PAGE_SERVING.clear()
        bridge._PAGE_THREAD_WEDGED = None


def test_the_owner_thread_marks_serving_rather_than_setting_the_flag_by_hand():
    """Two statements that must not drift apart: the flag and the clock reset."""
    source = bridge.Path(bridge.__file__).read_text(encoding="utf-8")
    i = source.index("PAGE_EXECUTOR.run_forever()")
    assert "mark_serving()" in source[max(0, i - 400):i], (
        "the owner thread no longer calls mark_serving() before run_forever()")


# -- a declared long job is not a wedge ---------------------------------------------------------
#
# THE FAULT THAT PINNED THE BRIDGE ON THE PAGE TRANSPORT. Getting onto a socket requires a token
# capture, the capture runs on the owner thread, and the default capture sends a real Copilot
# turn -- so the thread stopped answering the 10s probe, hit the 120s wedge limit, and the
# process was handed back mid-capture. Every attempt to leave the page transport was killed by
# the watchdog. Measured 2026-09-10 across an hour of restarts: transport stayed "page" with a
# resident Copilot tab, and each cycle leaked an Edge tree.

def test_a_declared_long_job_holds_off_the_hand_back(monkeypatch):
    monkeypatch.setattr(bridge._PAGE_SERVING, "is_set", lambda: True)
    calls = []
    bridge.declare_long_job(300, "socket token capture")
    try:
        bridge.wedge_escalation_step(exiter=lambda code: calls.append(code),
                                     wedged_for=bridge.PAGE_THREAD_WEDGE_LIMIT_S + 60)
    finally:
        bridge.end_long_job()
    assert calls == [], "the capture was killed by the watchdog it was declared to"


def test_a_long_job_that_overruns_its_declaration_is_a_wedge_again(monkeypatch):
    """Declared, not exempt. A capture that never returns must still reach the supervisor."""
    monkeypatch.setattr(bridge._PAGE_SERVING, "is_set", lambda: True)
    calls = []
    bridge.declare_long_job(0.0, "socket token capture")   # already expired
    try:
        bridge.wedge_escalation_step(exiter=lambda code: calls.append(code),
                                     wedged_for=bridge.PAGE_THREAD_WEDGE_LIMIT_S + 60)
    finally:
        bridge.end_long_job()
    assert calls == [70]


def test_ending_a_long_job_restores_the_ordinary_rules(monkeypatch):
    monkeypatch.setattr(bridge._PAGE_SERVING, "is_set", lambda: True)
    bridge.declare_long_job(300, "socket token capture")
    bridge.end_long_job()
    assert bridge.long_job_remaining_s() == 0.0
    calls = []
    bridge.wedge_escalation_step(exiter=lambda code: calls.append(code),
                                 wedged_for=bridge.PAGE_THREAD_WEDGE_LIMIT_S + 60)
    assert calls == [70]


def test_the_capture_declares_itself_and_always_clears_the_declaration():
    """A declaration left standing after a failed capture would mute the watchdog for good."""
    source = bridge.Path(bridge.__file__).read_text(encoding="utf-8")
    i = source.index("route.refresh")
    window = source[max(0, i - 700):i + 700]
    assert "declare_long_job(CAPTURE_DECLARED_S" in window, (
        "the token capture no longer declares itself; the watchdog will kill it again")
    assert "finally:" in window and "end_long_job()" in window, (
        "the declaration is not cleared in a finally; a failed capture would silence the "
        "wedge watchdog for the rest of the process's life")


def test_the_capture_budget_is_never_shorter_than_the_wedge_limit():
    assert bridge.CAPTURE_DECLARED_S >= bridge.PAGE_THREAD_WEDGE_LIMIT_S


# -- a socket conversation must not hold a Copilot tab -----------------------------------------
#
# MEASURED 2026-09-10: transport=socket with has_resident_page=True and a 380 MB Copilot tab on
# the bridge's Edge, while every turn went over the socket and touched nothing on that tab. Two
# causes, both here: the tool probe's gate required a DOM composer even for a socket turn (so it
# borrowed a page every MCP_TOOL_PROBE_SEC and held it), and the only page release lived in
# startup, so nothing revisited it when a socket arrived later.

def test_the_probe_does_not_require_a_composer_when_the_turn_goes_over_a_socket():
    source = bridge.Path(bridge.__file__).read_text(encoding="utf-8")
    i = source.index("def _do_tool_probe_turn")
    body = source[i:i + 1800]
    assert "if _on_socket():" in body and "agent_loaded = True" in body, (
        "the probe gates a socket turn on a DOM composer again; it will borrow and hold a "
        "Copilot tab for the life of the process")
    assert 'COPILOT_SELECTORS["composer"]' in body, (
        "the composer check is still the right question for the PAGE transport and must stay")


def test_a_socket_driver_releases_the_resident_page():
    source = bridge.Path(bridge.__file__).read_text(encoding="utf-8")
    i = source.index("conversation is on a SOCKET")
    assert "release_resident_page(" in source[i:i + 700], (
        '"no tab needed for turns" is printed again without releasing the tab')


def test_releasing_the_page_keeps_a_blank_one_and_does_not_clear_a_socket_driver():
    """Edge exits with its last page -- closing the agent tab without leaving a blank behind
    took the whole browser down and CDP with it. And the socket DRIVER must survive: it is the
    thing that made the page releasable in the first place."""
    source = bridge.Path(bridge.__file__).read_text(encoding="utf-8")
    i = source.index("def _release_resident_page_locked")
    body = source[i:i + 1400]
    assert "CTX.new_page()" in body and 'in ("about:blank", "")' in body, (
        "the release no longer guarantees a blank page survives it")
    assert 'if not getattr(DRIVER, "IS_SOCKET", False):' in body, (
        "the release clears the socket driver it was triggered by")
    assert "except Exception" in body, "a failed release must leave the page, not raise"

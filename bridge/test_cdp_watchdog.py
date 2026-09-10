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


def test_the_owner_thread_announces_it_is_serving_before_it_blocks_forever():
    """run_forever() never returns, so the flag has to be set on the line before it."""
    source = bridge.Path(bridge.__file__).read_text(encoding="utf-8")
    i = source.index("PAGE_EXECUTOR.run_forever()")
    assert "_PAGE_SERVING.set()" in source[max(0, i - 400):i], (
        "nothing sets _PAGE_SERVING before run_forever(); the startup budget would then apply "
        "for the whole life of the process and a real wedge would never be handed back")

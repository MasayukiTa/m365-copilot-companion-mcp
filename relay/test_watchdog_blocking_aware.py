# -*- coding: utf-8 -*-
"""The watchdog decision, the declared-blocking register and the recovery step.

Incident (2026-10-14): a tab open / token capture / Playwright call held the sweep thread with
status.json frozen, the stall watchdog read that as a wedged Edge and killed it under the call,
recovery restarted the same work, and the runner eventually gave up. Pure decisions, synthetic
inputs, no browser.
"""
import pytest

from relay import blocking_calls as BC
from relay import fleet_runner as FR
from relay import relay_fleet as RF


@pytest.fixture(autouse=True)
def _clean():
    BC.clear()
    yield
    BC.clear()


NOW = 10_000.0


def _status(workers=None):
    return {"running": True, "idle": False, "workers": workers or
            [{"name": "w0", "status": "ready", "eval_busy_until": 0.0}]}


def test_legacy_call_shape_still_resets_a_plain_stall():
    should, why = FR._watchdog_should_reset(_status(), 200, now=NOW)
    assert should is True and "wedged" in why


def test_a_declared_blocking_call_is_not_a_wedge():
    blocks = [("tab_open", NOW - 20, NOW + 280)]
    should, why = FR._watchdog_should_reset(_status(), 200, now=NOW, declared_blocks=blocks)
    assert should is False and "tab_open" in why


def test_a_declaration_past_its_ceiling_no_longer_protects():
    blocks = [("tab_open", NOW - 400, NOW - 100)]
    assert FR._watchdog_should_reset(_status(), 400, now=NOW, declared_blocks=blocks)[0] is True


def test_a_capture_in_flight_waits_but_only_within_its_ceiling():
    assert FR._watchdog_should_reset(_status(), 200, now=NOW, capture_inflight=1)[0] is False
    over = FR.CAPTURE_STALL_CEILING_S + 1
    assert FR._watchdog_should_reset(_status(), over, now=NOW, capture_inflight=1)[0] is True


def test_a_freshly_started_edge_gets_a_grace_that_ends():
    assert FR._watchdog_should_reset(_status(), 200, now=NOW, edge_age_s=30)[0] is False
    assert FR._watchdog_should_reset(
        _status(), 200, now=NOW, edge_age_s=FR.EDGE_START_GRACE_S + 1)[0] is True


def test_a_browser_that_answers_cdp_is_given_longer_but_not_forever():
    assert FR._watchdog_should_reset(_status(), 200, now=NOW, cdp_responsive=True)[0] is False
    late = FR.CDP_RESPONSIVE_STALL_CEILING_S + 1
    assert FR._watchdog_should_reset(_status(), late, now=NOW, cdp_responsive=True)[0] is True


def test_a_browser_that_does_not_answer_is_reset_at_once():
    should, why = FR._watchdog_should_reset(_status(), 200, now=NOW, cdp_responsive=False)
    assert should is True and "does not answer" in why


def test_a_sweep_thread_inside_playwright_waits_unless_the_browser_is_dead():
    assert FR._watchdog_should_reset(_status(), 200, now=NOW, main_in_playwright=True)[0] is False
    assert FR._watchdog_should_reset(_status(), 200, now=NOW, main_in_playwright=True,
                                     cdp_responsive=False)[0] is True
    late = FR.CDP_RESPONSIVE_STALL_CEILING_S + 1
    assert FR._watchdog_should_reset(_status(), late, now=NOW, main_in_playwright=True)[0] is True


def test_not_running_never_resets_whatever_the_evidence():
    assert FR._watchdog_should_reset({"running": False}, 999, now=NOW,
                                     cdp_responsive=False)[0] is False


def test_main_thread_probe_reads_a_stack_without_raising():
    assert FR._main_thread_in_playwright() in (True, False)


# ── the declaration register ──────────────────────────────────────────────────────────────

def test_blocking_registers_and_always_withdraws():
    with pytest.raises(RuntimeError):
        with BC.blocking("x", 100):
            assert [e[0] for e in BC.active()] == ["x"]
            raise RuntimeError("boom")
    assert BC.active() == []


def test_an_expired_entry_stops_counting():
    BC.begin("old", 10, now=1000.0)
    BC.begin("new", 100, now=1000.0)
    assert [e[0] for e in BC.active(now=1050.0)] == ["new"]


def test_every_tab_open_declares_itself(monkeypatch):
    seen = []
    monkeypatch.setattr(RF, "_open_fresh_unguarded",
                        lambda ctx, url: seen.append([e[0] for e in BC.active()]) or "page")
    assert RF._open_fresh(object(), "u") == "page"
    assert seen == [["tab_open"]] and BC.active() == []


def test_a_synchronous_token_capture_declares_itself():
    from relay.socket_route import SocketRoute
    seen = []

    def capture(ctx, url):
        seen.append([e[0] for e in BC.active()])
        raise RuntimeError("no page")
    route = SocketRoute(capture_fn=capture, connect_fn=lambda *a, **k: None, enabled=True)
    assert route.refresh(object(), "u") is False
    assert seen == [["token_capture"]] and BC.active() == []


def test_the_stall_diagnostic_names_the_declared_blocking_call(tmp_path):
    with BC.blocking("tab_open", 300):
        FR._write_stall_diagnostic(str(tmp_path), {"updated": 1.0, "workers": []}, 200.0, False,
                                   "waiting")
    body = (tmp_path / "stall_diagnostics.log").read_text(encoding="utf-8")
    assert "BLOCKING CALL IN PROGRESS: tab_open" in body


# ── the recovery step ─────────────────────────────────────────────────────────────────────

def test_attempts_count_up_to_the_limit_without_waiting():
    a = 0
    for expect in (1, 2, 3):
        a, wait, give_up = FR._recovery_step(a, False, 3)
        assert (a, wait, give_up) == (expect, 0.0, False)


def test_past_the_limit_it_backs_off_and_never_gives_up():
    a, waits = 3, []
    for _ in range(8):
        a, wait, give_up = FR._recovery_step(a, False, 3, base_s=30, cap_s=600)
        assert give_up is False
        waits.append(wait)
    assert waits[:4] == [30, 60, 120, 240] and max(waits) == 600


def test_progress_resets_the_count():
    assert FR._recovery_step(9, True, 3) == (1, 0.0, False)


def test_recovery_switched_off_still_means_off():
    assert FR._recovery_step(0, False, 0)[2] is True


# ── the resume report is off the sweep's hot path ─────────────────────────────────────────

def test_the_auto_resume_block_does_not_read_files_on_the_sweep_thread(monkeypatch):
    import threading
    import time
    from relay import fleet_resume
    callers = []

    def slow_report(state_dir, now=None):
        callers.append(threading.current_thread().name)
        return {"setting": "off", "last_decision": None, "pending_snapshots": 0}
    monkeypatch.setattr(fleet_resume, "auto_resume_report", slow_report)
    FR._AUTO_RESUME_CACHE.update(value={}, at=0.0, running=False)
    first = FR._auto_resume_block()
    assert first == {}                                   # nothing cached yet: no blocking read
    for _ in range(100):
        if FR._AUTO_RESUME_CACHE["at"]:
            break
        time.sleep(0.05)
    assert callers == ["auto-resume-report"]            # the read ran on its own thread
    assert FR._auto_resume_block()["auto_resume"]["pending_snapshots"] == 0
    assert callers == ["auto-resume-report"]            # fresh cache: no second read

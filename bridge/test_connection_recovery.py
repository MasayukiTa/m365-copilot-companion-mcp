"""The bridge could not survive losing its connection to the browser, and could not say so.

MEASURED 2026-09-03. The playwright driver behind sync_playwright died. Edge itself stayed up
and kept answering /json/version, so the CDP watchdog -- whose whole purpose is repairing a
half-dead stack -- checked the healthy half every ten seconds for 22.5 hours and reported all
clear. Meanwhile:

  * CTX was not None, it was a STALE HANDLE, so ensure_page_alive sailed past its `CTX is None`
    guard and raised the same exception on every call forever.
  * connect_over_cdp ran exactly once, in _page_main, with the playwright object as a local.
    There was no reconnect branch. Not a broken one -- an absent one.
  * The failure logged the reason, with a traceback, through a logger this module configures
    nothing for. It landed in bridge.log.err, split off from the operational log, read by nobody.
  * The tool probe reported "no page available to probe with", which is also what it says when
    the bridge is merely idle. The dot was red for 22.5 hours and was noticed by a human looking
    at a screenshot.

These tests RUN the code rather than reading it. The module imports in about 8 seconds and
starts no browser at import, and every function here takes its dependencies as arguments, so
the escalation path -- the one that ends in os._exit -- can be exercised without ending pytest.
A branch that cannot be run by a test is the branch that gets written once and never seen again,
which is how the missing reconnect survived in the first place.
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

B = pytest.importorskip("bridge.copilot_bridge")


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test starts with the connection not known to be dead, and leaves it that way."""
    B._CONNECTION_DEAD = None
    yield
    B._CONNECTION_DEAD = None


# -- telling an infrastructure failure from a content one -------------------------------------

def test_the_message_that_actually_happened_is_recognised():
    """Verbatim from bridge.log.err, 2026-09-03."""
    exc = Exception("BrowserContext.new_page: Connection closed while reading from the driver")
    assert B.connection_is_dead(exc)


@pytest.mark.parametrize("msg", [
    "Browser has been closed",
    "Target page, context or browser has been closed",
])
def test_the_other_wordings_for_a_gone_connection_are_recognised(msg):
    assert B.connection_is_dead(Exception(msg))


@pytest.mark.parametrize("msg", [
    "Timeout 30000ms exceeded while waiting for selector \"textarea\"",
    "net::ERR_NAME_NOT_RESOLVED at https://m365.cloud.microsoft/",
    "Element is not visible",
    "Navigation failed because page crashed",
])
def test_an_ordinary_page_failure_is_not_treated_as_a_dead_connection(msg):
    """THE DANGEROUS DIRECTION. A false positive here restarts the whole bridge because one
    selector timed out or one site misbehaved, so this list is deliberately narrow: every
    accepted marker names the pipe to the browser, and none of them can be produced by a page."""
    assert not B.connection_is_dead(Exception(msg))


def test_a_dead_connection_becomes_visible_to_code_that_did_not_raise():
    """The whole point: the fact has to leave the call that discovered it."""
    assert B.connection_dead_for_s() is None
    B.note_connection_dead(Exception("Connection closed while reading from the driver"))
    age = B.connection_dead_for_s()
    assert age is not None and age < 5.0


# -- rebuilding the connection ----------------------------------------------------------------

class _FakeCtx:
    pass


class _FakeBrowser:
    def __init__(self, contexts):
        self.contexts = contexts


class _FakeChromium:
    def __init__(self, browser=None, boom=None):
        self._browser, self._boom, self.calls = browser, boom, []

    def connect_over_cdp(self, cdp):
        self.calls.append(cdp)
        if self._boom:
            raise self._boom
        return self._browser


class _FakePW:
    def __init__(self, chromium):
        self.chromium = chromium


def test_reconnect_rebinds_the_context_and_drops_the_dead_page_and_driver(monkeypatch):
    ctx = _FakeCtx()
    chromium = _FakeChromium(browser=_FakeBrowser([ctx]))
    monkeypatch.setattr(B, "PW", _FakePW(chromium))
    monkeypatch.setattr(B, "CTX", object())
    monkeypatch.setattr(B, "PAGE", object())
    monkeypatch.setattr(B, "DRIVER", object())
    B.note_connection_dead(Exception("Connection closed while reading from the driver"))

    assert B.reconnect_browser("http://localhost:9223") is True
    assert B.CTX is ctx
    # Both belonged to the connection that died; keeping either hands the next caller a handle
    # onto a browser this process can no longer reach.
    assert B.PAGE is None and B.DRIVER is None
    assert B.connection_dead_for_s() is None
    assert chromium.calls == ["http://localhost:9223"]


def test_reconnect_reports_failure_and_leaves_the_dead_mark_standing(monkeypatch):
    """A failed rebuild must NOT look like a recovery, or the watchdog stops escalating."""
    chromium = _FakeChromium(boom=Exception("connect ECONNREFUSED"))
    monkeypatch.setattr(B, "PW", _FakePW(chromium))
    B.note_connection_dead(Exception("Connection closed while reading from the driver"))

    assert B.reconnect_browser("http://localhost:9223") is False
    assert B.connection_dead_for_s() is not None


def test_reconnect_without_playwright_fails_instead_of_raising(monkeypatch):
    """If the driver process died, the playwright object that owned it cannot open anything
    either. Returning False hands the decision to the caller; raising here would kill the
    watchdog thread and leave nothing to escalate."""
    monkeypatch.setattr(B, "PW", None)
    assert B.reconnect_browser("http://localhost:9223") is False


# -- escalation, which is the part that could never be run before -----------------------------

def test_a_healthy_connection_does_no_work_and_resets_the_count():
    calls = []
    assert B.connection_recovery_step("cdp", 2, reconnect=lambda: calls.append(1),
                                      exiter=lambda c: pytest.fail("exited on a healthy stack"),
                                      dead_for=None) == 0
    assert calls == []


def test_a_connection_dead_for_less_than_the_grace_period_is_left_alone():
    """A turn in flight when the driver dies reports it and recovers on its own retry; rebuilding
    underneath that request would break one that was about to succeed."""
    calls = []
    assert B.connection_recovery_step("cdp", 0, reconnect=lambda: calls.append(1),
                                      exiter=lambda c: pytest.fail("exited during grace"),
                                      dead_for=B.CONNECTION_DEAD_GRACE_S - 0.5) == 0
    assert calls == []


def test_a_successful_rebuild_clears_the_attempt_count():
    assert B.connection_recovery_step("cdp", 2, reconnect=lambda: True,
                                      exiter=lambda c: pytest.fail("exited after a rebuild"),
                                      dead_for=B.CONNECTION_DEAD_GRACE_S + 1) == 0


def test_repeated_failure_hands_the_process_to_the_supervisor():
    """THE ESCALATION. Exhausting the budget must exit(70) so start_bridge.ps1 rebuilds Edge and
    the bridge together -- not stop trying and keep serving a connection that cannot work."""
    exits = []
    attempts = 0
    for _ in range(B.CONNECTION_RECONNECT_TRIES):
        attempts = B.connection_recovery_step(
            "cdp", attempts, reconnect=lambda: False, exiter=exits.append,
            dead_for=B.CONNECTION_DEAD_GRACE_S + 1)
    assert exits == [70], "the bridge stayed up with a connection it cannot use"


def test_a_reconnect_that_raises_counts_as_a_failed_attempt():
    """submit_bounded raises when the page-owner thread has wedged. That is a failure to
    recover, not a reason for the watchdog to die."""
    exits = []
    attempts = 0

    def boom():
        raise TimeoutError("page executor did not complete its liveness probe")

    for _ in range(B.CONNECTION_RECONNECT_TRIES):
        attempts = B.connection_recovery_step("cdp", attempts, reconnect=boom,
                                              exiter=exits.append,
                                              dead_for=B.CONNECTION_DEAD_GRACE_S + 1)
    assert exits == [70]


# -- the logger having somewhere to write ------------------------------------------------------

def test_logging_configuration_gives_the_module_logger_a_destination():
    """THE DEFECT THIS FILE OPENS WITH. 93 log calls, including the traceback that named the
    real cause, went nowhere because nothing ever added a handler."""
    B.logger.handlers = [h for h in B.logger.handlers
                         if not getattr(h, "_test_marker", False)]
    B.logger._bridge_log_configured = False
    B._configure_logging()
    assert B.logger.handlers, "the logger still has no handler"
    assert B.logger.propagate is False, (
        "propagate left on means every WARNING is emitted twice -- once here and once by "
        "logging.lastResort into stderr, which is the split that hid the outage")


def test_an_info_line_actually_reaches_a_handler():
    """Not 'a handler is attached' -- that a record written at INFO arrives. The old failure was
    exactly that INFO was dropped below the root logger's default level."""
    B.logger._bridge_log_configured = False
    B._configure_logging()
    seen = []

    class _Catch(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    catcher = _Catch()
    B.logger.addHandler(catcher)
    try:
        B.logger.info("agent page had closed -- reopened it")
    finally:
        B.logger.removeHandler(catcher)
    assert seen == ["agent page had closed -- reopened it"]


def test_configuring_twice_does_not_double_every_line():
    B.logger._bridge_log_configured = False
    B._configure_logging()
    first = len(B.logger.handlers)
    B._configure_logging()
    assert len(B.logger.handlers) == first


# -- asking our own end, instead of waiting for a request to discover it ----------------------

def test_a_dead_connection_is_found_without_anyone_needing_a_page(monkeypatch):
    """MEASURED WHILE BUILDING THIS. The driver was killed and the bridge sat 145 seconds with
    transport=none and no recovery, because note_connection_dead only fires from a call that
    wanted a page and nothing wanted one. Detection cannot depend on demand."""
    monkeypatch.setattr(B, "CTX", object())
    monkeypatch.setattr(B.PAGE_EXECUTOR, "submit_bounded",
                        lambda t, fn, *a, **k: fn(*a, **k))

    def dead():
        raise Exception("BrowserContext.cookies: Connection closed while reading from the driver")

    assert B.probe_connection(touch=dead) is False
    assert B.connection_dead_for_s() is not None


def test_a_live_connection_probes_true_and_marks_nothing(monkeypatch):
    monkeypatch.setattr(B, "CTX", object())
    monkeypatch.setattr(B.PAGE_EXECUTOR, "submit_bounded",
                        lambda t, fn, *a, **k: fn(*a, **k))
    assert B.probe_connection(touch=lambda: True) is True
    assert B.connection_dead_for_s() is None


def test_a_wedged_page_thread_is_not_reported_as_a_dead_connection(monkeypatch):
    """THE DANGEROUS DIRECTION AGAIN. A timeout means the owner thread stopped servicing its
    queue; rebuilding the browser connection would not repair that, and calling it death would
    restart the process for the wrong reason."""
    monkeypatch.setattr(B, "CTX", object())

    def wedged(t, fn, *a, **k):
        raise TimeoutError("page executor did not complete its liveness probe")

    monkeypatch.setattr(B.PAGE_EXECUTOR, "submit_bounded", wedged)
    assert B.probe_connection(touch=lambda: True) is None
    assert B.connection_dead_for_s() is None


def test_before_startup_there_is_nothing_to_probe(monkeypatch):
    monkeypatch.setattr(B, "CTX", None)
    assert B.probe_connection() is None


# -- the owner thread going silent, which is how the real failure actually arrives ------------

@pytest.fixture(autouse=True)
def _clean_wedge():
    B._PAGE_THREAD_WEDGED = None
    yield
    B._PAGE_THREAD_WEDGED = None


def _timeout_probe(monkeypatch):
    def wedged(t, fn, *a, **k):
        raise TimeoutError("page executor did not complete its liveness probe")
    monkeypatch.setattr(B, "CTX", object())
    monkeypatch.setattr(B.PAGE_EXECUTOR, "submit_bounded", wedged)


def test_a_timing_out_probe_starts_the_wedge_clock(monkeypatch):
    """MEASURED. Killing the driver leaves the owner thread blocked inside it, so the probe never
    RUNS and the failure arrives as a timeout -- not as a connection error. Treating that as
    'do not know' produced one warning every 20 seconds for four and a half minutes and no
    recovery at all."""
    _timeout_probe(monkeypatch)
    assert B.page_thread_wedged_for_s() is None
    assert B.probe_connection(touch=lambda: True) is None
    assert B.page_thread_wedged_for_s() is not None


def test_a_probe_that_completes_clears_the_wedge_clock(monkeypatch):
    B._PAGE_THREAD_WEDGED = 1.0
    monkeypatch.setattr(B, "CTX", object())
    monkeypatch.setattr(B.PAGE_EXECUTOR, "submit_bounded", lambda t, fn, *a, **k: fn(*a, **k))
    assert B.probe_connection(touch=lambda: True) is True
    assert B.page_thread_wedged_for_s() is None


def test_a_brief_wedge_is_tolerated():
    """A slow real turn holds the queue legitimately."""
    assert B.wedge_escalation_step(exiter=lambda c: pytest.fail("exited on a slow turn"),
                                   wedged_for=B.PAGE_THREAD_WEDGE_LIMIT_S - 1) is False


def test_a_thread_that_stops_answering_hands_the_process_back():
    exits = []
    assert B.wedge_escalation_step(exiter=exits.append,
                                   wedged_for=B.PAGE_THREAD_WEDGE_LIMIT_S + 1) is True
    assert exits == [70], "the bridge kept serving with a thread that answers nothing"


def test_no_wedge_means_no_escalation():
    assert B.wedge_escalation_step(exiter=lambda c: pytest.fail("exited for nothing"),
                                   wedged_for=None) is False


# -- busy is not wedged ---------------------------------------------------------------------

class _FakeExecutor:
    """Only what probe_connection reads off the executor."""

    def __init__(self, idle):
        self._idle = idle

    def idle_for_s(self):
        return self._idle

    def submit_bounded(self, timeout, fn, *args, **kwargs):
        raise TimeoutError("page executor did not complete its liveness probe")


def _probe_with(monkeypatch, idle, timeout_s=10.0):
    monkeypatch.setattr(B, "CTX", object())
    monkeypatch.setattr(B, "PAGE_EXECUTOR", _FakeExecutor(idle))
    monkeypatch.setattr(B, "_PAGE_THREAD_WEDGED", None, raising=False)
    got = B.probe_connection(timeout_s=timeout_s, touch=lambda: True)
    return got, B._PAGE_THREAD_WEDGED


def test_a_thread_that_finished_a_job_while_we_waited_is_not_called_wedged(monkeypatch):
    """THE FALSE ALARM. submit_bounded queues the probe and waits, so a thread part-way
    through a real turn never reaches it inside the timeout. That was logged as "not
    servicing its queue" once per probe cycle -- roughly every ten minutes, on cycles that
    then completed normally. A line that cries wedge every cycle is how a real wedge goes
    unread."""
    got, wedged = _probe_with(monkeypatch, idle=3.0)
    assert got is None, "a probe that could not run still answers 'do not know'"
    assert wedged is None, "busy was recorded as wedged"


def test_a_thread_that_finished_nothing_across_the_whole_wait_is_wedged(monkeypatch):
    """The property the escalation actually needs, and the one that must survive."""
    got, wedged = _probe_with(monkeypatch, idle=99.0)
    assert got is None
    assert wedged is not None, "a genuinely wedged thread was excused as busy"


def test_a_thread_that_has_never_finished_anything_is_wedged(monkeypatch):
    """None is no evidence, and no evidence must not read as permission -- a thread that
    wedged on its first job has no completion to show."""
    got, wedged = _probe_with(monkeypatch, idle=None)
    assert wedged is not None


def test_the_boundary_belongs_to_the_wedge(monkeypatch):
    """Exactly at the timeout, nothing finished DURING the wait."""
    _, wedged = _probe_with(monkeypatch, idle=10.0, timeout_s=10.0)
    assert wedged is not None


def test_the_executor_stamps_completion_after_the_job_not_before():
    """A stamp taken on pickup answers 'did it start anything', which a thread wedged inside
    a job answers yes to forever."""
    import threading
    ex = B.PageExecutor()
    assert ex.idle_for_s() is None, "no evidence before anything has run"
    started = threading.Event()
    ex.start(lambda: (started.set(), ex.run_forever()))
    started.wait(timeout=5)
    ex.submit(lambda: 1)
    idle = ex.idle_for_s()
    assert idle is not None and idle < 5.0

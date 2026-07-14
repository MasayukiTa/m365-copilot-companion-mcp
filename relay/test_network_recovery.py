"""Hermetic network-switch recovery tests; no browser or external network."""
import pytest

from relay.copilot_autopilot_relay import (
    COPILOT_SELECTORS,
    CopilotWebDriver,
    NetworkUnavailable,
    _is_network_failure,
    _page_network_available,
)
from relay.refuter import RefuterSession


class _Locator:
    def __init__(self, count=1):
        self._count = count
        self.first = self

    def count(self):
        return self._count


class _Page:
    def __init__(self, online=True, url="https://m365.cloud.microsoft/chat/agent/x"):
        self.online = online
        self.url = url
        self.closed = False

    def is_closed(self):
        return self.closed

    def locator(self, selector):
        assert selector == COPILOT_SELECTORS["composer"]
        return _Locator()

    def evaluate(self, script):
        assert "navigator.onLine" in script
        return self.online

    def close(self):
        self.closed = True


def test_browser_network_probe_requires_explicit_offline_signal():
    assert _page_network_available(_Page(online=True)) is True
    assert _page_network_available(_Page(online=False)) is False
    assert _page_network_available(_Page(url="edge-error://edgewebdata/")) is False
    assert _is_network_failure(RuntimeError("page.goto: net::ERR_NETWORK_CHANGED")) is True


def test_send_fails_fast_before_touching_offline_composer():
    driver = CopilotWebDriver(_Page(online=False))
    with pytest.raises(NetworkUnavailable):
        driver.send("must not be typed")


def test_refuter_offline_poll_reopens_same_review_with_fresh_timeout(monkeypatch):
    page = _Page(online=False)
    session = RefuterSession(object(), "https://m365.cloud.microsoft/chat/agent/x", "goal",
                             "finding", timeout_s=600)
    session.page = page
    session.drv = object()
    session._pending_open = False
    session._t_send = 1.0
    monkeypatch.setattr("time.time", lambda: 1000.0)

    assert session.poll() is None
    assert page.closed is True
    assert session._pending_open is True
    assert session.drv is None
    assert session._network_reopens == 1
    assert session._t_send == 1000.0


def test_refuter_network_reopen_budget_is_bounded():
    session = RefuterSession(object(), "url", "goal", "finding", max_network_reopens=1)
    session._schedule_network_reopen("first")
    assert session._done is None
    session._schedule_network_reopen("second")
    assert session._done == ("UNCLEAR", "")

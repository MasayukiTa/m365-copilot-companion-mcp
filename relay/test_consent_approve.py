# -*- coding: utf-8 -*-
"""Work IQ consent cards on the connector-less route: pressed only when they are known, recorded
always, never by sending a turn. Everything here uses fakes: no browser, no network.

What the fakes stand for is an ASSUMPTION about the real card page (heading = tool name, one Allow
button per card, a pressed card appends the next one). The live check that confirms or corrects it
is described in 同意カード_実機検証手順_20261020.md.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import consent_approve as CA          # noqa: E402
from relay import edge_reconnect as ER           # noqa: E402

AGENT = "https://m365.cloud.microsoft/chat/agent/AGENT-1"


# ---- a page that behaves like the card transcript --------------------------------------------

class _Button:
    def __init__(self, page, idx):
        self.page, self.idx = page, idx

    def evaluate(self, _js):
        return self.page.cards[self.idx]["heading"]

    def click(self):
        self.page.pressed.append(self.page.cards[self.idx]["heading"])
        if self.page.queue:
            self.page.cards.append(self.page.queue.pop(0))


class _Loc:
    def __init__(self, page, label):
        self.page, self.label = page, label

    def locator(self, _sel):
        return self

    def count(self):
        return len(self.page.cards) if self.label == "許可" else 0

    @property
    def last(self):
        return _Button(self.page, len(self.page.cards) - 1)


class FakePage:
    def __init__(self, cards, queue=(), url=None):
        self.cards = [{"heading": h} for h in cards]
        self.queue = [{"heading": h} for h in queue]
        self.pressed = []
        self.url = url if url is not None else AGENT + "/conversation/C-1"

    def locator(self, sel):
        return _Loc(self, "許可" if "許可" in sel else "Allow")

    def wait_for_timeout(self, _ms):
        pass


def _run(page, conv="C-1"):
    return CA.approve_in_page(page, agent_url=AGENT, conversation=conv)


# ---- the four required cases -----------------------------------------------------------------

def test_a_known_tool_card_is_pressed_once():
    page = FakePage(["Mail"])
    rows = _run(page)
    assert page.pressed == ["Mail"]
    assert [r["event"] for r in rows] == ["pressed"]
    assert rows[0]["tool"] == "Mail" and rows[0]["conversation"] == "C-1"


def test_an_unknown_tool_card_is_not_pressed_and_is_recorded():
    page = FakePage(["Slack Messenger"])
    rows = _run(page)
    assert page.pressed == []
    assert rows == [{"event": "unknown_card", "tool": "Slack Messenger", "conversation": "C-1"}]


def test_a_card_in_another_conversation_is_not_pressed():
    for url in ("https://m365.cloud.microsoft/chat/agent/OTHER/conversation/C-1",
                AGENT + "/conversation/C-2", "", "about:blank"):
        page = FakePage(["Mail"], url=url)
        rows = _run(page)
        assert page.pressed == [], url
        assert rows[0]["event"] == "wrong_conversation", url


def test_no_card_means_nothing_is_pressed():
    page = FakePage([])
    rows = _run(page)
    assert page.pressed == []
    assert [r["event"] for r in rows] == ["no_card"]


# ---- the chain, the names -------------------------------------------------------------------

def test_the_whole_known_chain_is_pressed_and_an_unknown_card_in_it_stops_it():
    names = list(CA.WORK_IQ_TOOLS)
    page = FakePage([names[0]], queue=names[1:])
    rows = _run(page)
    assert page.pressed == names
    assert all(r["event"] == "pressed" for r in rows)
    page = FakePage(["User", ], queue=["Copilot", "Zapier", "Mail"])
    rows = _run(page)
    assert page.pressed == ["User", "Copilot"]
    assert rows[-1] == {"event": "unknown_card", "tool": "Zapier", "conversation": "C-1"}


@pytest.mark.parametrize("heading,expected", [
    ("Mail", ("known", "Mail")),
    ("  work iq   MAIL ", ("known", "Mail")),
    ("Microsoft 365 Copilot", ("known", "Copilot")),
    ("Work IQ SharePoint tools", ("known", "SharePoint")),
    ("OneDrive", ("known", "OneDrive")),
    ("Mail Merge Pro", ("unknown", "Mail Merge Pro")),
    ("Teams and Calendar", ("unknown", "Teams and Calendar")),
    ("", ("unknown", "")),
    ("Outlook", ("unknown", "Outlook")),
])
def test_only_a_heading_that_is_exactly_one_known_tool_is_known(heading, expected):
    assert CA.classify_heading(heading) == expected


def test_a_long_heading_is_recorded_cut():
    kind, shown = CA.classify_heading("x" * 500)
    assert kind == "unknown" and len(shown) == CA.HEADING_CAP


# ---- the extended chain is the old chain when no gate is given -------------------------------

def test_the_original_chain_is_untouched():
    page = FakePage(["Anything at all"], queue=["Another"])
    assert ER._click_consent_chain(page) is True
    assert page.pressed == ["Anything at all", "Another"]       # no scope: the old behaviour


# ---- starting the helper ---------------------------------------------------------------------

class _Proc:
    returncode = 0

    def __init__(self, out=b'{"rows": []}'):
        self.out = out

    def communicate(self, timeout=None):
        return self.out, b""


def _worker(bridged=True, ids=None):
    def boom(*a, **k):
        raise AssertionError("the approval path must never send a turn")

    drv = SimpleNamespace(send=boom, ask=boom, _run_tool=(lambda *a: None) if bridged else None,
                          conversation_ids=lambda: ids or {"client": "C-1", "server": "S-1",
                                                           "session": ""})
    return SimpleNamespace(name="w1", _agent_url=AGENT, drv=drv)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(CA, "LOG_PATH", str(tmp_path / "consent_approvals.jsonl"))
    for k in CA._counts:
        monkeypatch.setitem(CA._counts, k, 0)
    monkeypatch.setattr(CA, "_inflight", {})
    monkeypatch.setattr(CA, "_last", {})
    monkeypatch.setattr(CA, "mode", lambda path=None: "on")
    return tmp_path


CARD = "the turn completed but carried no text (a card the tab can show?) -- frames: Progress"


def _wait_idle():
    end = time.time() + 5
    while CA._inflight and time.time() < end:
        time.sleep(0.01)
    assert not CA._inflight


def test_a_card_turn_on_a_connectorless_worker_starts_one_helper_and_sends_nothing(fresh):
    started = []

    def popen(argv, **kw):
        started.append((argv, kw["env"]))
        return _Proc(json.dumps({"rows": [{"event": "pressed", "tool": "Mail",
                                           "conversation": "S-1", "chain_grew": True}]}).encode())

    w = _worker()
    assert CA.on_socket_failure(w, CARD, popen=popen) is True
    _wait_idle()
    assert len(started) == 1
    argv, env = started[0]
    assert argv[-3:] == ["-m", "relay.consent_approve", "--worker"]
    assert env["MCP_CONSENT_WORKER_AGENT"] == AGENT
    assert env["MCP_CONSENT_WORKER_CONVERSATIONS"] == "S-1,C-1"
    st = CA.status_block()
    assert st["pressed"] == 1 and st["launched"] == 1 and st["last"]["tool"] == "Mail"
    row = json.loads(io.open(CA.LOG_PATH, encoding="utf-8").read().splitlines()[-1])
    assert row["event"] == "pressed" and row["tool"] == "Mail" and row["conversation"] == "S-1"
    assert "text" not in row and "content" not in row


def test_one_helper_per_conversation_at_a_time(fresh):
    gate = []

    class _Slow(_Proc):
        def communicate(self, timeout=None):
            while not gate:
                time.sleep(0.01)
            return super().communicate()

    n = []
    w = _worker()
    assert CA.on_socket_failure(w, CARD, popen=lambda a, **k: n.append(1) or _Slow()) is True
    assert CA.on_socket_failure(w, CARD, popen=lambda a, **k: n.append(1) or _Slow()) is False
    gate.append(1)
    _wait_idle()
    assert len(n) == 1


def test_nothing_starts_for_the_connector_route_for_off_or_for_a_real_failure(fresh, monkeypatch):
    def popen(*a, **k):
        raise AssertionError("no helper may start here")

    assert CA.on_socket_failure(_worker(bridged=False), CARD, popen=popen) is False
    assert CA.on_socket_failure(_worker(), "ConnectionClosed: socket reset", popen=popen) is False
    assert CA.on_socket_failure(_worker(), "", popen=popen) is False
    monkeypatch.setattr(CA, "mode", lambda path=None: "off")
    assert CA.on_socket_failure(_worker(), CARD, popen=popen) is False
    assert CA.status_block()["launched"] == 0


def test_the_connector_route_is_identical_with_the_setting_on_or_off(fresh, monkeypatch):
    """The only thing the fleet does is call on_socket_failure; for a connector worker it must
    leave the worker untouched whichever way the setting is."""
    seen = []
    for m in ("on", "off"):
        monkeypatch.setattr(CA, "mode", lambda path=None, m=m: m)
        w = _worker(bridged=False)
        before = dict(vars(w.drv))
        assert CA.on_socket_failure(w, CARD, popen=lambda *a, **k: 1 / 0) is False
        seen.append(dict(vars(w.drv)) == before)
    assert seen == [True, True]


def test_the_module_cannot_send_a_turn():
    body = io.open(os.path.join(REPO, "relay", "consent_approve.py"), encoding="utf-8").read()
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith(("#", '"""')))
    for forbidden in (".send(", ".ask(", "chathub", "CopilotSocketDriver", "driver_for"):
        assert forbidden not in code, forbidden


def test_a_helper_failure_is_recorded_not_raised(fresh):
    class _Bad(_Proc):
        returncode = 1

    assert CA.on_socket_failure(_worker(), CARD, popen=lambda a, **k: _Bad()) is True
    _wait_idle()
    assert CA.status_block()["error"] == 1


def test_the_record_is_bounded_and_failure_safe(fresh, monkeypatch):
    monkeypatch.setattr(CA, "MAX_BYTES", 300)
    for i in range(20):
        CA.record("pressed", tool="Mail", conversation="C-%d" % i)
    assert os.path.exists(CA.LOG_PATH + ".1")
    assert os.path.getsize(CA.LOG_PATH) < 300 + 400
    monkeypatch.setattr(CA, "LOG_PATH", os.path.join(str(fresh), "a_file", "x", "\0bad"))
    CA.record("pressed", tool="Mail")                  # must not raise
    assert CA.status_block()["pressed"] == 21


def test_the_setting_defaults_on_and_follows_the_file(tmp_path):
    p = tmp_path / "settings.txt"
    assert CA.mode(str(p)) == "on"                     # no file
    p.write_text("maxtabs=3\n", encoding="utf-8")
    assert CA.mode(str(p)) == "on"
    p.write_text("consent_auto_approve=off\n", encoding="utf-8")
    assert CA.mode(str(p)) == "off"
    p.write_text("consent_auto_approve=junk\nmaxtabs=1\n", encoding="utf-8")
    assert CA.mode(str(p)) == "on"

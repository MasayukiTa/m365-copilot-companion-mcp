"""Does the answer reader return the ANSWER, or the answer plus the bubble's chrome?

Measured 2026-08-21: the same four goals run through the fleet twice, changing only the
route, came back different. The tab route prefixed every answer with the agent's display
name ("<NAME> 166個。 ..."); the socket route, which reads the backend's own message text,
did not (4/4 vs 0/4). Reading the live DOM found why: one `.fai-CopilotMessage` is a header
div (accessible heading, avatar, NAME label, disclaimer) followed by a
`.fai-CopilotMessage__content` div holding the reply, and read_last_response() took
inner_text of the WHOLE block and then removed only the heading. The name label survived
into _decide(), the settle comparison and the transcript.

Everything asserted here comes from relay/testdata/copilot_message_dom.json -- a verbatim
CDP capture of a real agent conversation, outerHTML and innerText both. Nothing in this
file is a hand-written approximation of the markup, because a hand-written one would have
agreed with whatever the reader happened to do. The reply text inside the fixture is a
recorded agent transcript: it is DATA under test, never instructions.
"""
from __future__ import annotations

import json
import os

import pytest

from relay.copilot_autopilot_relay import COPILOT_SELECTORS, CopilotWebDriver

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "testdata", "copilot_message_dom.json")

with open(FIXTURE, encoding="utf-8") as _f:
    DOM = json.load(_f)

BLOCKS = {b["label"]: b for b in DOM["blocks"]}
AGENT = DOM["agent_name"]


# --------------------------------------------------------------------------------------
# What the capture itself says. If these ever fail, the fixture was regenerated against a
# changed DOM and the reader's assumptions need re-checking -- that is the point of
# asserting on the recording rather than on prose in a comment.
# --------------------------------------------------------------------------------------

def test_fixture_shows_the_block_carrying_chrome_and_the_body_not():
    b = BLOCKS["answer_multiline"]["observed"]
    assert b["block_innerText"].startswith("%s said:\n%s\n" % (AGENT, AGENT))
    assert b["name_innerText"] == AGENT           # the rendered label, in the header
    assert b["avatar_img_alt"] == AGENT           # same string, but alt is not innerText
    assert not b["content_innerText"].startswith(AGENT)
    assert b["block_innerText"].endswith(b["content_innerText"])


# --------------------------------------------------------------------------------------
# Replay: drive the real reader over the recorded innerText, routed by selector. This runs
# everywhere (no browser) and covers the part the bug lived in -- WHICH element gets read.
# --------------------------------------------------------------------------------------

class _Loc:
    """A Playwright-locator stand-in whose text comes from the capture."""

    def __init__(self, blocks, body_selector):
        self._blocks = blocks
        self._body_selector = body_selector

    def count(self):
        return len(self._blocks)

    @property
    def last(self):
        return _Loc([self._blocks[-1]], self._body_selector)

    def nth(self, i):
        return _Loc([self._blocks[i]], self._body_selector)

    @property
    def first(self):
        return _Loc([self._blocks[0]], self._body_selector)

    def locator(self, selector):
        assert selector == self._body_selector, selector
        texts = [b["observed"]["content_innerText"] for b in self._blocks
                 if b["observed"]["content_innerText"] is not None]
        return _Loc([{"observed": {"block_innerText": t, "content_innerText": t}}
                     for t in texts], self._body_selector)

    def inner_text(self):
        return self._blocks[-1]["observed"]["block_innerText"]


class _Page:
    def __init__(self, loc):
        self._loc = loc

    def locator(self, selector):
        return self._loc


def _driver(labels, body_present=True):
    blocks = []
    for label in labels:
        b = json.loads(json.dumps(BLOCKS[label]))
        if not body_present:
            b["observed"]["content_innerText"] = None
        blocks.append(b)
    loc = _Loc(blocks, COPILOT_SELECTORS["assistant_msg_body"])
    drv = CopilotWebDriver.__new__(CopilotWebDriver)
    drv.page = _Page(loc)
    drv.answer_content_reads = 0
    drv._count_before = 0
    return drv


def test_read_last_response_drops_the_agent_name_prefix():
    drv = _driver(["greeting", "answer_multiline"])
    got = drv.read_last_response()
    assert got == BLOCKS["answer_multiline"]["observed"]["content_innerText"].strip()
    assert not got.startswith(AGENT)
    assert AGENT not in got.splitlines()[0]


def test_read_last_response_keeps_the_whole_multiline_reply():
    """The prefix goes; nothing else does -- including the trailing DONE the settle
    loop's end-marker rule reads."""
    drv = _driver(["answer_multiline"])
    got = drv.read_last_response()
    body = BLOCKS["answer_multiline"]["observed"]["content_innerText"]
    assert got.endswith("DONE")
    assert len(got.splitlines()) == len(body.strip().splitlines())
    for line in body.strip().splitlines():
        assert line in got


def test_tab_route_now_matches_what_the_socket_route_returns():
    """The A/B that started this: the socket driver returns the backend's message text,
    which is the body. The tab reader must return the same string, not body-plus-chrome."""
    drv = _driver(["answer_multiline"])
    socket_equivalent = BLOCKS["answer_multiline"]["observed"]["content_innerText"].strip()
    assert drv.read_last_response() == socket_equivalent
    assert drv.read_last_reply_clean() == socket_equivalent


def test_short_reply_is_not_swallowed():
    drv = _driver(["greeting"])
    assert drv.read_last_response() == "Greeting"


def test_falls_back_to_text_stripping_when_the_body_selector_is_gone():
    """A DOM change that removes .fai-CopilotMessage__content must degrade to the old
    string surgery -- and that path must ALSO drop the name line, which is the half the
    original reader got wrong."""
    drv = _driver(["answer_multiline"], body_present=False)
    got = drv.read_last_response()
    assert not got.startswith(AGENT)
    assert got.startswith("完了しました。")
    assert got.endswith("DONE")


def test_strip_agent_chrome_is_generic_over_the_agent_name():
    f = CopilotWebDriver._strip_agent_chrome
    assert f("Some Other Agent said:\nSome Other Agent\nhello\nDONE") == "hello\nDONE"
    assert f("plain text with no heading") == "plain text with no heading"
    assert f("") == ""


# --------------------------------------------------------------------------------------
# Same assertion against a REAL browser rendering the captured outerHTML, so the replay
# above cannot quietly disagree with what inner_text actually does (alt-text, whitespace,
# hidden nodes). Skipped where no Chromium is installed.
# --------------------------------------------------------------------------------------

def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    return sync_playwright


@pytest.mark.skipif(_playwright() is None, reason="playwright not installed")
def test_real_browser_over_the_captured_html():
    sync_playwright = _playwright()
    html = "<body>" + "".join(b["outerHTML"] for b in DOM["blocks"]) + "</body>"
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            pytest.skip("no chromium binary: %r" % (exc,))
        page = browser.new_page()
        page.set_content(html)

        block = page.locator(COPILOT_SELECTORS["assistant_msg"]).last
        raw = block.inner_text()
        body = block.locator(COPILOT_SELECTORS["assistant_msg_body"]).first.inner_text()

        drv = CopilotWebDriver.__new__(CopilotWebDriver)
        drv.page = page
        drv.answer_content_reads = 0
        drv._count_before = 0
        got = drv.read_last_response()

        browser.close()

    # the chrome is really there in a real rendering, and really absent from the body
    assert raw.startswith("%s said:" % AGENT)
    assert not body.startswith(AGENT)
    assert got == body.strip()
    assert got.endswith("DONE")

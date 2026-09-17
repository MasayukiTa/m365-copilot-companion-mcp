# -*- coding: utf-8 -*-
"""An Analyst answers in one line, and the completion test required a thousand characters.

MEASURED 2026-09-17 11:42. A worker emitted the ANALYZE line it had been asked for, the
session opened, the file was attached, the question was asked -- and then:

    [research] gave up: timeout: 600s without a finished report

The attachment path was never the problem. Acceptance was: a block counts as the finished
report only if it carries 「推論が N ステップで完了」 -- the header Copilot's Researcher writes
when a DEEP RESEARCH run ends -- or is at least 1000 characters long. An Analyst asked what
six characters are on a picture answers in one line and produces neither, so it could never be
accepted. Ten minutes, then discarded.

WHY THE FLOOR EXISTS, AND WHY IT IS NOT UNIVERSAL. A deep-research STATUS line is short and
can sit still long enough to look settled; the floor rejects those. That is a statement about
the Researcher, whose reports are long. Copying it onto an agent whose answers are short turns
a guard against false positives into a guarantee of false negatives. So the floor became
AgentProfile.min_report_chars and the Analyst got one it can clear.

AND THE DISCARDED BLOCK IS EVIDENCE. The sub-transcript recorded "(no report captured)", so
whatever the Analyst actually said -- including whether it could see the attached file at all
-- was thrown away. That is the shape of every defect found today: a failure written down as
an absence. Keeping the last block costs nothing and is the difference between "it timed out"
and "it timed out, and this was on the screen".
"""
from __future__ import annotations

import os
import sys
import time
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import agent_profiles as AP          # noqa: E402
from relay import settle as _settle             # noqa: E402


class _Drv:
    """The smallest driver poll() will talk to."""

    def __init__(self, block):
        self.block = block
        self.sent = []
        self.count = 1
        self.closed = False

    def _answers(self):
        return types.SimpleNamespace(count=lambda: self.count)

    def read_last_response(self):
        return self.block

    def _is_generating(self):
        return False

    def send(self, text):
        self.sent.append(text)

    def close(self):
        self.closed = True


def _session(block, profile, timeout_s=10_000):
    s = AP.ResearchSession.__new__(AP.ResearchSession)
    s.drv = _Drv(block)
    s.profile = profile
    s._count_before = 0
    s._t_send = time.time()
    s.timeout_s = timeout_s
    s.dwell_s = 0.2
    s.approval = AP.DEFAULT_APPROVAL
    s.max_approvals = 0
    s._approvals = 0
    s._approved = False
    s._last = None
    s._stable_since = None
    s._settle_state = _settle.SettleState()
    s._done = None
    s._pending_open = False
    s.socket = False
    s.tx_dir = ""
    s.parent_key = ""
    s.query = "q"
    return s


def _settle_out(s, tries=8):
    for _ in range(tries):
        out = s.poll()
        if out is not None:
            return out
        time.sleep(0.15)
    return s._done


def test_an_analyst_one_liner_is_accepted():
    """The measured case: six characters, one line, no marker. Ten minutes were spent
    refusing this."""
    s = _session("画像の6文字は 4CXZK8 です。", AP.ANALYST)
    assert _settle_out(s), "a one-line Analyst answer was still refused"
    assert "4CXZK8" in s._done


def test_a_researcher_one_liner_is_still_refused():
    """THE OTHER HALF. The floor was bought with an incident -- a short deep-research status
    line that sat still long enough to look settled -- and lowering it everywhere would buy
    that incident back. Same block, different agent, opposite answer."""
    s = _session("調査を実行しています…", AP.RESEARCHER)
    for _ in range(8):
        assert s.poll() is None
        time.sleep(0.15)
    assert s._done is None


def test_a_researcher_report_is_still_accepted():
    s = _session("本文" * 600, AP.RESEARCHER)
    assert _settle_out(s), "a long report stopped being accepted"


def test_what_was_on_the_screen_survives_the_timeout():
    """"(no report captured)" made an answer that was present indistinguishable from an agent
    that never replied."""
    s = _session("画像の6文字は 4CXZK8 です。", AP.ANALYST, timeout_s=0.0)
    s._t_send = time.time() - 5.0
    s.poll()
    assert s._done == "", "a timeout must still end the session"
    assert "4CXZK8" in getattr(s, "_rejected", ""), \
        "the block that was refused is the evidence; it was thrown away"
    assert "last block on screen" in (s.error or ""), s.error


def test_the_timeout_message_says_when_nothing_was_there():
    """An empty screen and a refused answer are different failures and must read differently."""
    s = _session("", AP.ANALYST, timeout_s=0.0)
    s._t_send = time.time() - 5.0
    s.poll()
    assert "nothing was on screen" in (s.error or ""), s.error


def test_the_floor_is_declared_on_the_profile_not_copied():
    """One owner for the number, so the two agents cannot drift apart silently."""
    assert AP.ANALYST.min_report_chars and AP.ANALYST.min_report_chars < AP.SUBSTANTIAL_CHARS
    assert AP.RESEARCHER.min_report_chars is None, \
        "the Researcher should fall through to the module default, not carry a copy"

def test_an_analyst_status_line_is_still_refused():
    """THE PROTECTION THE FLOOR USED TO PROVIDE, where it is actually implemented. Dropping the
    length floor to 4 means _is_processing carries the discrimination: a known progress marker
    under 40 characters is not an answer, whoever wrote it."""
    from relay.copilot_autopilot_relay import PROCESSING_MARKERS
    marker = next(m for m in PROCESSING_MARKERS if len(m) > 3)
    s = _session(marker, AP.ANALYST)
    for _ in range(6):
        assert s.poll() is None, "a progress marker was accepted as an answer"
        time.sleep(0.15)
    assert s._done is None

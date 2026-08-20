"""A driver that talks to the agent over a socket, shaped exactly like the one that drives a tab.

WHY IT IS SHAPED LIKE THE TAB DRIVER AND NOT LIKE A TRANSPORT. The fleet's turn loop is not a
thin wrapper -- it carries the settle rules, the stale-capture guard, the partial-preview path
and the marker discipline, all of them written against live failures. Putting a transport
abstraction underneath it would mean rewriting that loop for two worlds and testing it for one.
So this object answers to the same names the loop already calls, drops into `self.drv`, and the
loop cannot tell which one it has.

WHAT CHANGES, AND WHAT MUST NOT
  * memory. A tab costs 137-161 MB of JS heap and its renderer 285-657 MB; a socket costs a
    socket. That is the entire reason this exists.
  * nothing else. The same agent, the same connector, the same tools, the same tenant
    grounding -- because the socket names the agent and the backend binds the tools to it.

FALLBACK IS THE POINT, NOT A CAVEAT. The endpoint is undocumented and can change without
notice. `failed` says so plainly, and the caller opens a tab instead: losing this route costs
speed and memory, never a capability.

STREAMING IS REAL HERE. `ask` blocks until a turn completes, so the turn runs on its own thread
and the answer grows under a lock. The loop polls exactly as it does against a tab.
"""
from __future__ import annotations

import re
import threading

from relay.chathub import ChatHubError
from relay.copilot_autopilot_relay import GenerationInProgress

#: Citation markers the browser resolves into links and the socket hands over raw. Observed on
#: a live answer: `**166**  166 tools available.【1-abc】  【1-abc】: cite:1 "Citation-1"`. The
#: trailer is machinery, not prose, and leaving it in would put it in front of the decision
#: machinery and into transcripts. Only the machine-shaped form is touched -- Japanese text
#: uses 【】 for its own reasons and must survive.
_CITE_TRAILER = re.compile(r"^\s*【\d+-[0-9a-z]+】\s*:\s*cite:.*$", re.MULTILINE)
_CITE_MARKER = re.compile(r"【\d+-[0-9a-z]+】")


def normalize_answer(text: str) -> str:
    """The answer as a reader would see it in the tab, with the citation plumbing removed."""
    if not text:
        return ""
    out = _CITE_MARKER.sub("", _CITE_TRAILER.sub("", text))
    return re.sub(r"[ \t]+\n", "\n", out).strip()


class _Answers:
    """Stands in for the tab's answer blocks. The loop asks it one question: how many."""

    def __init__(self, n):
        self._n = n

    def count(self) -> int:
        return self._n


class CopilotSocketDriver:
    """Drives one Copilot conversation over a WebSocket. No tab, no DOM, no renderer."""

    def __init__(self, conversation, connect, *, catalogue=None, protocol="", run_tool=None):
        self.conv = conversation
        self._connect = connect
        self._catalogue = catalogue
        self._protocol = protocol or ""
        self._run_tool = run_tool

        self._lock = threading.Lock()
        self._thread = None
        self._answers_done = 0
        self._partial = ""
        self._last = ""
        #: Why the socket route stopped working, or "". The caller falls back on this, and it
        #: is a string rather than a flag so the reason survives into a log.
        self.failed = ""

        # The tab driver's contract, kept because the loop reads these directly.
        self._count_before = 0
        self.answer_content_reads = 0
        self._last_returned_reply = None

    # ---- what the loop calls ---------------------------------------------------------------

    def _answers(self):
        with self._lock:
            return _Answers(self._answers_done)

    def response_block_count(self) -> int:
        return self._answers().count()

    def _is_generating(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def send(self, text, gen_wait_s=None, **_kw):
        """Start a turn. Returns as soon as it is running, exactly as the tab driver does.

        `gen_wait_s` is accepted and ignored: it exists because the tab driver must not block
        the round-robin while a previous generation finishes, and here a turn never blocks the
        caller at all.
        """
        if self._is_generating():
            raise GenerationInProgress("a turn is already running on this socket")
        if self.failed:
            raise ChatHubError("this socket route already failed: %s" % self.failed)
        with self._lock:
            self._partial = ""
        self._thread = threading.Thread(target=self._run_turn, args=(text,),
                                        name="socket-turn", daemon=True)
        self._thread.start()

    def _run_turn(self, text):
        def on_text(sofar):
            with self._lock:
                self._partial = sofar

        try:
            answer = self.conv.ask(text, connect=self._connect, on_text=on_text,
                                   catalogue=self._catalogue, protocol=self._protocol,
                                   run_tool=self._run_tool)
        except Exception as exc:
            # THE ROUTE FAILING IS NOT THE JOB FAILING. Recorded, not raised: the caller reads
            # `failed`, opens a tab and carries on with the same goal.
            self.failed = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            return
        clean = normalize_answer(answer)
        if not clean:
            # A COMPLETED TURN THAT SAYS NOTHING IS NOT AN ANSWER. The backend surfaces tool
            # authorisation and confirmation as their own message types, which this reader
            # deliberately does not treat as prose -- so a consent card arrives here as an
            # empty answer. A tab can show that card and be clicked; a socket cannot. Falling
            # back is therefore the correct move, not an error to swallow.
            self.failed = "the turn completed but carried no text (a card the tab can show?)"
            return
        with self._lock:
            self._last = clean
            self._partial = ""
            self._answers_done += 1

    def read_last_response(self) -> str:
        """The answer, or as much of it as has arrived. Never blocks."""
        with self._lock:
            self.answer_content_reads += 1
            return self._last if not self._partial else normalize_answer(self._partial)

    def read_last_reply_clean(self) -> str:
        return self.read_last_response()

    def _is_stale_repeat(self, text: str) -> bool:
        """The tab reader could hand back the PREVIOUS turn's answer; this one cannot.

        Kept because the loop calls it, and answered honestly rather than copied: the text
        here is produced by the turn that is running, so it is never a stale capture. Two
        identical answers in a row are two identical answers, and suppressing the second would
        lose a real turn.
        """
        return False

    def _accept_new_reply(self, text: str) -> None:
        self._last_returned_reply = text

    def conversation_title(self) -> str:
        """No tab, so no title strip to scrape. The caller falls back to the goal text."""
        return ""

    def close(self):
        self.conv.close()

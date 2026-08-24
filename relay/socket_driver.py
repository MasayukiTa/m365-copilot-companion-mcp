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
        #: The completed-answer count when the last reply was accepted. Distinguishes "you are
        #: asking about the turn you already took" from "a later turn happened to say the same".
        self._accepted_at = -1

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

        # WHAT ELSE ARRIVED, so an empty answer can say what it was instead of only that it
        # was empty. The backend delivers tool authorisation and confirmation as their own
        # message types; this reader keeps them out of the prose, which is right, but dropping
        # them entirely left the one case that matters -- a completed turn carrying a consent
        # card and no text -- indistinguishable from a model that simply said nothing.
        seen_types = []

        def on_progress(item):
            try:
                mt = str((item or {}).get("type") or "")
                origin = str((item or {}).get("origin") or "")
                tag = mt + (("/" + origin) if origin else "")
                if tag and tag not in seen_types:
                    seen_types.append(tag)
            except Exception:
                pass

        try:
            answer = self.conv.ask(text, connect=self._connect, on_text=on_text,
                                   on_progress=on_progress,
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
            # Name the message types that DID arrive. Until the consent path is exercised
            # deliberately, nobody knows which of these a lapse produces -- so the honest move
            # is to record what was there and let the first real occurrence say.
            self.failed = ("the turn completed but carried no text (a card the tab can show?)"
                           + ((" -- frames: " + ", ".join(seen_types[:6])) if seen_types
                              else " -- no non-chat frames either"))
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
        """Whether the loop is asking about a turn it has ALREADY taken.

        NOT the tab's question, and the difference cost a run. The tab reader can hand back the
        PREVIOUS turn's DOM, so its guard asks "is this text stale". A socket's text is always
        produced by the turn that produced it, so this answered False -- and that removed a
        protection the loop was leaning on for a second purpose: stopping the same settled
        reply being decided twice.

        It mattered because another branch could leave the worker in 'waiting' after deciding,
        so the next sweep re-read the same answer and re-decided it. That branch is fixed, and
        this stops being the only thing standing between a re-read and a repeated decision.

        The honest form of the question here is not "is this stale" but "have I already handed
        you this one": the same text, with no new turn completed since it was accepted. Two
        genuinely identical answers on two different turns are still two answers.
        """
        return (bool(self._last_returned_reply)
                and text == self._last_returned_reply
                and self._answers_done == self._accepted_at)

    def _accept_new_reply(self, text: str) -> None:
        self._last_returned_reply = text
        self._accepted_at = self._answers_done

    def conversation_ids(self) -> dict:
        """Which conversation this driver has been talking to. Never raises.

        NOT PERSISTENCE -- just the ability to be asked. A socket worker used to end without
        leaving any trace of which conversation it had held, so a follow-up instruction could
        only start a new one: 531 of the 542 sessions on this machine carry no way back to the
        conversation they were about. The server keeps the history; what was being thrown away
        was the key to it.

        Both ids are exposed because it is not yet established that they agree. The client
        proposes one in the URL and the backend answers with one in the frames, and
        conversation_id_of already suspects they can differ -- "a backend that did not create
        the conversation will not continue it". Recording both is how that gets answered from
        operational data instead of from a guess.
        """
        try:
            c = self.conv
            return {
                "client": str(getattr(c, "conversation_id", "") or ""),
                "server": str(getattr(c, "server_conversation_id", "") or ""),
                "session": str(getattr(c, "session_id", "") or ""),
                "turns": int(getattr(c, "turns", 0) or 0),
            }
        except Exception:
            return {}

    def conversation_title(self) -> str:
        """No tab, so no title strip to scrape. The caller falls back to the goal text."""
        return ""

    def close(self):
        self.conv.close()

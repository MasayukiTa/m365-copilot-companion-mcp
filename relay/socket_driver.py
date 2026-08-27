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

import os
import re
import threading
import time

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


#: When a forced-failure WINDOW ends. None until the first forced turn opens it. Module
#: state, not environment state: os.environ is inherited by children, and a window is one
#: process's outage, not the machine's.
_FORCED_UNTIL = None


class CopilotSocketDriver:
    """Drives one Copilot conversation over a WebSocket. No tab, no DOM, no renderer."""

    #: Declared rather than sniffed. Callers that must branch on transport -- the bridge reads
    #: its answer from the DOM and cannot do that here -- get a name they can grep for, instead
    #: of a hasattr() on a method that someone later adds to the tab driver for another reason.
    IS_SOCKET = True

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
        #: Which turn we are on, and whether THIS one has produced an answer. Without the
        #: second flag "not generating and _last is non-empty" cannot tell a finished turn
        #: from a dead one standing in front of an older answer.
        self._turn_seq = 0
        self._turn_answered = False
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

    def wait_for_idle(self, timeout_s=180.0, poll_s=0.25) -> bool:
        """Block until the running turn finishes. True if it finished, False on timeout.

        THE ONE METHOD THE BRIDGE NEEDED AND THIS CLASS DID NOT HAVE. The bridge drives its
        conversation through exactly four calls -- send, _is_generating, read_last_response and
        this -- so the whole distance between its resident tab and a socket was one wrapper
        around the generation flag that `send` already maintains.

        Deliberately not `self._thread.join(timeout)`: a caller that asks "is it idle" must get
        the same answer whether a turn is running, already finished, or was never started, and
        join() on a thread that is None would have to be special-cased at every call site.

        A False here is a TIMEOUT, not a failure: the turn is still running and the caller
        decides whether to keep waiting. Whether the turn FAILED is `failed`, which is a
        separate question and is why this does not raise.
        """
        deadline = time.time() + float(timeout_s)
        while self._is_generating():
            if time.time() >= deadline:
                return False
            time.sleep(poll_s)
        return True

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
            # THE PREVIOUS ANSWER IS RETIRED HERE, not left lying around. It used to survive
            # into the next turn: `send` cleared only the partial, so between this call and
            # the first token, "what is the answer" returned the LAST turn's answer -- and if
            # the new turn then failed, `settled_text` kept returning it, which is a complete,
            # stable, wrong reply to a question that was never answered.
            self._last = ""
            self._turn_seq += 1
            self._turn_answered = False
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
            # A DELIBERATE WAY TO FAIL, because the failure path is the one that matters and
            # nothing could reach it on purpose. Every socket fault seen so far arrived by
            # luck, hours apart, in the middle of real work -- a poor way to find out whether
            # the bridge notices a dead turn or waits ten minutes and hands back a fragment.
            # Off unless the variable is set; its value is how many more turns to fail, so a
            # verification can watch the recovery as well as the failure.
            _forced = os.environ.get("MCP_SOCKET_FORCE_FAIL", "").strip()
            if _forced:
                # COUNT, OPTIONALLY FOLLOWED BY THE REASON TO FAIL WITH.
                #
                # The reason is not decoration: transport_policy classifies it, and the
                # classification decides whether the fault is coalesced into one incident
                # or votes separately against the breaker. So a forced failure carrying
                # only the words "forced failure" exercises the `unknown` path and cannot
                # test the transport path at all -- which is where the route actually
                # closed, on 2026-08-27 at 12:03, over an upstream proxy's HTTP 502.
                #
                # Split on the FIRST colon only: the reasons worth reproducing are full of
                # them ("could not open the socket: InvalidProxyStatus: proxy rejected
                # connection: HTTP 502").
                #
                # A COUNT ("3") OR A WINDOW ("45s"), AND THE WINDOW IS USUALLY THE HONEST
                # ONE. A count cannot reliably close the route, because closing takes three
                # CONSECUTIVE fallbacks and eight workers turn in parallel: a success from
                # any of them resets the counter between forced faults. Measured 2026-08-28
                # -- three forced faults across eight workers produced one fallback, then a
                # success, then nothing. The count was spent; the route never closed.
                #
                # A window fails EVERY attempt while it is open, which is what an upstream
                # proxy refusing upgrades actually does, and what it did on 2026-08-27 for
                # about a minute. No success can interleave, so the close is deterministic --
                # and when the window ends the transport is genuinely healthy again, which
                # is the state a reopen has to be tested against.
                _n, _, _why = _forced.partition(":")
                _why = _why.strip() or "forced failure (MCP_SOCKET_FORCE_FAIL)"
                if _n.endswith("s"):
                    global _FORCED_UNTIL
                    if _FORCED_UNTIL is None:
                        _FORCED_UNTIL = time.time() + float(_n[:-1])
                        print("[socket_driver] forcing every socket turn to fail for %ss"
                              % _n[:-1], flush=True)
                    if time.time() < _FORCED_UNTIL:
                        raise ChatHubError(_why)
                else:
                    left = int(_n)
                    if left > 0:
                        os.environ["MCP_SOCKET_FORCE_FAIL"] = "%d:%s" % (left - 1, _why)
                        raise ChatHubError(_why)
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
            self._turn_answered = True

    def read_last_response(self) -> str:
        """The answer, or as much of it as has arrived. Never blocks."""
        with self._lock:
            self.answer_content_reads += 1
            return self._last if not self._partial else normalize_answer(self._partial)

    def read_last_reply_clean(self) -> str:
        return self.read_last_response()

    # ---- the two states the bridge's DOM reads distinguish -----------------------------------
    #
    # The bridge does not read its answer through the driver at all: it scrapes
    # `loading-message` for the growing text and `lastChatMessage`, which populates only when
    # the turn is DONE, for the settled one. Its whole finish condition is built on the
    # difference. `read_last_response` deliberately blurs the two -- it is the fleet's
    # question, "what is the best text you have" -- so a socket could not answer the bridge's
    # question through it. These two can, and they read the same state the class already keeps.

    def partial_text(self) -> str:
        """What has arrived so far, growing. "" before the first token."""
        with self._lock:
            return normalize_answer(self._partial) if self._partial else ""

    def settled_text(self) -> str:
        """The completed answer, or "" while a turn is still running.

        EMPTY IS A REAL ANSWER HERE, and it is the one that makes the bridge's loop work: it
        means "not finished yet", exactly as an unpopulated lastChatMessage does.
        """
        if self._is_generating():
            return ""
        with self._lock:
            # ONLY FOR THE TURN THE CALLER IS WAITING ON. A turn whose thread died without an
            # answer is not generating any more either, and answering it with whatever text
            # is in hand is how a failure becomes a confident wrong reply.
            return (self._last or "") if self._turn_answered else ""

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

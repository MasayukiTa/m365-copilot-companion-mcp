"""Drive real turns through the settle predicate, so Stage 0 has something to replay.

WHY THE OBVIOUS GENERATOR WAS THE WRONG ONE

The first attempt drove the benchmark: 44 episodes through the bridge, expecting 44 recorded
turns. It produced two, and neither came from an episode. The relay's own configuration
comment says why, and had said so since the day it was written -- an earlier investigation
concluded that "no records at all across a full run proved wait_for_idle was never entered".
The interactive bridge turn does not go through the settle path at all. `_send_and_stream_once`
has its own completion handling; `wait_for_idle` belongs to the AUTOPILOT driver, which is what
the fleet, the agent profiles and the bridge's own idle tool-probe use. The two turns that did
get recorded were the probe.

So the generator has to be something that actually enters the predicate under study. This
does: it attaches to the fleet's Edge over CDP, sends prompts, and waits with the real
`wait_for_idle` -- the same code Stage 0 is replaying, driving a real tenant, with collect mode
recording every poll.

WHY IT USES THE FLEET'S EDGE AND NOT THE BRIDGE'S

They are deliberately separate profiles on separate ports so a fleet run and an interactive
session can coexist. Recording turns must not take the interactive session's page lock for
half an hour, and driving the bridge's page from outside the bridge would do exactly that.

WHY THE PROMPTS VARY

Twenty-two identical prompts repeated are not twenty-two samples; they draw the same response
shapes over and over, and an interval computed from them describes that repetition rather than
the population. The prompts here deliberately range over short factual answers, medium
explanations and long structured ones, because the length and the streaming pattern are what
the settle predicate is deciding about. This is a convenience sample of a live system and not
a random one, which is a real limitation and is reported with the result rather than beside it.
"""
from __future__ import annotations

import argparse
import os
import time

#: Varied on purpose -- see the module docstring. Short, medium and long answers, and a couple
#: that tend to stream in bursts, which is the shape a stability rule is most likely to get
#: wrong.
PROMPTS = (
    "Reply with exactly the word: ready",
    "In one sentence, what is idempotency?",
    "List three causes of flaky tests, one line each.",
    "Explain the difference between a mutex and a semaphore in about 120 words.",
    "Write a five-step checklist for reviewing a database migration.",
    "Summarise the trade-offs between polling and webhooks in about 150 words.",
    "Give a short worked example of Bayes' theorem with numbers.",
    "Describe, in about 200 words, how a write-ahead log makes a crash recoverable.",
    "Name four HTTP status codes and when each is appropriate.",
    "In about 250 words, explain what makes a benchmark's held-out set valuable.",
    "What is the difference between latency and throughput? Two sentences.",
    "Outline a plan for migrating a service to a new authentication scheme, six bullets.",
)


#: The plain Copilot chat. NOT an agent.
DEFAULT_CHAT_URL = "https://m365.cloud.microsoft/chat/"

#: URL fragments that identify a custom agent rather than the plain chat.
_AGENT_MARKERS = ("/chat/agent/", "/agents/")


def refuse_an_agent_url(url: str) -> None:
    """Refuse to drive a custom agent, and say what went wrong when one was driven.

    THE FIRST VERSION POINTED AT THE RESEARCHER AGENT, and every part of that was wrong in a
    way that reported success:

      * the deep-research agent answers a query with a SCOPING QUESTION -- "is this A, B or C,
        or say go ahead" -- and then waits. `ask_agent` exists partly to auto-approve that
        step; this module drove `send` and `wait_for_idle` directly and so skipped it;
      * the scoping question is itself a short, stable reply, so `wait_for_idle` accepted it
        and the run recorded a turn and printed "ok". Nothing was researched. The recorded
        sequences were short clarification prompts -- the opposite shape from the long
        streaming answers the settle predicate is interesting for;
      * and every parked turn raised an "agent task needs attention" notification on the
        operator's phone. The measurement was generating work for a person.

    The plain chat streams an answer directly, which is the behaviour under study.
    """
    lowered = (url or "").lower()
    if any(marker in lowered for marker in _AGENT_MARKERS):
        raise SystemExit(
            "refusing to drive a custom agent (%s). A research/analyst agent answers with a "
            "scoping question and waits for approval: wait_for_idle accepts that question as "
            "a settled reply, so the run records turns and reports ok while nothing streams, "
            "and each parked turn notifies the operator. Use the plain chat (%s)."
            % (url, DEFAULT_CHAT_URL))


def collect(*, cdp_url, agent_url, turns=24, timeout_s=180, dwell_s=2.0,
            on_turn=None) -> dict:
    """Send `turns` prompts through the real settle path. Returns what was recorded.

    Never raises for one turn's sake: a turn that times out is a turn the predicate could not
    settle, which is data rather than an error, and losing the rest of the run over it would
    trade a whole recording for one row.
    """
    if os.environ.get("MCP_SETTLE_TRACE_COLLECT") != "1":
        raise SystemExit(
            "refusing to run without MCP_SETTLE_TRACE_COLLECT=1: the ordinary trace records "
            "only turns already past 60 seconds and keeps no full text, so this would drive a "
            "live tenant for half an hour and produce nothing replayable")
    refuse_an_agent_url(agent_url)

    from playwright.sync_api import sync_playwright

    from relay.copilot_autopilot_relay import (CopilotWebDriver, find_conversation_page)

    done, failed, started = 0, 0, time.time()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()

        for i in range(turns):
            prompt = PROMPTS[i % len(PROMPTS)]
            try:
                # A FRESH CONVERSATION PER TURN. Loading the bare agent URL starts a new chat,
                # and a turn that inherits the previous answer's text starts its stability
                # count against the wrong baseline -- which would be a defect introduced by
                # the measurement itself.
                page = find_conversation_page(context, agent_url)
                drv = CopilotWebDriver(page)
                drv.send(prompt)
                ok = drv.wait_for_idle(timeout_s=timeout_s, dwell_s=dwell_s)
                done += 1
                if on_turn is not None:
                    on_turn(i + 1, prompt, ok)
            except Exception as exc:
                failed += 1
                if on_turn is not None:
                    on_turn(i + 1, prompt, "%s: %s" % (type(exc).__name__, exc))

    return {"turns_driven": done, "turns_failed": failed,
            "wall_clock_s": round(time.time() - started, 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cdp-url", default=os.environ.get("MCP_FLEET_CDP_URL",
                                                        "http://127.0.0.1:9222"))
    ap.add_argument("--chat-url", default=os.environ.get("MCP_SETTLE_CHAT_URL",
                                                         DEFAULT_CHAT_URL),
                    help="the PLAIN Copilot chat. A custom agent is refused -- see "
                         "refuse_an_agent_url for what happened when one was driven.")
    ap.add_argument("--turns", type=int, default=24)
    ap.add_argument("--timeout", type=float, default=180)
    args = ap.parse_args(argv)

    def progress(n, prompt, ok):
        print("  %3d  %-6s %s" % (n, "ok" if ok is True else str(ok)[:6], prompt[:60]),
              flush=True)

    out = collect(cdp_url=args.cdp_url, agent_url=args.chat_url, turns=args.turns,
                  timeout_s=args.timeout, on_turn=progress)
    print("driven %d, failed %d, %.0fs" % (out["turns_driven"], out["turns_failed"],
                                           out["wall_clock_s"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

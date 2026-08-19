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
    # --- widened 2026-08-18 ------------------------------------------------------------
    # TWELVE WAS THE CEILING ON THE WHOLE EXPERIMENT. The offline replay over the previous
    # collection came out as twelve clusters carrying a hundred and twenty turns, and the
    # plan's own power calculation asks for roughly forty discordant PAIRS -- which twelve
    # units cannot produce however many times they are repeated. More turns against the same
    # twelve prompts adds rows and no information.
    #
    # The axes below are the ones a stability rule is actually deciding about: how long the
    # answer is, and whether it arrives smoothly or in bursts. So the set ranges over one-word
    # replies, tables (which stream a row at a time), code blocks (which pause at fence
    # boundaries), enumerations, and long prose.
    "Reply with only: ok",
    "What year did the first web browser ship? Just the number.",
    "Give the SI unit of pressure and its definition in one line.",
    "Name the four ACID properties, comma separated, nothing else.",
    "In two sentences, why is UTC preferred over local time in logs?",
    "What does a 503 mean, and how does it differ from a 500? Two lines.",
    "Define cardinality in the context of a database index, briefly.",
    "One sentence: what problem does a bloom filter solve?",
    "Explain, in about 80 words, why floating point addition is not associative.",
    "Describe the difference between at-least-once and exactly-once delivery, 100 words.",
    "In about 120 words, explain what a race condition is, with one concrete example.",
    "Summarise the CAP theorem in about 130 words without using the word 'trade-off'.",
    "Explain in about 150 words how a hash join differs from a nested loop join.",
    "In roughly 180 words, describe what happens between typing a URL and the first byte.",
    "Explain garbage collection generational hypothesis in about 200 words.",
    "In about 250 words, describe how TLS establishes a shared secret.",
    "Write a markdown table of five sorting algorithms with time and space complexity.",
    "Produce a table comparing four HTTP caching headers and when each applies.",
    "Make a table of six git commands and what each one does to the index.",
    "Write a short Python function that merges two sorted lists, with a docstring.",
    "Show a SQL query that finds the second highest salary, and explain it briefly.",
    "Write a bash one-liner that finds the ten largest files under a directory, explained.",
    "Give a JSON example of a paginated API response, with a note on each field.",
    "List seven code review smells, one line each.",
    "Enumerate eight steps for onboarding a new service into an on-call rotation.",
    "Give ten questions to ask before adopting a new dependency.",
    "List five ways a retry can make an outage worse, one line each.",
    "Name six metrics worth alerting on for a queue-backed worker, with thresholds.",
    "Outline a rollback plan for a schema change that has already shipped, seven bullets.",
    "Draft a five-point checklist for reviewing a change to authentication code.",
    "Give a step-by-step plan for finding a memory leak in a long-running process.",
    "Describe how you would bisect a performance regression across 200 commits.",
    "Explain the difference between a feature flag and a config toggle, and when each fits.",
    "In about 100 words, what makes an error message good? Give one bad and one good example.",
    "Explain idempotency keys in payment APIs in about 140 words.",
    "What is the difference between a leader election and a lock? About 120 words.",
    "Describe backpressure and two ways to implement it, about 160 words.",
    "In about 200 words, explain why distributed tracing needs sampling.",
    "Explain what a flaky test costs a team, in about 120 words.",
    "Describe three strategies for migrating data with zero downtime, one paragraph each.",
    "What is the difference between authentication and authorisation? Two sentences.",
    "Explain the purpose of a dead letter queue in about 90 words.",
    "In one sentence each, define: p50, p95, p99.",
    "Name five reasons a deploy might succeed in staging and fail in production.",
    "Describe how a circuit breaker works and what its three states are.",
    "Explain in about 110 words why timestamps from different machines cannot be compared.",
    "Give a worked example of computing a confidence interval for a proportion.",
    "Explain McNemar's test and when it is preferred over a chi-squared test, 150 words.",
    "What is Simpson's paradox? Give a numeric example in about 130 words.",
    "In about 170 words, explain the difference between precision and recall with an example.",
)


#: The plain Copilot chat. NOT an agent.
DEFAULT_CHAT_URL = "https://m365.cloud.microsoft/chat/"

#: A trace file of this collector's OWN, separate from whatever else is recording.
#:
#: The first run shared the default path with a bridge that was also in collect mode, and the
#: bridge's idle tool-probe wrote its synthetic turns into the same file. Two of the three
#: truncations the replay then found were probe turns -- a different population entirely,
#: mixed in silently because both writers agreed on a filename. A campaign's data should be
#: identifiable as that campaign's.
DEFAULT_TRACE_NAME = "settle_trace_collect.jsonl"

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
            on_turn=None, campaign="c1") -> dict:
    """Send `turns` prompts through the real settle path. Returns what was recorded.

    Never raises for one turn's sake: a turn that times out is a turn the predicate could not
    settle, which is data rather than an error, and losing the rest of the run over it would
    trade a whole recording for one row.
    """
    if not os.environ.get("MCP_SETTLE_TRACE_PATH"):
        raise SystemExit(
            "set MCP_SETTLE_TRACE_PATH to this campaign's own file (suggested name: %s). "
            "Sharing the default path with a bridge that is also recording mixes its "
            "synthetic probe turns into the population, which is how two of the first run's "
            "three findings turned out to be probes." % DEFAULT_TRACE_NAME)
    if os.environ.get("MCP_SETTLE_TRACE_COLLECT") != "1":
        raise SystemExit(
            "refusing to run without MCP_SETTLE_TRACE_COLLECT=1: the ordinary trace records "
            "only turns already past 60 seconds and keeps no full text, so this would drive a "
            "live tenant for half an hour and produce nothing replayable")
    refuse_an_agent_url(agent_url)

    from playwright.sync_api import sync_playwright

    from relay import copilot_autopilot_relay as CAR
    from relay.copilot_autopilot_relay import (CopilotWebDriver, find_conversation_page)

    # WHAT THIS RUN BUYS, SAID BEFORE IT SPENDS AN HOUR. Turns beyond the number of distinct
    # prompts are repeats of a cluster, not new clusters, and the statistic the plan needs
    # counts clusters. Reported rather than enforced -- a repeat run is a legitimate thing to
    # want, it just must not be mistaken for a wider one.
    clusters = min(turns, len(PROMPTS))
    repeats = turns / float(len(PROMPTS)) if PROMPTS else 0.0
    print("[settle_collect] %d turns over %d prompts -> %d clusters, %.1f repeats each"
          % (turns, len(PROMPTS), clusters, repeats))
    if turns > len(PROMPTS):
        print("[settle_collect] NOTE: %d of those turns are repeats of a prompt already "
              "recorded. The plan's power calculation counts clusters, so this run adds "
              "%d units of information and %d rows."
              % (turns - clusters, clusters, turns))
    if clusters < 40:
        print("[settle_collect] WARNING: %d clusters is below the ~40 discordant pairs the "
              "plan asks for; a Stage 1 built on this will be underpowered whatever the "
              "turn count says." % clusters)

    done, failed, started = 0, 0, time.time()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()

        for i in range(turns):
            prompt = PROMPTS[i % len(PROMPTS)]
            # LABEL THE CLUSTER. Twelve prompts cycled ten times is twelve independent units,
            # not 120, and an interval computed as though it were 120 is too narrow. The
            # grouping has to be recorded while it is known; it cannot be recovered from the
            # trace afterwards, because the trace holds answers rather than questions.
            CAR.settle_trace_set_cluster("%s.p%02d" % (campaign, i % len(PROMPTS)))
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
    # NAMES THE CORPUS. `turn_id` is "<campaign>.p<NN>|<turn>", so two runs sharing a
    # campaign name produce prefixes that collide -- and the replay groups clusters by
    # that prefix, so concatenating the files would silently merge two populations into
    # one and understate the cluster count.
    ap.add_argument("--campaign", default="c1")
    args = ap.parse_args(argv)

    def progress(n, prompt, ok):
        print("  %3d  %-6s %s" % (n, "ok" if ok is True else str(ok)[:6], prompt[:60]),
              flush=True)

    out = collect(cdp_url=args.cdp_url, agent_url=args.chat_url, turns=args.turns,
                  timeout_s=args.timeout, on_turn=progress, campaign=args.campaign)
    print("driven %d, failed %d, %.0fs" % (out["turns_driven"], out["turns_failed"],
                                           out["wall_clock_s"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

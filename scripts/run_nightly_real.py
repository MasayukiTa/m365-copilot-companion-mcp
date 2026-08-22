"""Run `nightly()` with a real evaluator. It has never been run at all.

WHAT IS DIFFERENT FROM scripts/run_route_campaign.py

The campaign script calls the evaluator directly on a candidate I wrote by hand. That measures
a hypothesis; it does not exercise the loop. `nightly()` is the part that decides WHAT to try:
it reads recent decisions, checks whether the harness is well enough to run, selects a replay
set, and then sweeps coordinates, generating the candidate genomes itself. Until this runs,
"the system proposes its own experiments" is a claim about code nobody has executed -- and
this repository has found, four times, that an untested path is usually a broken one. The
comments inside `nightly()` say as much: `.entries()` did not exist on Archive, and the key it
read from archive rows was one nothing ever wrote.

SCOPE IS ONE COORDINATE ON PURPOSE

The sweep would otherwise visit memory, planner, quality_cards, max_refute_passes,
max_retries and memory_max_items, and the route evaluator cannot judge any of them -- it
measures transport. Handing it those coordinates would produce six confident rows about
comparisons where both arms ran the same program, which is the exact defect this evaluator
was built to escape. `transport` is the coordinate it can answer.

ACTIVATION IS OFF

A KEEP here does not change the running harness. `activate=False` is the operator's switch and
the safe value is the one you get without thinking about it.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay.selfimprove import archive as A  # noqa: E402
from relay.selfimprove import campaign as C  # noqa: E402
from relay.selfimprove import scheduler as S  # noqa: E402
from relay.selfimprove.controller import EvolutionController  # noqa: E402
from scripts.run_route_campaign import GOALS  # noqa: E402

RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "docs", "research", "results")
ARCHIVE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       ".fleet", "selfimprove", "archive.jsonl")


def main():
    agent_url = os.environ.get("MCP_FLEET_AGENT_URL", "")
    if not agent_url:
        env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        for line in open(env, encoding="utf-8", errors="ignore"):
            if line.startswith("MCP_FLEET_AGENT_URL="):
                agent_url = line.split("=", 1)[1].strip()
                break

    stamp = int(time.time())
    # Arm order alternates between nightly runs so the pair forms a crossover: whatever the
    # position of an arm costs, it costs each treatment once across the pair.
    candidate_first = "--candidate-first" in sys.argv
    # BOTH ARMS ON THE ROUTE, ALWAYS.
    #
    # `worker_done` rows -- where the turns instrument reads its observable -- are written only
    # while the socket route is enabled. With the control arm on tabs it logs nothing, and the
    # first planner run did exactly that: the control recorded zero rows, the judge was handed
    # a fabricated 0.0 turns per goal against the candidate's 1.0, and reported a difference of
    # -1.00 that no measurement supports. Holding the transport fixed across both arms costs
    # nothing here, because transport is not what this comparison is about.
    evaluate = S.route_evaluator_for(
        GOALS, agent_url=agent_url, max_concurrent=2, warmup=True,
        candidate_first=candidate_first, control_socket=True,
        transcript_dir=os.path.join(RESULTS, "tx", "nightly-%d" % stamp))

    reasons = S.preconditions(budget_candidates=1, activate=False)
    print("[nightly] preconditions: %s" % (reasons or "CLEAR"), flush=True)
    if reasons:
        print(json.dumps({"ran": False, "blocked_by": reasons}, ensure_ascii=False, indent=2))
        return

    # Straight to the sweep rather than through nightly(): nightly() hands the evaluator
    # every coordinate, and this evaluator can only answer one. Restricting coords is the
    # honest way to run it -- the alternative is six rows about arms that were identical.
    archive = A.Archive(ARCHIVE)
    controller = EvolutionController(activate=False, archive=archive)
    # WHICH COORDINATE, FROM THE COMMAND LINE. `transport` was the only one an instrument
    # could judge when this was written; `planner` is the second, and hardcoding the first
    # would have left the second unreachable from the entry point that exists.
    coord = next((a for a in sys.argv[1:] if not a.startswith("-")), "transport")
    print("[nightly] coordinate: %s" % coord, flush=True)
    out = C.sweep(controller, evaluate=evaluate, coords=[coord],
                  on_result=lambda row: print("[nightly] %s -> %s: %s"
                                              % (row["coordinate"], row["state"],
                                                 row["reason"][:120]), flush=True))
    out["arm_order"] = "candidate,control" if candidate_first else "control,candidate"
    path = os.path.join(RESULTS, "nightly_real_%d.json" % stamp)
    os.makedirs(RESULTS, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str), flush=True)
    print("[nightly] wrote %s" % path, flush=True)


if __name__ == "__main__":
    main()

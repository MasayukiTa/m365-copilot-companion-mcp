"""Run the transport A/B and record whatever comes out, including a refusal.

THE GOAL SET IS CHOSEN SO THE TWO VERSIONS CAN DIFFER

`transport/v1` returns SOCKET for everything; `transport/v2` sends Work IQ goals to a tab and
the rest to a socket. A goal set with no Work IQ in it makes the two versions the same program
on this data, and the run would return a confident null about a difference it never gave the
component a chance to express. So half the goals are Work IQ and half are not.

They are also small on purpose. This is the first measured experiment this loop has ever
completed; the thing being established is that a verdict can be reached at all, and a long run
on a machine with 200 MB of headroom would abort on the floor before it told us that.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import provenance as PROV  # noqa: E402
from relay.selfimprove import ledger as L  # noqa: E402
from relay.selfimprove import manifest as M  # noqa: E402
from relay.selfimprove import scheduler as S  # noqa: E402

#: Two that the FIXED predicate sends to a tab under v2, two it clears for a socket.
GOALS = [
    "Outlook の受信トレイから今日届いた未読メールの件名を3件挙げて",
    "Teams の直近の会議で決まったことを1行でまとめて",
    "Python で与えられた文字列が回文かどうかを判定する関数を書いて",
    "1 から 100 までの整数のうち 3 と 5 の両方で割り切れるものを列挙して",
]

RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "docs", "research", "results")
OUT = os.path.join(RESULTS, "route_campaign.json")


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else "transport/v2"
    candidate_first = "--candidate-first" in sys.argv
    candidate = M.apply_genome(M.base_manifest(), {"components": {"transport": version}})
    agent_url = os.environ.get("MCP_FLEET_AGENT_URL", "")
    if not agent_url:
        for line in open(os.path.join(os.path.dirname(OUT), "..", "..", "..", ".env"),
                         encoding="utf-8", errors="ignore"):
            if line.startswith("MCP_FLEET_AGENT_URL="):
                agent_url = line.split("=", 1)[1].strip()
                break
    print("[campaign] candidate=%s goals=%d" % (version, len(GOALS)), flush=True)
    print("[campaign] agent=%s" % agent_url[:60], flush=True)

    exp = "route-%s%s-%s-%d" % (version.replace("/", "-"),
                              "-NULL" if "--null" in sys.argv else "",
                              "candfirst" if candidate_first else "ctrlfirst",
                              int(time.time()))
    led = L.HypothesisLedger()
    # BEFORE THE ARMS, NOT AFTER. A proposal written once the numbers are in is not a
    # prediction, and the ledger exists precisely because an automated proposer will
    # rationalise any outcome on request. The first campaign was run before this line
    # existed and is deliberately NOT backfilled -- see the results file.
    led.propose(
        experiment_id=exp,
        candidate_id=M.harness_id(candidate),
        parent_harness_id=M.harness_id(M.base_manifest()),
        target_failure_class="edge_memory_exhaustion",
        hypothesis=(
            "NULL RUN: both arms are the control. Any difference reported is the "
            "instrument's noise, and it is what the decision threshold has to clear."
            if "--null" in sys.argv else
            "Sending goals the fixed Work IQ predicate clears over a socket instead "
            "of a tab lowers peak Edge memory without losing completions, because a "
            "socket carries the conversation without a renderer."),
        changed_components={"transport": version},
        predicted_effect={"peak_edge_mb": "-300 or better", "done": "unchanged"},
        possible_regressions=["a goal that needed a tab falls back and costs a turn",
                              "Work IQ answers formed without Work IQ context"],
        evaluation_plan={
            "arms": "control=tabs everywhere under the base manifest; "
                    "candidate=the route under this manifest",
            "goals": len(GOALS),
            "measured": ["peak Edge memory (a rise over the arm's own start)",
                         "wall clock", "goals reaching DONE", "fallbacks"],
            "rule": "route_evaluator.decide: DONE loss -> reject; >=300 MB gain at equal "
                    "DONE -> keep; otherwise inconclusive",
            "known_bias": "arms run in sequence, so the second inherits the first's Edge "
                          "residue; start_mb is recorded per arm",
        },
        # The key is "authority", and anything else falls through to EXTERNAL_UNTRUSTED --
        # fail-closed, which is why the first attempt at this line was refused. The weakest
        # item decides, so every one has to stand on its own.
        evidence=[
            {"source": "docs/research/results/route_campaign.json",
             "authority": PROV.MACHINE_VERIFIER,
             "note": "first campaign, measured: both arms 4/4 DONE, 0 fallbacks, "
                     "control peak +364.6 MB vs candidate +569.0 MB, floor broke at 952 MB"},
            {"source": "operator, 2026-08-21",
             "authority": PROV.OPERATOR_INSTRUCTION,
             "note": "the memory floor for this machine is 512 MB"},
        ],
    )
    evaluate = S.route_evaluator_for(GOALS, agent_url=agent_url, max_concurrent=2,
                                     candidate_first=candidate_first,
                                     warmup="--warmup" in sys.argv,
                                     null_arm="--null" in sys.argv)
    t0 = time.time()
    out = evaluate(candidate, exp)
    out["wall_s"] = round(time.time() - t0, 1)
    out["version"] = version
    infra = out.get("infra") or {}
    if infra.get("aborted"):
        # INFRA_ABORT, never INCONCLUSIVE. "the harness broke" and "the change did nothing"
        # must not pool -- the ledger's docstring says so and this is the first run to test it.
        led.conclude(experiment_id=exp, verdict=L.INFRA_ABORT,
                     actual_effect={"control": out.get("control"),
                                    "candidate": out.get("candidate"),
                                    "min_free_mb": out.get("min_free_mb")},
                     infra_delta=1, note=infra.get("reason", ""))
    else:
        gate = out.get("gate") or {}
        verdict = {"keep": L.KEEP, "reject": L.REJECT}.get(gate.get("verdict"),
                                                           L.INCONCLUSIVE)
        led.conclude(experiment_id=exp, verdict=verdict,
                     actual_effect={"control": out.get("control"),
                                    "candidate": out.get("candidate"),
                                    "memory_gain_mb": out.get("memory_gain_mb")},
                     note=gate.get("reason", ""))
    out["ledger_experiment_id"] = exp
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # One file per run. Overwriting would erase the mirrored-order run with its pair.
    per_run = os.path.join(RESULTS, "route_campaign_%s.json" % exp)
    with open(per_run, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
    print("[campaign] wrote %s" % OUT, flush=True)


if __name__ == "__main__":
    main()

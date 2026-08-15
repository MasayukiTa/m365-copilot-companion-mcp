"""Run every pool once and write down what happened. The thing that was missing.

WHY THIS DID NOT EXIST, AND WHY THAT MATTERED

Every piece needed to compare two harnesses was built and tested: episodes, graders, the
paired evaluator, the significance gate, the archive, the ledger. What there was no way to do
was RUN THE BENCHMARK -- `run_episode` took one episode, and nothing took the suite. So the
archive was empty, and "we can measure Companion performance meaningfully" was a statement
about the code rather than about any number that existed.

A comparison also needs something to compare against. Without a baseline, the first candidate
is measured against nothing and the gate has no denominator.

WHAT A BASELINE RUN IS

Every episode in every pool, once, against the CURRENT harness and a real target. No
candidate, no arms, no gate -- just what the system does today, recorded with enough context
to be re-read later: which dataset, which grader, which agent, and what each episode scored
rather than only whether it passed.

WHAT IT REFUSES TO DO

It will not run against a simulated agent. SimulatedAgent exists to test the harness and says
so in its own docstring; a baseline produced from a script is a measurement of the script.
The refusal is explicit rather than a note, because a number that looks like a baseline gets
quoted like one.
"""
from __future__ import annotations

import argparse
import json
import os
import time

from bench.companionbench import agents as A
from bench.companionbench import runner as R
from bench.companionbench.pools import EVOLUTION, REGISTRY, REGRESSION, SEALED
from bench.companionbench.runner import SECURITY_CATEGORY
from relay.selfimprove import manifest as M

#: Pools in the order a reader wants them: the pool the loop optimises against, the pool that
#: must not break, and the pool nobody has optimised against at all.
POOLS = (EVOLUTION, REGRESSION, SEALED)


class RefusedToMeasure(RuntimeError):
    """Raised when the requested run would produce a number that means nothing."""


def run_suite(agent, *, pools=POOLS, root=None, on_result=None, limit=0) -> dict:
    """Every episode of every named pool, once. Never raises for an episode's sake.

    `on_result(row)` is called as each episode finishes, because a suite against a live agent
    takes half an hour and a run that reports only at the end is a run nobody can watch.
    """
    _refuse_a_meaningless_target(agent)

    started = time.time()
    rows, by_pool = [], {}
    for pool in pools:
        episodes = REGISTRY.get(pool)
        if limit:
            episodes = episodes[:limit]
        pool_rows = []
        for episode in episodes:
            row = R.run_episode(episode, agent, root=root)
            row["pool"] = pool
            pool_rows.append(row)
            rows.append(row)
            if on_result is not None:
                on_result(row)
        by_pool[pool] = summarise(pool_rows)

    return {
        "rows": rows,
        "by_pool": by_pool,
        "by_category": _by_category(rows),
        "totals": summarise(rows),
        "harness_id": M.harness_id(M.base_manifest()),
        "agent": R.describe_agent(agent),
        "dataset_fingerprint": R.dataset_fingerprint(),
        "grader_version": R._grader_version(),
        "wall_clock_s": round(time.time() - started, 1),
        "started_at": started,
    }


def repeat_suite(agent, *, repeats=3, pools=POOLS, root=None, on_result=None,
                 on_run=None, limit=0) -> dict:
    """Run the whole suite `repeats` times and report how much the verdicts move.

    WHY THIS EXISTS. Three runs of this suite against the same system, with nothing changed
    between them, scored 13/22, 6/22 and 8/22 -- and 19 of the 22 episodes returned a
    different verdict in at least one run. Only three were stable. A single run's number is
    therefore mostly noise, and every comparison built on top of one inherits that: a paired
    A/B assumes a difference between arms is attributable to the arm, which cannot hold while
    one episode's verdict is close to a coin flip.

    So the reliability is measured rather than assumed. `stable` is the count of episodes that
    gave the same answer every time; `flipped` is the rest, listed, because which ones move is
    more actionable than how many.
    """
    runs = []
    for i in range(max(1, repeats)):
        out = run_suite(agent, pools=pools, root=root, on_result=on_result, limit=limit)
        runs.append(out)
        if on_run is not None:
            on_run(i + 1, out)
    return {"repeats": len(runs), "runs": runs, "reliability": reliability(runs)}


def reliability(runs) -> dict:
    """Per-episode agreement across repeated runs of the same suite."""
    per = {}
    for run in runs:
        for row in run["rows"]:
            per.setdefault(row["episode_id"], []).append(bool(row.get("success")))
    stable = sorted(k for k, v in per.items() if len(set(v)) == 1)
    flipped = sorted(k for k, v in per.items() if len(set(v)) > 1)
    totals = [r["totals"]["passed"] for r in runs]
    attempted = [r["totals"]["attempted"] for r in runs]
    return {
        "episodes": len(per),
        "stable": len(stable),
        "flipped": len(flipped),
        "flipped_ids": flipped,
        "pass_counts": totals,
        "attempted": attempted,
        "spread": (max(totals) - min(totals)) if totals else 0,
        "per_episode_rate": {k: round(sum(v) / len(v), 3) for k, v in sorted(per.items())},
        "note": "a single run's total is only as meaningful as the spread here is small; "
                "an A/B whose effect is smaller than this spread is measuring the weather",
    }


def _refuse_a_meaningless_target(agent):
    """A baseline from a scripted agent measures the script.

    Stated as a refusal rather than a caveat: a caveat travels separately from the number,
    and this number will be quoted.
    """
    name = type(agent).__name__
    if name == "SimulatedAgent" or getattr(agent, "simulated", False):
        raise RefusedToMeasure(
            "SimulatedAgent exists to test the harness, not to measure capability; a "
            "baseline produced from a script is a measurement of the script")


def summarise(rows) -> dict:
    """Pass rate and the things a bare pass rate hides.

    INFRA IS REPORTED SEPARATELY AND EXCLUDED FROM THE DENOMINATOR. An episode the
    environment could not run is not a failure of the system under test, and folding it in
    makes a bad afternoon look like a regression. `attempted` is the honest denominator and
    `infra` is the number that says how much of the suite actually got measured.
    """
    rows = list(rows)
    infra = [r for r in rows if r.get("infra_failure")]
    attempted = [r for r in rows if not r.get("infra_failure")]
    passed = [r for r in attempted if r.get("success")]
    security = [r for r in attempted if r.get("category") == SECURITY_CATEGORY]
    return {
        "total": len(rows),
        "attempted": len(attempted),
        "passed": len(passed),
        "pass_rate": round(len(passed) / len(attempted), 4) if attempted else None,
        "infra": len(infra),
        "infra_ids": [r["episode_id"] for r in infra],
        "failed_ids": [r["episode_id"] for r in attempted if not r.get("success")],
        # Security is not a pass rate. An episode can pass while the coverage that produced
        # the pass was partial, and reporting only the rate hides which of the two it was.
        "security": {
            "clean": sum(1 for r in security if r.get("security_score", 0) >= 1.0),
            "total": len(security),
            "coverage": _coverage_counts(security),
        },
        "median_latency_s": _median([r.get("latency_s") or 0.0 for r in attempted]),
    }


def _coverage_counts(rows) -> dict:
    out = {}
    for row in rows:
        key = row.get("security_coverage") or "unreported"
        out[key] = out.get(key, 0) + 1
    return out


def _by_category(rows) -> dict:
    out = {}
    for row in rows:
        out.setdefault(row.get("category", "?"), []).append(row)
    return {k: summarise(v) for k, v in sorted(out.items())}


def _median(values):
    values = sorted(v for v in values if v)
    if not values:
        return 0.0
    mid = len(values) // 2
    if len(values) % 2:
        return round(values[mid], 2)
    return round((values[mid - 1] + values[mid]) / 2, 2)


def report(result) -> str:
    """The run, as a person would want to read it."""
    lines = ["COMPANIONBENCH BASELINE", "",
             "agent           %s" % json.dumps(result["agent"], ensure_ascii=False),
             "harness         %s" % result["harness_id"][:16],
             "dataset         %s" % result["dataset_fingerprint"][:16],
             "grader          %s" % result["grader_version"][:16],
             "wall clock      %.0fs" % result["wall_clock_s"], ""]

    lines += ["%-12s %8s %8s %8s %8s" % ("pool", "passed", "of", "infra", "rate"), "-" * 48]
    for pool, s in result["by_pool"].items():
        lines.append("%-12s %8d %8d %8d %8s"
                     % (pool, s["passed"], s["attempted"], s["infra"],
                        "n/a" if s["pass_rate"] is None else "%.2f" % s["pass_rate"]))
    t = result["totals"]
    lines += ["-" * 48,
              "%-12s %8d %8d %8d %8s"
              % ("all", t["passed"], t["attempted"], t["infra"],
                 "n/a" if t["pass_rate"] is None else "%.2f" % t["pass_rate"]), ""]

    lines += ["by category", ""]
    for cat, s in result["by_category"].items():
        lines.append("  %-14s %d/%d%s"
                     % (cat, s["passed"], s["attempted"],
                        "   (%d infra)" % s["infra"] if s["infra"] else ""))

    sec = t["security"]
    lines += ["", "security  %d/%d clean   coverage %s"
              % (sec["clean"], sec["total"], json.dumps(sec["coverage"]))]

    failures = [r for r in result.get("rows", [])
                if not r.get("success") and not r.get("infra_failure")]
    if failures:
        # WHAT THE GRADER SAW, not just which ids failed. Each grader records its own keys --
        # `target_updated`, `collaterally_changed`, whatever that episode is about -- and an
        # earlier version of this report looked only for `details.reason`, which almost none
        # of them set. So every failure printed as a bare id and the run said what broke
        # without saying anything about why, which is most of the value of a baseline.
        lines += ["", "failures", ""]
        for row in failures:
            detail = ", ".join("%s=%s" % (k, v)
                               for k, v in sorted((row.get("details") or {}).items())
                               if v not in ((), [], {}, "", None)) or "(grader recorded no detail)"
            lines.append("  %-28s %s" % (row["episode_id"], detail[:110]))
    if t["infra_ids"]:
        lines += ["infra:   " + ", ".join(t["infra_ids"]),
                  "",
                  "Infra is excluded from the denominator: an episode the environment could",
                  "not run is not a failure of the system under test. It is reported because",
                  "a suite that measured half of itself should say so."]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------

def build_agent(kind: str):
    """The real targets only. `kind` is bridge or fleet."""
    if kind == "bridge":
        if not A.bridge_available():
            raise RefusedToMeasure(
                "nothing is listening on the bridge; a baseline cannot be produced from an "
                "absent target, and pretending otherwise is what the infra category is for")
        return A.BridgeAgent()
    if kind == "fleet":
        # `agent_url` is REQUIRED and is the chat URL a worker's tab opens -- not an API
        # endpoint and not the bridge. This function used to call FleetAgent() with no
        # arguments, which cannot construct: the fleet target was reachable from the CLI in
        # name only. It comes from the operator's environment because it is tenant-specific
        # and does not belong in this repository.
        from bench.companionbench.fleet_agent import FleetAgent
        agent_url = (os.environ.get("MCP_FLEET_AGENT_URL")
                     or os.environ.get("MCP_IMPL_AGENT_URL") or "")
        if not agent_url:
            raise RefusedToMeasure(
                "the fleet target needs MCP_FLEET_AGENT_URL (or MCP_IMPL_AGENT_URL): the "
                "chat URL a worker tab opens. Without it the fleet opens a tab with no "
                "composer and reports a dead target ninety seconds later")
        return FleetAgent(agent_url=agent_url,
                          cdp_url=os.environ.get("MCP_FLEET_CDP_URL",
                                                 "http://127.0.0.1:9222"),
                          memory_seed=os.environ.get("MCP_FLEET_MEMORY_SEED") or None)
    raise RefusedToMeasure("unknown target %r; use bridge or fleet" % kind)


if __name__ == "__main__":                                   # pragma: no cover
    ap = argparse.ArgumentParser(description="Run every pool once and record the result.")
    ap.add_argument("--target", default="bridge", choices=("bridge", "fleet"))
    ap.add_argument("--pools", default=",".join(POOLS))
    ap.add_argument("--limit", type=int, default=0,
                    help="episodes per pool, for a smoke run")
    ap.add_argument("--out", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    agent = build_agent(args.target)
    pools = tuple(p.strip() for p in args.pools.split(",") if p.strip())

    def progress(row):
        mark = "infra" if row.get("infra_failure") else ("pass" if row.get("success")
                                                         else "FAIL")
        print("  %-5s %-28s %6.1fs" % (mark, row["episode_id"], row.get("latency_s") or 0),
              flush=True)

    print("running %s against %s" % (", ".join(pools), args.target), flush=True)
    result = run_suite(agent, pools=pools, on_result=progress, limit=args.limit)
    text = report(result)
    print("")
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2, default=str)

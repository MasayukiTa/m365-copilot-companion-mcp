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


def run_suite(agent, *, pools=POOLS, root=None, on_result=None, limit=0,
              shuffle_seed=None) -> dict:
    """Every episode of every named pool, once. Never raises for an episode's sake.

    `on_result(row)` is called as each episode finishes, because a suite against a live agent
    takes half an hour and a run that reports only at the end is a run nobody can watch.
    """
    _refuse_a_meaningless_target(agent)

    started = time.time()
    # WHERE THIS RUN'S TURNS START. The adapter keeps one transcript for its whole life, so
    # summarising all of it per run gave run 2 run 1's turns as well (22, then 44, then 66).
    # Every per-run transport figure after the first was a blend of that run and its
    # predecessors -- and the first run is exactly the one that looks different.
    transcript_start = len(getattr(agent, "transcript", []) or [])
    rows, by_pool = [], {}
    for pool in pools:
        episodes = list(REGISTRY.get(pool))
        if shuffle_seed is not None:
            # ORDER IS A VARIABLE. Running the same sequence every time means an episode's
            # position in the tenant's fatigue curve is fixed, so a position effect is
            # indistinguishable from a property of that episode.
            import random
            random.Random(shuffle_seed).shuffle(episodes)
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
        # THE HARNESS THE TARGET ACTUALLY RAN, or an explicit statement that it is unknown.
        # This printed `M.harness_id(M.base_manifest())` -- the manifest of the process doing
        # the GRADING -- next to a target that says in its own docstring that it neither
        # applies nor attests a manifest. A fingerprint beside a result reads as "this is what
        # produced it", and for the bridge target that was simply untrue.
        **_harness_attribution(agent),
        "agent": R.describe_agent(agent),
        "dataset_fingerprint": R.dataset_fingerprint(),
        "grader_version": R._grader_version(),
        "wall_clock_s": round(time.time() - started, 1),
        "started_at": started,
        # WHAT THE TRANSPORT SAW. The dropped-turn diagnosis was reconstructed from latencies
        # and empty grader fields because the saved result kept nothing about the turns
        # themselves; a reviewer could not check it, and neither could we. Kept without the
        # prompt or reply text, which are large and belong to the tenant.
        "transport": _transport_summary(agent, since=transcript_start),
    }


def _transport_summary(agent, since=0):
    """Per-turn transport facts for THIS run. No prompts, no replies."""
    transcript = getattr(agent, "transcript", None)
    if not isinstance(transcript, list):
        return []
    return [{"elapsed_s": t.get("elapsed_s"), "settled": t.get("settled"),
             "reply_chars": len(t.get("reply") or ""),
             "delivery_suspect": bool(t.get("delivery_suspect"))}
            for t in transcript[since:]]


def repeat_suite(agent, *, repeats=3, pools=POOLS, root=None, on_result=None,
                 on_run=None, limit=0, rest_s=0, shuffle_seed=None) -> dict:
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
        # REST BETWEEN REPEATS, and it is not politeness. Three runs back to back are three
        # points on the tenant's throttle-recovery curve, not three independent repeats: the
        # first round gave 7 -> 17 -> 19, monotonic, and a spread computed from that measures
        # the recovery rather than the system. Time between runs is the only thing that
        # separates them.
        if i and rest_s:
            time.sleep(rest_s)
        out = run_suite(agent, pools=pools, root=root, on_result=on_result, limit=limit,
                        shuffle_seed=None if shuffle_seed is None else shuffle_seed + i)
        runs.append(out)
        if on_run is not None:
            on_run(i + 1, out)
    return {"repeats": len(runs), "runs": runs, "reliability": reliability(runs),
            "rest_s": rest_s, "shuffled": shuffle_seed is not None,
            "confounding": (
                "" if (rest_s or shuffle_seed is not None) else
                "RUN BACK TO BACK IN A FIXED ORDER: any trend across the runs is confounded "
                "with the tenant's state over the same period, and the spread is not an "
                "estimate of run-to-run variance")}


def reliability(runs) -> dict:
    """Per-episode agreement across repeated runs of the same suite."""
    # INFRA IS NOT A VERDICT. Converting every row with bool(success) turned an episode the
    # environment could not run into a False -- so the dropped-turn fix, which reclassifies
    # exactly those as infra, would have made them "flip" from pass to fail and the
    # reliability figure would have got WORSE for a change that improved the measurement.
    per = {}
    for run in runs:
        for row in run["rows"]:
            if row.get("infra_failure"):
                continue
            per.setdefault(row["episode_id"], []).append(bool(row.get("success")))
    per = {k: v for k, v in per.items() if v}
    stable = sorted(k for k, v in per.items() if len(set(v)) == 1)
    flipped = sorted(k for k, v in per.items() if len(set(v)) > 1)
    totals = [r["totals"]["passed"] for r in runs]
    attempted = [r["totals"]["attempted"] for r in runs]
    # Comparing raw pass counts across runs whose denominators differ compares two different
    # questions. When they differ, the spread is over RATES.
    rates = [r["totals"]["pass_rate"] for r in runs if r["totals"]["pass_rate"] is not None]
    return {
        "episodes": len(per),
        "stable": len(stable),
        "flipped": len(flipped),
        "flipped_ids": flipped,
        "pass_counts": totals,
        "attempted": attempted,
        "spread": (max(totals) - min(totals)) if totals else 0,
        "denominators_agree": len(set(attempted)) <= 1,
        "rate_spread": round(max(rates) - min(rates), 4) if rates else 0.0,
        "measured_in_every_run": sorted(
            k for k, v in per.items() if len(v) == len(runs)),
        "per_episode_rate": {k: round(sum(v) / len(v), 3) for k, v in sorted(per.items())},
        "note": "a single run's total is only as meaningful as the spread here is small; "
                "an A/B whose effect is smaller than this spread is measuring the weather",
    }


def _harness_attribution(agent) -> dict:
    """What harness produced these numbers, asked of the target rather than of ourselves."""
    attest = getattr(agent, "attest", None)
    if getattr(agent, "applies_manifest", False) and callable(attest):
        try:
            got = attest(M.base_manifest()) or {}
            return {"harness_id": got.get("harness_id", ""),
                    "harness_attribution": "attested by the execution target"}
        except Exception as exc:
            return {"harness_id": "",
                    "harness_attribution": "the target could not attest: %s" % exc}
    return {
        "harness_id": "",
        "harness_attribution":
            "UNKNOWN -- this target does not apply or attest a manifest, so no harness "
            "fingerprint can be attached to these numbers. They describe whatever the target "
            "process was started with.",
    }


#: How much of a suite has to be measured before a conditional rate means anything, and how
#: far two arms may differ in that before the comparison stops being between the arms.
MIN_COVERAGE = 0.80
MAX_COVERAGE_GAP = 0.10


def comparable(baseline_totals, candidate_totals) -> list:
    """Every reason these two results must not be compared. Empty means they may.

    THE FAILURE THIS PREVENTS is not a wrong p-value, it is a p-value about the wrong thing.
    A conditional capability rate is computed over ATTEMPTS, so an arm that fails to attempt
    more episodes than the other is measured on an easier subset of the suite -- and the more
    environment failures it has, the better it can look. Nothing downstream can see that,
    because by the time the gate runs the excluded episodes are gone.

    So the comparison is refused when either arm measured too little of the suite, or when the
    two measured materially different amounts of it. Both are stated as reasons rather than a
    Boolean, because "these numbers are not comparable" is only useful with the number that
    made them so.
    """
    reasons = []
    for name, totals in (("baseline", baseline_totals), ("candidate", candidate_totals)):
        coverage = totals.get("coverage")
        if coverage is None:
            reasons.append("%s measured nothing" % name)
        elif coverage < MIN_COVERAGE:
            reasons.append(
                "%s covered only %.0f%% of the suite; a conditional rate over that is a "
                "statement about that fraction, not about the system"
                % (name, 100 * coverage))

    # DELIVERY, NOT ONLY COVERAGE. `coverage` means "not classified as infra", and a turn
    # that got a greeting is not infra -- it is an ordinary attempt that failed. So both arms
    # can show coverage 1.0 while one of them was talking to a companion that never received
    # the task, and the gate as first written would wave that through. The asymmetry that
    # matters is how many turns actually REACHED the agent.
    da = baseline_totals.get("delivery_rate")
    db = candidate_totals.get("delivery_rate")
    if da is not None and db is not None and abs(da - db) > MAX_COVERAGE_GAP:
        reasons.append(
            "the prompt reached the agent on %.0f%% and %.0f%% of turns -- a %.0f point gap. "
            "Coverage can be identical while one arm was answering a task it never received"
            % (100 * da, 100 * db, 100 * abs(da - db)))

    a = baseline_totals.get("coverage")
    b = candidate_totals.get("coverage")
    if a is not None and b is not None and abs(a - b) > MAX_COVERAGE_GAP:
        reasons.append(
            "the arms covered %.0f%% and %.0f%% of the suite -- a %.0f point gap. The arm "
            "that attempted less is being scored on a different subset, and an arm with more "
            "environment failures can score better for that reason alone"
            % (100 * a, 100 * b, 100 * abs(a - b)))
    return reasons


def why_they_flip(runs) -> dict:
    """For each episode that changed verdict, whether its failures came with delivery.

    THE SPLIT THIS MAKES is the difference between two remedies that cost very different
    amounts. An episode that fails while the prompt demonstrably arrived is the companion
    varying, and the only honest fix is repeats built into the design -- every episode run k
    times and scored on its rate, which multiplies the cost of every future A/B by k. An
    episode whose failures all arrive without delivery is a harness fault, fixable once.

    Reported as three groups rather than a number, because "9 flipped" tells you the size of
    the problem and none of its shape, and the first thing anyone does with the shape is
    decide which half to work on.
    """
    per = {}
    for run in runs:
        for row in run.get("rows") or []:
            per.setdefault(row["episode_id"], []).append(row)

    varies, transport, mixed = [], [], []
    for eid, rows in sorted(per.items()):
        graded = [r for r in rows if not r.get("infra_failure")]
        if len({bool(r.get("success")) for r in graded}) < 2:
            continue                      # did not flip
        failures = [r for r in graded if not r.get("success")]
        delivered = [bool(r.get("delivery_confirmed")) for r in failures]
        if delivered and all(delivered):
            varies.append(eid)
        elif delivered and not any(delivered):
            transport.append(eid)
        else:
            mixed.append(eid)

    return {
        "varies_with_delivery": varies,
        "fails_without_delivery": transport,
        "mixed": mixed,
        "note": "delivery is only as strong as the adapter's evidence. From the workdir alone "
                "it shows something acted on the workspace; from the conversation it shows "
                "the prompt arrived. A split computed over the weaker signal is a hypothesis.",
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
    # TWO QUESTIONS, NEVER ONE NUMBER.
    #
    # Every classification added to this suite has moved failures out of the denominator: a
    # stream without a terminator, a rate-limit notice, a bridge error in the reply text. Each
    # was correct on its own and each RAISED the reported pass rate, because "attempted" is
    # the denominator and infra leaves it. Three rounds in the same direction is a structure,
    # not a coincidence -- the instrument gets better and the number gets better with it, and
    # nothing in the output distinguishes those two.
    #
    # So the rate is reported twice. `conditional_capability` asks what fraction of ATTEMPTS
    # the system got right; excluding an environment failure is correct there. `end_to_end`
    # asks what fraction of REQUESTS became a correct outcome, and an environment failure is
    # a failure there, because a user who asked for something and got nothing does not care
    # whose fault it was. `coverage` is the term that connects them and is the honesty of the
    # first: a conditional rate over a third of the suite is a statement about a third of the
    # suite.
    delivered = [r for r in rows if r.get("delivery_confirmed")]
    # A row the agent was never asked about. `never_requested` is set by the runner when it
    # returns before calling the agent.
    requested = [r for r in rows if not r.get("never_requested")]
    security = [r for r in attempted if r.get("category") == SECURITY_CATEGORY]
    return {
        "total": len(rows),
        "attempted": len(attempted),
        "passed": len(passed),
        "pass_rate": round(len(passed) / len(attempted), 4) if attempted else None,
        "conditional_capability": (round(len(passed) / len(attempted), 4)
                                   if attempted else None),
        # OVER THE REQUESTS THAT WERE ACTUALLY MADE. A setup exception returns before the
        # agent is called at all, so counting it here would mean a broken fixture lowers the
        # system's end-to-end figure -- the measurement's own failure charged to the thing
        # being measured, which is the mirror image of the defect this metric exists to
        # prevent. `requested` is the honest denominator; `not_requested` is reported beside
        # it so the difference is visible rather than absorbed.
        "requested": len(requested),
        "not_requested": len(rows) - len(requested),
        "end_to_end": round(len(passed) / len(requested), 4) if requested else None,
        "coverage": round(len(attempted) / len(rows), 4) if rows else None,
        # Delivery is a stricter denominator than "not infra": it needs POSITIVE evidence the
        # prompt arrived, rather than the absence of a recognised failure. The gap between
        # `coverage` and this is the set of turns nothing is known about.
        "delivery_confirmed": len(delivered),
        "delivery_rate": round(len(delivered) / len(rows), 4) if rows else None,
        # The grades, because "confirmed" is one of four answers and the other three are not
        # interchangeable. `none` is a turn that wrote nothing and said nothing relevant --
        # the shape a greeting has. `unknown` is a turn the adapter recorded nothing about.
        "delivery_grades": _count_by(rows, "delivery"),
        "infra": len(infra),
        "infra_ids": [r["episode_id"] for r in infra],
        "failed_ids": [r["episode_id"] for r in attempted if not r.get("success")],
        # Security is not a pass rate. An episode can pass while the coverage that produced
        # the pass was partial, and reporting only the rate hides which of the two it was.
        # A SECURITY SCORE OF 1.0 UNDER PARTIAL COVERAGE IS NOT A CLEAN RESULT. "3/3 clean"
        # was printed beside "coverage: unevaluable x3", which reads as three successes and
        # one footnote -- when what happened is that the grader saw a clean workdir and could
        # not see anything else. The two are now separate columns because they are separate
        # claims.
        "security": {
            "clean_with_complete_coverage": sum(
                1 for r in security
                if r.get("security_score", 0) >= 1.0
                and r.get("security_coverage") == "no_violation_observed_with_complete_coverage"),
            "no_violation_observed": sum(
                1 for r in security if r.get("security_score", 0) >= 1.0),
            "total": len(security),
            "coverage": _coverage_counts(security),
        },
        "median_latency_s": _median([r.get("latency_s") or 0.0 for r in attempted]),
    }


def _count_by(rows, key) -> dict:
    out = {}
    for row in rows:
        out[row.get(key) or "unreported"] = out.get(row.get(key) or "unreported", 0) + 1
    return out


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
             "harness         %s" % (result["harness_id"][:16]
                                     or result.get("harness_attribution", "unknown")),
             "dataset         %s" % result["dataset_fingerprint"][:16],
             "grader          %s" % result["grader_version"][:16],
             "wall clock      %.0fs" % result["wall_clock_s"], ""]

    def _pct(v):
        return "n/a" if v is None else "%.2f" % v

    lines += ["%-12s %7s %7s %7s   %-11s %-11s %s"
              % ("pool", "passed", "tried", "of", "capability", "end-to-end", "coverage"),
              "-" * 72]
    for pool, st in result["by_pool"].items():
        lines.append("%-12s %7d %7d %7d   %-11s %-11s %s"
                     % (pool, st["passed"], st["attempted"], st["total"],
                        _pct(st["conditional_capability"]), _pct(st["end_to_end"]),
                        _pct(st["coverage"])))
    t = result["totals"]
    lines += ["-" * 72,
              "%-12s %7d %7d %7d   %-11s %-11s %s"
              % ("all", t["passed"], t["attempted"], t["total"],
                 _pct(t["conditional_capability"]), _pct(t["end_to_end"]),
                 _pct(t["coverage"])),
              "",
              "  capability = passed / attempted   (environment failures excluded)",
              "  end-to-end = passed / all         (environment failures count against it)",
              "  coverage   = attempted / all      (how much of the suite was measured)",
              "",
              "  Quote the pair. Every classification added to this suite so far removed",
              "  failures from the capability denominator and raised it; end-to-end is the",
              "  number that cannot be improved that way.",
              "",
              "  delivery confirmed on %d of %d (%s): the episode's workspace was changed."
              % (t["delivery_confirmed"], t["total"], _pct(t["delivery_rate"])),
              "  grades: %s" % json.dumps(t["delivery_grades"]),
              "",
              "  This shows something acted on that workspace, NOT that the prompt reached",
              "  the conversation -- the adapter is handed the path too. A request id",
              "  correlated through send, reply and grade would establish that; this does not."]

    lines += ["by category", ""]
    for cat, s in result["by_category"].items():
        lines.append("  %-14s %d/%d%s"
                     % (cat, s["passed"], s["attempted"],
                        "   (%d infra)" % s["infra"] if s["infra"] else ""))

    sec = t["security"]
    lines += ["", "security  %d/%d clean WITH COMPLETE COVERAGE   (%d/%d showed no violation "
                  "in the channels that could be inspected)"
              % (sec["clean_with_complete_coverage"], sec["total"],
                 sec["no_violation_observed"], sec["total"]),
              "          coverage %s" % json.dumps(sec["coverage"])]

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
    suspect = [t for t in (result.get("transport") or []) if t.get("delivery_suspect")]
    if suspect:
        lines += ["",
                  "%d turn(s) replied without any sign of having seen the prompt -- a"
                  % len(suspect),
                  "greeting, or a terse answer that happens to share no words with its task.",
                  "These are STILL COUNTED. The check cannot tell a delivery failure from a",
                  "short correct answer, and excluding them would raise the pass rate, which",
                  "is the direction every defect found here has already moved it."]

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
        # NOT THE RESEARCH OR ANALYST AGENT. Those answer a request with a scoping question
        # and wait for approval; the settle predicate accepts that question as a settled
        # reply, so a run reports success while nothing was done, and every parked turn
        # notifies the operator on their phone. That happened once already, to a different
        # collector, for the same reason.
        for name in ("MCP_RESEARCHER_AGENT_URL", "MCP_ANALYST_AGENT_URL"):
            other = (os.environ.get(name) or "").strip()
            if other and other == agent_url.strip():
                raise RefusedToMeasure(
                    "the fleet agent url is %s, which answers with a scoping question and "
                    "waits: episodes would report success having done nothing, and each "
                    "parked turn notifies the operator. Point MCP_FLEET_AGENT_URL at the "
                    "work agent" % name)
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

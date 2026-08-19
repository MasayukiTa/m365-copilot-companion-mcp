"""Sections 19-21: the durable record of one episode, and what it refuses to claim.

WHY A SCHEMA AND NOT JUST MORE FIELDS

The pieces already exist and are scattered: the grader returns an outcome, `experiment`
fingerprints the harness, the runner attaches delivery evidence, `provenance` knows what
authority a piece of evidence carried. Nothing puts them in one row, so "what actually ran"
is reassembled by hand from four places every time someone asks -- and reassembly is where
the answer quietly changes.

WHAT MAKES A RECORD WORTH KEEPING

Section 20 lists what an experiment must record to be reconstructable: commit, manifest,
parent, model, task ids, data version, seed, grader version, security policy version, runtime
configuration. `validate` FAILS on a record missing any of them rather than storing it
anyway.

That refusal is the whole point. A record that looks complete but cannot support a re-run is
worse than no record: it invites the re-run, the numbers come out different, and the
difference is attributed to the change under test rather than to the four unrecorded things
that also moved. An absent field is stored as absent -- never as a plausible default, and
never guessed -- so a reader can tell "nobody recorded this" from "this was empty".

SECTION 21, WHICH IS A REFUSAL RATHER THAN A FIELD

"Improved from X to Y" may only be said when the data used to optimise the harness is
distinguishable from the data used to estimate generalisation. Repeatedly inspecting a set
turns it into optimisation feedback, and it stops being held out at that moment -- not at the
moment someone decides to stop calling it that. So each record carries which pool it came
from and how many times that pool has been read, and `may_claim_improvement` answers on the
evidence rather than on intent.

RAW AND COMPACT ARE SEPARATE, as the brief asks. `compact` drops the tool calls and the
per-turn detail and keeps what a comparison needs; the raw trace stays where it was written
and the record points at it. One row per episode in the summary, and the summary is the thing
that gets read.
"""
from __future__ import annotations

import json
import os
import time

#: Everything section 20 says must be reconstructable. Named here rather than checked inline
#: so the list is one thing a reader can compare against the brief.
REPRODUCIBILITY_FIELDS = (
    "git_commit",
    "harness_id",
    "candidate_parent",
    "model",
    "episode_id",
    "pool_version",
    "random_seed",
    "grader_version",
    "security_policy_version",
    "execution_profile",
)

#: Pools whose results may be cited as an estimate of generalisation.
#:
#: `regression` USED TO BE HERE AND WAS WRONG. A regression pool is read on every candidate
#: cycle by definition -- that is what it is for -- so under a one-read rule it is burned at
#: the second candidate and permanently uncitable. Two constants disagreed, and the one that
#: had to give was this: a regression pool detects LOSS against a known baseline; it does not
#: estimate how a change generalises. Only the sealed holdout does that.
GENERALISATION_POOLS = frozenset({"sealed"})

#: How many reads of a held-out pool are tolerated before it stops being held out. ONE. The
#: brief's rule is that repeated inspection converts a set into optimisation feedback, and the
#: conversion happens on the second look, not on the tenth.
MAX_HELDOUT_READS = 1


class RecordError(ValueError):
    """Raised when a record cannot support the claim someone wants to make from it."""


def build(*, episode_id, experiment_id="", task_class="", failure_class=None,
          harness_id="", component_versions=None, execution_profile="",
          git_commit="", candidate_parent="", model="", pool="", pool_version="",
          random_seed=None, grader_version="", security_policy_version="",
          start_state_hash="", end_state_hash="", turn_count=None, tool_calls=None,
          human_interventions=None, security_events=None, provenance_events=None,
          verification=None, outcome=None, latency=None, cost=None,
          raw_trace_path="", ts=None) -> dict:
    """Assemble one episode record. Absent stays absent.

    Nothing here defaults a missing value to something plausible. `git_commit` unknown is
    recorded as "" and fails validation later, which is the behaviour that matters: the
    alternative is a record that passes and cannot be reproduced.
    """
    # ABSENT STAYS ABSENT, WHICH THE FIRST VERSION ONLY CLAIMED. Missing telemetry became
    # `[]` and a missing outcome became `{}`, so "nobody recorded the tool calls" and "this
    # episode made no tool calls" were the same row -- and `compact` then reported a count of
    # zero for both. The containers stay (callers iterate them) and the names of the fields
    # nobody supplied are recorded beside them.
    not_recorded = [name for name, value in (
        ("component_versions", component_versions), ("tool_calls", tool_calls),
        ("human_interventions", human_interventions), ("security_events", security_events),
        ("provenance_events", provenance_events), ("verification", verification),
        ("outcome", outcome), ("latency", latency), ("cost", cost),
        # "turns not recorded" and "zero turns" were one row here too.
        ("turn_count", turn_count), ("raw_trace_path", raw_trace_path or None),
    ) if value is None]
    if ts is None:
        # The build time is not the episode time. Recording it silently would let a record
        # written during a later analysis pass claim to be from the run.
        not_recorded.append("ts")
    return {
        "schema_version": 1,
        "not_recorded": not_recorded,
        "episode_id": episode_id,
        "experiment_id": experiment_id,
        "task_class": task_class,
        "failure_class": failure_class,
        "harness_id": harness_id,
        "component_versions": dict(component_versions or {}),
        "execution_profile": execution_profile,
        "git_commit": git_commit,
        "candidate_parent": candidate_parent,
        "model": model,
        "pool": pool,
        "pool_version": pool_version,
        "random_seed": random_seed,
        "grader_version": grader_version,
        "security_policy_version": security_policy_version,
        "start_state_hash": start_state_hash,
        "end_state_hash": end_state_hash,
        "turn_count": None if turn_count is None else int(turn_count),
        "tool_calls": list(tool_calls or []),
        "human_interventions": list(human_interventions or []),
        "security_events": list(security_events or []),
        "provenance_events": list(provenance_events or []),
        "verification": dict(verification or {}),
        "outcome": dict(outcome or {}),
        "latency": dict(latency or {}),
        "cost": dict(cost or {}),
        # THE RAW TRACE STAYS WHERE IT IS. Inlining a whole transcript makes the summary
        # unreadable and unsearchable, and the brief asks for them kept apart.
        "raw_trace_path": raw_trace_path,
        # NONE, NOT THE BUILD TIME. Stamping `time.time()` and flagging it in `not_recorded`
        # was a fig leaf: no consumer reads the flag, and the dashboard sorts on `ts`
        # unconditionally, so a row built during a later analysis pass sorted as though it
        # ran then. An absent time sorts nowhere, which is the correct amount of nowhere.
        "ts": ts,
    }


def missing_for_reproduction(record) -> list:
    """Which of section 20's fields this record cannot supply."""
    if record is not None and not isinstance(record, dict):
        raise RecordError("a record must be a dict, got %r" % type(record).__name__)
    out = []
    for field in REPRODUCIBILITY_FIELDS:
        value = (record or {}).get(field)
        if field == "random_seed":
            # A seed of 0 is a seed. `if not value` would call it missing, which is the same
            # mistake as treating a clock reading of 0.0 as unset.
            if value is None:
                out.append(field)
            continue
        if value in (None, "", [], {}):
            out.append(field)
    return out


def validate(record, *, what="this record") -> dict:
    """Raise unless the record can support a re-run. Returns it unchanged when it can."""
    if not isinstance(record, dict):
        raise RecordError("%s is not a record" % what)
    gaps = missing_for_reproduction(record)
    if gaps:
        raise RecordError(
            "%s cannot be reproduced: %s not recorded. A record that stores the result "
            "without what produced it invites a re-run whose difference gets attributed to "
            "the change under test rather than to the unrecorded things that also moved"
            % (what, ", ".join(gaps)))
    return record


def compact(record) -> dict:
    """The summary row: what a comparison needs, without the per-turn detail.

    Kept separate from the raw trace deliberately. A summary that carries every tool call is
    a transcript with a header, and the thing people actually read is the summary.
    """
    keep = ("schema_version", "not_recorded", "episode_id", "experiment_id", "task_class",
            "failure_class", "harness_id", "execution_profile", "git_commit",
            "candidate_parent", "model", "pool", "pool_version", "random_seed",
            "grader_version", "security_policy_version", "start_state_hash",
            "end_state_hash", "turn_count", "outcome", "latency", "cost",
            # COMPARISON-LEVEL EVIDENCE, NOT TRANSCRIPT BULK. These were dropped as though
            # they were per-turn detail. `component_versions` is what two harnesses differ by
            # and `verification` is what the run proved; a summary without them cannot answer
            # the question a summary exists for.
            "component_versions", "verification",
            "raw_trace_path", "ts")
    out = {key: record.get(key) for key in keep if key in record}
    # COUNTS, NOT CONTENTS -- except where nothing was recorded, which is None rather than 0.
    # "Telemetry was not collected" and "no events occurred" are different facts, and only one
    # of them is reassuring.
    absent = set(record.get("not_recorded") or [])
    for key in ("tool_calls", "human_interventions", "security_events", "provenance_events"):
        value = record.get(key)
        out[key + "_count"] = (None if key in absent or not isinstance(value, list)
                               else len(value))
    return out


def may_claim_improvement(records, *, pool_reads=None) -> dict:
    """Whether "improved from X to Y" is sayable from these records.

    `pool_reads` maps a pool name to how many times it has been looked at. The brief's rule
    is that repeated inspection converts a held-out set into optimisation feedback, so the
    answer depends on the reading history and not on what the set is called.

    Returns {"may_claim", "reason", "pools"} and never a number. Whether the difference is
    real is a separate question with its own gate; this only answers whether the SENTENCE is
    permitted.
    """
    rows = [r for r in (records or []) if isinstance(r, dict)]
    if not rows:
        return {"may_claim": False, "reason": "no records", "pools": []}

    # A row too broken to store cannot license a sentence. The two refusals have to compose,
    # or the weaker one becomes the way around the stronger.
    unusable = [r for r in rows if not r.get("pool_version") or not r.get("episode_id")]
    if unusable:
        return {"may_claim": False, "pools": sorted({r.get("pool") or "" for r in rows}),
                "reason": ("%d record(s) carry no pool_version or episode_id, so which data "
                           "this number came from cannot be established" % len(unusable))}

    pools = sorted({(r.get("pool") or "").strip().lower() for r in rows})
    reads = {}
    for key, value in dict(pool_reads or {}).items():
        try:
            reads[str(key).strip().lower()] = int(value)
        except (TypeError, ValueError):
            return {"may_claim": False, "pools": pools,
                    "reason": ("the read count for %r is %r, which is not a number of looks; "
                               "a malformed history is refused rather than raised through the "
                               "caller" % (key, value))}
    optimisation = [p for p in pools if p and p not in GENERALISATION_POOLS]
    generalisation = [p for p in pools if p in GENERALISATION_POOLS]

    if not generalisation:
        return {"may_claim": False, "pools": pools,
                "reason": ("every record comes from %s, which is the optimiser's own "
                           "feedback. A number from the data used to tune the harness "
                           "estimates fit, not generalisation"
                           % ", ".join(optimisation or ["an unnamed pool"]))}

    # UNKNOWN HISTORY IS NOT ZERO HISTORY -- asked AFTER the pool question, because for a set
    # with no held-out pool at all the read count is irrelevant and answering with it would
    # bury the stronger reason under a weaker one.
    if pool_reads is None:
        return {"may_claim": False, "pools": pools,
                "reason": ("no reading history was supplied. A held-out set stops being held "
                           "out on the second look, so not knowing how often it was looked at "
                           "is not the same as knowing it was looked at once")}

    burned = [p for p in generalisation if int(reads.get(p, 0)) > MAX_HELDOUT_READS]
    if burned:
        return {"may_claim": False, "pools": pools,
                "reason": ("%s has been read %s times. Repeated inspection turns a set into "
                           "optimisation feedback at the second look, and it stopped being "
                           "held out then -- not when someone decides to stop calling it that"
                           % (", ".join(burned),
                              ", ".join(str(reads.get(p, 0)) for p in burned)))}

    if optimisation:
        return {"may_claim": True, "pools": pools,
                # ORDER MATTERS AND WAS BACKWARDS. The sentence named the held-out pools as
                # "the data used to tune" and the optimiser's pools as the generalisation
                # estimate -- the exact inversion the rule exists to prevent, in the text
                # that explains the rule.
                "reason": ("%s was used to tune; %s estimates generalisation. Say which is "
                           "which alongside the number"
                           % (", ".join(optimisation), ", ".join(generalisation)))}
    return {"may_claim": True, "pools": pools,
            "reason": "%s is held out and has not been over-read" % ", ".join(generalisation)}


def append(path, record) -> None:
    """Append-only, one JSON object per line, as the brief prefers.

    Validated before it is written. A store that accepts unreproducible rows becomes a store
    nobody can cite, and the point of failing here is that the gap is fixed while the run
    that produced it is still around to ask.
    """
    validate(record, what="the record being appended")
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def from_paired_result(result, *, experiment_id, harness_id, candidate_parent,
                       git_commit="", model="", pool_version="", random_seed=None,
                       grader_version="", security_policy_version="",
                       execution_profile="", arm="candidate") -> list:
    """One record per episode, built from what a paired evaluation returns.

    A DELIBERATELY THIN RECORD, and the thinness is the honest part. `paired_evaluate`
    returns id sets and aggregates, not per-episode rows: no latency, no turn count, no state
    hashes, no tool calls. Reaching for those would mean changing the runner, which sits in
    the frozen judge set, for data that can be added later without re-blessing it.

    So every field the caller cannot supply arrives as None and lands in `not_recorded`.
    That is the difference between a record that says "this episode made no tool calls" and
    one that says "nobody recorded them" -- and the whole module exists to keep those apart.

    `arm` distinguishes the two halves of a paired run. Without it both arms produce a record
    per episode id and the pair collapses into what looks like a duplicate.
    """
    part = (result or {}).get("on" if arm == "candidate" else "off") or {}
    resolved = set(part.get("resolved_ids") or [])
    infra = set(part.get("infra_ids") or [])
    out = []
    for episode_id in (result or {}).get("slice_ids") or []:
        out.append(build(
            episode_id="%s:%s" % (arm, episode_id),
            experiment_id=experiment_id,
            harness_id=harness_id,
            candidate_parent=candidate_parent,
            git_commit=git_commit,
            model=model,
            pool=(result or {}).get("pool") or "evolution",
            pool_version=pool_version,
            random_seed=random_seed,
            grader_version=grader_version,
            security_policy_version=security_policy_version,
            execution_profile=execution_profile,
            outcome={
                "functional_success": episode_id in resolved,
                "infra_failure": episode_id in infra,
                # NOT CLAIMED. A paired result carries no per-episode security verdict, and
                # defaulting this to True would report a security pass the run never made.
                "security_success": None,
            },
        ))
    return out

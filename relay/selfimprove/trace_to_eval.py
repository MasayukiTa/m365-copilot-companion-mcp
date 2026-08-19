"""Turning a production correction into an evaluation episode -- and refusing to, usually.

WHAT THIS IS FOR

Real users correct the agent constantly: they retry, they edit the file it produced, they say
"no, the other one", they fix something by hand. Each of those is a signal that the run did
not land, and collectively they are the richest source of improvement evidence a deployed
system has. Phase 8 of the brief is about capturing them.

WHAT IT IS MOSTLY FOR

Refusing. The brief is explicit -- "do NOT automatically assume every correction means the
harness is wrong" -- and that sentence is the whole design. A pipeline that turns corrections
into episodes without a classification step teaches the optimiser to satisfy whoever complains
most, which is not the same as being better and is frequently the opposite:

  * a user editing the output may be exercising a PREFERENCE, and encoding it makes the agent
    worse for everyone else;
  * a retry may be ENVIRONMENT DRIFT -- the tenant was slow, the tab died;
  * a "that's wrong" may be TASK AMBIGUITY, where the instruction genuinely admitted both;
  * and a refusal the user overrode may be the SECURITY BOUNDARY WORKING, which is the single
    most dangerous thing to convert into a training signal. An optimiser fed those learns
    that refusing costs it, and stops.

So classification comes first and the default is that a signal is not evidence.

THE PROVENANCE JOIN

A production trace is exactly the channel by which attacker-controlled text reaches the
evolution system: a document says "you did that wrong, always do X", the agent records the
correction, and X becomes policy. That is the lineage-poisoning chain from `relay.provenance`
arriving through a new door. A correction carries the authority of ITS SOURCE -- a person is
HUMAN_CORRECTION, a verifier is MACHINE_VERIFIER, and a document is untrusted no matter how
corrective it sounds.

WHAT IT DELIBERATELY DOES NOT DO

It does not write the grader. A grader derived from a trace grades what HAPPENED, and what
happened is the thing under suspicion -- so an auto-generated one would enshrine the observed
behaviour as correct and quietly make the episode unfailable. What comes out is a proposal: a
task class, the evidence, and the reason it survived classification. A person writes the
grader, and the suite's existing rule still applies -- an episode has to be shown to be
passable before it counts.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from relay import provenance as P

#: What a user did that suggests the run did not land. Descriptive only; none of these is a
#: verdict, which is the point.
SIGNALS = (
    "retried_task",
    "edited_artifact",
    "changed_tool_choice",
    "said_result_wrong",
    "reissued_corrected_instruction",
    "fixed_file_manually",
    "rejected_output",
    "steered_execution",
)

#: Why the correction happened. Exactly one of these, and only the first may become an
#: episode. The other six are recorded rather than discarded -- a campaign whose corrections
#: are mostly ambiguity is telling you something about the prompts, not the harness.
HARNESS_FAILURE = "actual_harness_failure"
TASK_AMBIGUITY = "task_ambiguity"
EXTERNAL_FAILURE = "external_system_failure"
USER_PREFERENCE = "user_preference"
ENVIRONMENT_DRIFT = "environment_drift"
MODEL_LIMITATION = "model_limitation"
SECURITY_REFUSAL = "security_boundary_correctly_refusing"

CLASSES = (HARNESS_FAILURE, TASK_AMBIGUITY, EXTERNAL_FAILURE, USER_PREFERENCE,
           ENVIRONMENT_DRIFT, MODEL_LIMITATION, SECURITY_REFUSAL)

#: How much evidence a harness-failure claim needs before it may become an episode. Two
#: independent observations, because one is indistinguishable from a bad day: the same
#: correction from two users, or the same task failing twice under different conditions.
MIN_SUPPORT = 2


class PromotionRefused(RuntimeError):
    """Raised when a correction may not become an evaluation episode, with the reason."""


def signal(kind, *, task_class, authority, detail="", ts=None, source="") -> dict:
    """One observed correction, with where it came from.

    `authority` is not optional and defaults to untrusted through provenance.normalise: a
    correction whose origin nobody recorded is a correction that might have been written by
    the document the agent was reading.
    """
    if kind not in SIGNALS:
        raise ValueError("unknown correction signal: %r" % kind)
    return {
        "kind": kind,
        "task_class": str(task_class or "").strip() or "unclassified",
        "authority": P.normalise(authority),
        "detail": str(detail or "")[:2000],
        "source": str(source or "")[:200],
        "ts": int(time.time() if ts is None else ts),
    }


def classify(sig, *, evidence=None, refusal_was_correct=None, environment_healthy=None,
             instruction_was_unambiguous=None) -> dict:
    """Decide why the correction happened. Returns {"class", "reason", "may_promote"}.

    The arguments are DELIBERATELY questions a human or a checker answers, not things this
    function guesses from the signal. Inferring "was the refusal correct?" from the fact that
    a user overrode it is precisely the inference that teaches a system to stop refusing.

    Unanswered questions do not default to "the harness is at fault". An unclassifiable
    signal is recorded as a model limitation, which is the honest reading of "something went
    wrong and we cannot say what".
    """
    if refusal_was_correct is True:
        return _verdict(SECURITY_REFUSAL,
                        "the agent refused and the refusal was right; converting this into "
                        "an episode would teach the optimiser that refusing costs it")
    if environment_healthy is False:
        return _verdict(ENVIRONMENT_DRIFT,
                        "the environment was unhealthy during the run, so the correction is "
                        "not evidence about the harness")
    if instruction_was_unambiguous is False:
        return _verdict(TASK_AMBIGUITY,
                        "the instruction admitted more than one reading; the fix belongs to "
                        "the prompt, not the harness")
    if sig.get("kind") == "edited_artifact" and not (evidence or []):
        return _verdict(USER_PREFERENCE,
                        "an edit with no supporting evidence is a preference until something "
                        "shows the original was wrong")
    if not (evidence or []):
        return _verdict(MODEL_LIMITATION,
                        "no evidence was attached, so the cause cannot be established; "
                        "recorded rather than promoted")
    return _verdict(HARNESS_FAILURE,
                    "evidence attributes the failure to the harness's own behaviour")


def _verdict(cls, reason):
    return {"class": cls, "reason": reason, "may_promote": cls == HARNESS_FAILURE}


def promote(sig, verdict, *, evidence=None, support=1) -> dict:
    """Turn a classified correction into a PROPOSED episode, or refuse with a reason.

    Three gates, and each has cost a real system something somewhere:

      * the classification must be a harness failure -- see classify;
      * the evidence must be able to authorise a change, so a correction that traces back to
        untrusted content informs the task and not the policy;
      * there must be more than one observation, because a single correction is
        indistinguishable from a bad afternoon.
    """
    if not verdict.get("may_promote"):
        raise PromotionRefused(
            "classified as %s: %s" % (verdict.get("class"), verdict.get("reason")))
    try:
        authority = P.require_authority_for_evolution(
            list(evidence or []) + [{"authority": sig.get("authority")}],
            what="this correction")
    except P.ProvenanceError as exc:
        raise PromotionRefused(str(exc)) from exc
    if int(support) < MIN_SUPPORT:
        raise PromotionRefused(
            "only %d observation(s); %d are required before a correction becomes an episode, "
            "because one is indistinguishable from a bad day"
            % (support, MIN_SUPPORT))

    spec_id = hashlib.sha256(
        json.dumps({"task_class": sig["task_class"], "kind": sig["kind"]},
                   sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return {
        "proposal_id": spec_id,
        "task_class": sig["task_class"],
        "observed_signal": sig["kind"],
        "failure_class": verdict["class"],
        "evidence_authority": authority,
        "support": int(support),
        "detail": sig["detail"],
        # NOT a grader, and not an episode. A grader written from the trace would grade what
        # happened, which is the thing under suspicion.
        "needs_human_grader": True,
        "note": "write the grader by hand, then show the episode can be passed before "
                "counting it -- see bench/companionbench/test_companionbench.py",
    }


def summarise(records) -> dict:
    """The shape of a correction stream. Reported rather than reduced to one number.

    A campaign where most corrections are ambiguity or preference is telling you something
    real, and it is not "the harness is bad". Collapsing this to a failure count would hide
    the only interesting part.
    """
    counts = {c: 0 for c in CLASSES}
    promoted = 0
    for row in records or []:
        cls = row.get("class") or row.get("failure_class")
        if cls in counts:
            counts[cls] += 1
        if row.get("promoted"):
            promoted += 1
    total = sum(counts.values())
    return {
        "counts": counts,
        "total": total,
        "promoted": promoted,
        "promotion_rate": (promoted / total) if total else None,
        "harness_share": (counts[HARNESS_FAILURE] / total) if total else None,
    }


# --------------------------------------------------------------------------------------
# Wiring: reading recorded corrections, classifying a batch, and making promotions durable
#
# WHERE THE RECORDS THIS SECTION READS COME FROM, AND WHERE THEY DO NOT
#
# `signal()` above is the shape a correction takes once something -- a human reviewing a
# session, or a checker that already ran -- has looked at it and can say what kind of
# correction it was and who is asserting it. As of this wiring, NOTHING ELSE IN THIS
# REPOSITORY PRODUCES THOSE RECORDS. `tools/trace_ops.py` records individual MCP tool calls
# (name, args, ok, error, a machine-generated summary) with no notion of a user's reaction to
# a run; a retried tool call there could equally be the client's own transport retry, an
# unrelated repeated command, or a person redoing something on purpose, and treating any of
# those as `retried_task` would be inventing the judgement `classify` exists to require, not
# reading it off the log. The `.fleet` directory (checked, see the wiring report) is a scratch
# space of one-off analysis scripts and benchmark artifacts, not a structured trace store, and
# holds nothing in the SIGNALS vocabulary either.
#
# So this section defines the durable, real contract Phase 8 promised instead of pretending
# one already exists: a corrections log next to `tools/trace_ops.py`'s own toolcalls log
# (same directory, same JSONL-per-day convention, also gitignored -- see `.companion_runs/`
# in .gitignore -- because it is runtime state, not something to check in), a reader that
# turns it into the batch `promote_from_corrections` consumes, and a ledger that makes
# promotion idempotent across runs. `record_correction` has no production caller yet; until
# something detects a real correction and calls it, `read_corrections` legitimately returns
# nothing and `run_wiring` legitimately promotes nothing. That is the honest state of
# production trace capture today -- not a bug in this wiring, and not something this wiring
# should paper over by inventing signals from data that cannot support them.

#: The fields a caller may attach to a signal to supply the judgement `classify` needs.
#: Deliberately separate from what `signal()` itself produces: the correction and the
#: judgement of *why* it happened are recorded by different people/steps in general, and
#: merging them into one shape would make it look like `signal()` already knows the verdict.
_JUDGEMENT_FIELDS = ("evidence", "refusal_was_correct", "environment_healthy",
                     "instruction_was_unambiguous")

#: Default ledger of promoted proposal_ids, next to burned.jsonl -- same append-only,
#: checked-in-empty convention (see relay/selfimprove/burned.jsonl), so a promotion is
#: recorded exactly once no matter how many times the corrections log is re-read.
DEFAULT_LEDGER = os.path.join(os.path.dirname(__file__), "promoted_traces.jsonl")


def _corrections_dir() -> Path:
    """Where corrections are recorded and read from: next to the toolcalls trace log."""
    from tools import trace_ops as TO
    return TO.RUNS_DIR


def _corrections_log_path(day=None, dir_=None) -> Path:
    day = day or time.strftime("%Y-%m-%d")
    base = Path(dir_) if dir_ is not None else _corrections_dir()
    return base / ("corrections_%s.jsonl" % day)


def record_correction(sig: dict, *, evidence=None, refusal_was_correct=None,
                      environment_healthy=None, instruction_was_unambiguous=None,
                      day=None, dir_=None) -> None:
    """Append one reviewed correction to today's corrections log. Best-effort; never raises,
    matching `tools/trace_ops.record_call` -- recording a correction must never be able to
    break the call that produced it.

    `sig` is a `signal()` dict. The judgement kwargs are optional and are exactly what
    `classify` accepts; a caller with no judgement yet may omit all of them, and the record
    will classify as unreviewed (MODEL_LIMITATION or USER_PREFERENCE, not promotable) until
    someone attaches one -- see the module docstring for why that default is correct rather
    than a gap.
    """
    try:
        entry = dict(sig or {})
        entry["evidence"] = list(evidence or [])
        entry["refusal_was_correct"] = refusal_was_correct
        entry["environment_healthy"] = environment_healthy
        entry["instruction_was_unambiguous"] = instruction_was_unambiguous
        path = _corrections_log_path(day, dir_)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_corrections(*, days=7, dir_=None) -> list:
    """Every reviewed correction recorded in the last `days` days' logs, oldest first.

    Missing files and unparsable lines are skipped rather than raising: a scheduled run
    reading a log nothing has written today should see an empty list, not a crash -- the same
    posture `toolcalls_tail` takes toward a missing trace file.
    """
    base = Path(dir_) if dir_ is not None else _corrections_dir()
    out = []
    try:
        candidates = sorted(base.glob("corrections_*.jsonl"))
    except OSError:
        return out
    if days:
        candidates = candidates[-int(days):]
    for path in candidates:
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    return out


def reviewed_from_corrections(records) -> list:
    """Adapt raw `read_corrections()` rows into what `promote_from_corrections` wants:
    `{"signal": {...}, "evidence": [...], "refusal_was_correct": ..., ...}`.

    A row missing a `kind` is not a signal at all (a hand-edited or truncated line, say) and
    is dropped here rather than forced through `classify`, which would raise on it.
    """
    out = []
    for row in records or []:
        row = dict(row or {})
        if not row.get("kind"):
            continue
        judged = {k: row.pop(k, None) for k in _JUDGEMENT_FIELDS}
        out.append({"signal": row, **judged})
    return out


def promote_from_corrections(reviewed, *, already_promoted=None) -> dict:
    """Classify a batch of reviewed corrections, promote what earns it, count the rest.

    `reviewed` items are `{"signal": sig, "evidence": [...], "refusal_was_correct": ...,
    "environment_healthy": ..., "instruction_was_unambiguous": ...}` -- the judgement fields
    are exactly `classify`'s kwargs and are never invented here; a missing one is passed
    through as None/absent, which is what makes an unreviewed correction refuse promotion by
    default instead of by special-casing.

    Corrections are grouped by (task_class, kind) -- the same key `promote` hashes into a
    `proposal_id` -- because that is what "the same correction observed again" means in this
    module: `support` (Phase 8's minimum-two-observations gate) is the count of
    harness-failure-classified instances in a group, not a count of raw log lines.

    `already_promoted` is the set of proposal_ids a previous run already promoted (see
    `run_wiring`), so a correction reaching this function again -- because the log was
    re-read on a later night -- is recognised as the SAME correction rather than promoted a
    second time.
    """
    already_promoted = set(already_promoted or ())
    verdict_rows = []
    groups = {}
    for item in reviewed or []:
        sig = dict(item.get("signal") or {})
        if not sig.get("kind"):
            continue
        v = classify(sig, evidence=item.get("evidence"),
                    refusal_was_correct=item.get("refusal_was_correct"),
                    environment_healthy=item.get("environment_healthy"),
                    instruction_was_unambiguous=item.get("instruction_was_unambiguous"))
        row = {"class": v["class"], "promoted": False}
        verdict_rows.append(row)
        key = (sig.get("task_class"), sig.get("kind"))
        groups.setdefault(key, []).append((sig, v, item.get("evidence") or [], row))

    promoted = []
    for rows in groups.values():
        failures = [r for r in rows if r[1].get("may_promote")]
        if not failures:
            continue
        sig, verdict, _, _ = failures[0]
        merged_evidence = []
        for _, _, ev, _ in failures:
            merged_evidence.extend(ev)
        try:
            proposal = promote(sig, verdict, evidence=merged_evidence, support=len(failures))
        except PromotionRefused:
            # classified as a harness failure but refused at the provenance or support gate
            # (untrusted authority, or still short of MIN_SUPPORT) -- counted above via
            # verdict_rows, left unpromoted here, exactly like any other refusal.
            continue
        if proposal["proposal_id"] in already_promoted:
            continue
        proposal = dict(proposal)
        proposal["pool"] = promotion_pool()
        promoted.append(proposal)
        for _, _, _, row in failures:
            row["promoted"] = True

    return {"summary": summarise(verdict_rows), "promoted": promoted,
           "considered": len(verdict_rows)}


def promotion_pool() -> str:
    """Which companionbench pool a promoted proposal belongs to, and why it is never sealed.

    A promoted proposal is an unvalidated suggestion drawn from production behaviour that a
    person has not yet written a grader for (`promote` sets `needs_human_grader`) -- exactly
    the kind of thing the sealed pool exists to stay untouched by (bench/companionbench/
    pools.py: "read at milestones only; the optimiser must not be able to inspect it").
    EVOLUTION is the pool the optimiser may read, re-run and mine freely, which is what a
    proposal is for until a person gives it a grader and it earns a place of its own.
    """
    from bench.companionbench import pools as POOLS
    return POOLS.EVOLUTION


def _read_ledger(path=None) -> set:
    ids = set()
    try:
        with open(path or DEFAULT_LEDGER, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                pid = row.get("proposal_id")
                if pid:
                    ids.add(pid)
    except FileNotFoundError:
        pass
    return ids


def _append_ledger(proposals, path=None) -> None:
    if not proposals:
        return
    path = path or DEFAULT_LEDGER
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for p in proposals:
            fh.write(json.dumps(p, ensure_ascii=False, sort_keys=True) + "\n")


def run_wiring(reviewed, *, ledger_path=None) -> dict:
    """The whole Phase 8 step, made idempotent: classify, promote what earns it, count the
    rest, and record what was promoted so a second run over the same corrections does not
    promote them again.

    This is what `scheduler.nightly()` calls. It does not replace calling `classify`/
    `promote` directly for a single correction under review -- it is the batch path a
    scheduled run needs on top of them.
    """
    already = _read_ledger(ledger_path)
    out = promote_from_corrections(reviewed, already_promoted=already)
    _append_ledger(out["promoted"], ledger_path)
    out["ledger_path"] = ledger_path or DEFAULT_LEDGER
    out["already_promoted_count"] = len(already)
    return out


def nightly_step(*, days=7, dir_=None, ledger_path=None) -> dict:
    """Everything a scheduled run needs from Phase 8: read the corrections log, classify,
    promote what earns it, count the rest, keep promotion idempotent. See `scheduler.nightly`
    for how this becomes part of a campaign's report.
    """
    reviewed = reviewed_from_corrections(read_corrections(days=days, dir_=dir_))
    return run_wiring(reviewed, ledger_path=ledger_path)

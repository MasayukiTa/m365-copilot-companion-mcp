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

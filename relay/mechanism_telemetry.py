"""Record, per turn, what each accuracy mechanism did -- as a staircase, not a boolean.

WHY THIS EXISTS. Two independent reviews of the same measurement reached the same verdict:
the mechanisms in this system have never been given a valid efficacy test, because the thing
they were accepted against was `outcome == DONE` -- a self-report whose precision, once a
grader existed, measured 71.8%.

The usage numbers that prompted it: fan-out 7.9% of rows and all from one batch that then
force-disabled it; the multi-lens panel 4.3%; the security lens seven times ever; best-of-N
one real decision ever; effort arms that resolved the SAME five instances and failed the SAME
one. Reading that as "the mechanisms do not work" is the same error as trusting DONE -- it
takes an unmeasured thing for a measured one.

WHAT MAKES THE DIFFERENCE MEASURABLE. Three states have to be distinguishable, and a single
"fired: true/false" cannot separate them:

    never configured on          the knob is off; the mechanism had no chance
    on but no opportunity        configured, and the situation it exists for did not occur
    fired and changed nothing    it ran, and the decision was the same either way

So each record is a staircase: every step is null unless the step above it is true. A step
that is false says exactly where it stopped, and `absence of a record` means logging failed --
never "it did not fire". That distinction is the whole point.

WHAT IT DOES NOT DO. Instrumentation establishes exposure and mediation, not efficacy. Knowing
a panel fired and changed a decision still does not say the decision was better; only a graded
comparison does. This file exists to make the graded comparison POSSIBLE by carrying the join
key -- self_report_outcome and patch_hash on every turn -- so that joining to the grader is a
join rather than a reconstruction. Reconstructing it after the fact was the most expensive
part of the analysis that led here.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import time

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   ".fleet", "mechanisms.jsonl")

#: Every mechanism that claims to improve accuracy. Named here so a mechanism that never
#: reports is visible as a gap rather than as an absence nobody noticed.
MECHANISMS = ("fanout", "refuter", "panel", "veto", "retry", "bestofn", "skill", "effort")


def patch_hash(text):
    """The join key to the grader. A patch is the artefact that gets graded, so its hash is
    what lets an attempt's telemetry meet its verdict without guessing."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def record(mechanism, *, run_id="", instance="", goal_hash="", attempt=None, turn=None,
           configured=None, config_source="", config_value=None,
           eligible=None, ineligible_reason="",
           triggered=None, not_triggered_reason="",
           executed=None, execution_error="",
           changed_decision=None, before=None, after=None,
           self_report_outcome="", artifact_hash="", extra=None):
    """One staircase record. Never raises: telemetry must not be able to fail a run.

    The arguments are all keyword-only and all default to None, because None means "the step
    above stopped, so this step was never reached" and False means "this step was reached and
    the answer was no". Collapsing those two is how a mechanism that is switched off comes to
    look like one that ran and did nothing.
    """
    if mechanism not in MECHANISMS:
        # Not an error: a new mechanism should appear in the log the day it is written, and
        # the name check exists to make an unregistered one visible, not to drop it.
        pass
    row = {
        "ts": time.time(),
        "mechanism": mechanism,
        "run_id": run_id,
        "instance": instance,
        "goal_hash": goal_hash,
        "attempt": attempt,
        "turn": turn,
        # -- the staircase; each None means "not reached"
        "configured": configured,
        "config_source": config_source or None,
        "config_value": config_value,
        "eligible": eligible,
        "ineligible_reason": ineligible_reason or None,
        "triggered": triggered,
        "not_triggered_reason": not_triggered_reason or None,
        "executed": executed,
        "execution_error": execution_error or None,
        "changed_decision": changed_decision,
        "decision_before": before,
        "decision_after": after,
        # -- the join keys to the grader, on EVERY record
        "self_report_outcome": self_report_outcome or None,
        "artifact_hash": artifact_hash or None,
    }
    if extra:
        row["extra"] = extra
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return row


def load(path=LOG):
    rows = []
    if not os.path.exists(path):
        return rows
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def funnel(rows, mechanism=None):
    """The counts that answer 'did it reach the situation it was built for'.

    Reported as a funnel because the interesting number is where it stops, not the total.
    """
    out = {}
    for m in (MECHANISMS if mechanism is None else (mechanism,)):
        rs = [r for r in rows if r.get("mechanism") == m]
        conf = [r for r in rs if r.get("configured")]
        elig = [r for r in conf if r.get("eligible")]
        trig = [r for r in elig if r.get("triggered")]
        exe = [r for r in trig if r.get("executed")]
        chg = [r for r in exe if r.get("changed_decision")]
        out[m] = {
            "records": len(rs),
            "configured": len(conf),
            "eligible": len(elig),
            "triggered": len(trig),
            "executed": len(exe),
            "changed_decision": len(chg),
            # Where it stops is the finding. A mechanism with configured=0 was never given a
            # chance; one with eligible=0 is solving a problem that does not occur here; one
            # with changed_decision=0 ran and made no difference.
            "stops_at": ("never configured" if not conf else
                         "no opportunity" if not elig else
                         "did not trigger" if not trig else
                         "did not execute" if not exe else
                         "changed nothing" if not chg else
                         "changed decisions"),
        }
    return out

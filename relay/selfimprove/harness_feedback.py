"""What the harness needs, said before someone asks -- and only when it is worth saying.

WHAT PROACTIVE FEEDBACK IS

Phase 6 of the brief: the harness should surface what is limiting it rather than waiting for
a human to go looking. Everything needed is already recorded -- the decision states, the
ledger's prediction accuracy, the QD coverage, the infra share -- and nobody reads any of it
until something goes wrong.

WHY IT IS MOSTLY SILENT

A monitor that speaks every cycle is a monitor nobody reads, and one that speaks only when
the news is bad trains its reader to dread it and eventually to ignore it. Both failure modes
end the same way: the signal arrives and no one is listening.

So each observation here has a THRESHOLD and a specific recommended action. "Pass rate is
73%" is not feedback; "eleven of the last twelve experiments were INCONCLUSIVE, so the slices
are too small to decide anything -- raise N or stop running them" is. If there is nothing
above a threshold, this produces nothing, and that silence means the loop is healthy rather
than that the monitor is broken.

WHAT IT DOES NOT DO

It does not act. Every observation names something a human decides: enlarge a slice, fix the
environment, distrust a proposer, widen the search. A component that both noticed problems
and fixed them would be an optimiser with no judge, which is the arrangement the rest of this
package exists to prevent.
"""
from __future__ import annotations

from collections import Counter

#: A campaign this short says nothing about anything. Reporting shape from three experiments
#: produces confident noise, which is worse than the silence it replaces.
MIN_EXPERIMENTS = 8

#: Above this share, the named condition is the thing to fix before anything else is worth
#: measuring. Chosen to be obviously-too-high rather than finely tuned: a threshold near the
#: normal rate fires constantly and gets muted.
INFRA_SHARE = 0.20
INCONCLUSIVE_SHARE = 0.70
NEEDS_REVIEW_SHARE = 0.30

#: A proposer whose hypotheses almost never survive is generating plausible sentences rather
#: than reasoning about the system. Low is not damning -- most ideas fail -- so this is set
#: where "almost never" begins.
POOR_PREDICTION = 0.10


def observe(*, decisions=None, prediction_accuracy=None, qd_coverage=None,
            sealed_unevaluable=0, security_incomplete=0) -> list:
    """Everything worth saying about the loop right now, most actionable first.

    Returns a list of {"finding", "evidence", "do"} -- empty when nothing crosses a
    threshold, which is the common and correct outcome.
    """
    rows = list(decisions or [])
    out = []

    if len(rows) < MIN_EXPERIMENTS:
        return out

    counts = Counter(d.get("state") if isinstance(d, dict) else str(d) for d in rows)
    total = len(rows)

    infra = counts.get("INFRA_ABORT", 0) / total
    if infra > INFRA_SHARE:
        out.append({
            "finding": "the harness is unwell more often than it is measuring",
            "evidence": "%.0f%% of the last %d experiments aborted on infrastructure"
                        % (infra * 100, total),
            "do": "fix the environment before running more candidates; every abort is a slot "
                  "that produced no evidence, and a campaign at this rate is mostly cost",
        })

    inconclusive = counts.get("INCONCLUSIVE", 0) / total
    if inconclusive > INCONCLUSIVE_SHARE:
        out.append({
            "finding": "the slices are too small to decide anything",
            "evidence": "%.0f%% of the last %d experiments were INCONCLUSIVE"
                        % (inconclusive * 100, total),
            "do": "raise N, or stop running candidates whose expected effect is smaller than "
                  "this suite can resolve -- more experiments at this size buys nothing",
        })

    review = counts.get("NEEDS_HUMAN_REVIEW", 0) / total
    if review > NEEDS_REVIEW_SHARE:
        out.append({
            "finding": "the loop keeps stopping for a human it does not have",
            "evidence": "%.0f%% needed review" % (review * 100),
            "do": "look at WHY: an unconfigured sentinel and incomplete security coverage "
                  "both land here, and both are configuration rather than findings",
        })

    if prediction_accuracy is not None:
        acc = prediction_accuracy.get("keep_rate")
        decided = prediction_accuracy.get("decided") or 0
        if acc is not None and decided >= MIN_EXPERIMENTS and acc < POOR_PREDICTION:
            out.append({
                "finding": "the proposer's hypotheses are not surviving contact",
                "evidence": "%d decided, %.0f%% kept" % (decided, acc * 100),
                "do": "distrust the proposal mechanism before distrusting the candidates -- "
                      "a generator whose predictions never hold is writing plausible "
                      "sentences rather than reasoning about this system",
            })

    if qd_coverage is not None and qd_coverage.get("described", 0) >= MIN_EXPERIMENTS:
        if qd_coverage.get("cells_occupied", 0) <= 2:
            out.append({
                "finding": "the search is exploring one behaviour and turning its dial",
                "evidence": "%d behaviour cells occupied across %d described candidates"
                            % (qd_coverage["cells_occupied"], qd_coverage["described"]),
                "do": "vary a component rather than a parameter; the map is telling you the "
                      "candidates differ in degree and not in kind",
            })

    if sealed_unevaluable:
        out.append({
            "finding": "the holdout has not been run",
            "evidence": "%d evaluations reported the sealed pool unevaluable" % sealed_unevaluable,
            "do": "provide the salt, or stop reading these results as generalisation "
                  "evidence -- an unrun canary is not a passed one",
        })

    if security_incomplete:
        out.append({
            "finding": "security is being reported from evidence that cannot carry it",
            "evidence": "%d evaluations had incomplete security coverage" % security_incomplete,
            "do": "run through an adapter that produces a tool-call trace, or read those "
                  "results as 'no violation observed' rather than 'no violation'",
        })

    return out


def report(observations) -> str:
    """One line per observation, or the sentence that says nothing is wrong."""
    if not observations:
        return ("harness feedback: nothing above threshold. That is the healthy outcome, not "
                "a broken monitor.")
    lines = ["harness feedback: %d observation(s)" % len(observations)]
    for o in observations:
        lines.append("  * %s" % o["finding"])
        lines.append("      evidence: %s" % o["evidence"])
        lines.append("      do:       %s" % o["do"])
    return "\n".join(lines)

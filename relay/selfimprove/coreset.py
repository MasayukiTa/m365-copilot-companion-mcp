"""Phase 9: which past failures are worth re-running, and which are the same failure twice.

WHAT RETROSPECTIVE REPLAY IS FOR

Traces accumulate. Most of what is in them is redundant -- the same failure, met twenty
times, in twenty tasks that differ only in their filenames. Re-running all of it to check
whether a change helped costs twenty slots and answers one question.

So the pipeline the brief describes is: cluster the historical failures, select a diverse and
difficult coreset, replay it against the baseline, and use that as the comparison set. The
value is entirely in the selection -- a coreset that is merely small is a sample, and a
coreset that is merely difficult is a collection of the same hard case.

THE TWO WAYS THIS GOES WRONG

Selecting only the hardest failures produces a set nothing passes, where every candidate
scores zero and the measurement cannot distinguish them. That is the "grader tightened past
the correct answer" mistake wearing different clothes: a set with no headroom ranks
everything equally.

Selecting greedily by difficulty also collapses diversity, because the hardest cases tend to
come from the same cluster. Difficulty has to be sampled ACROSS clusters, not globally, which
is what this does: one from each cluster in turn, hardest first within a cluster, until the
budget is spent.

WHAT IT DELIBERATELY DOES NOT DO

It does not cluster by embedding. The failure signature available here -- the episode's
category, its failure mode, and the grader's stated reason -- is discrete and already
meaningful, and a learned clustering would add a dependency, a training step and a second
thing to be wrong, in exchange for grouping things that are already grouped.
"""
from __future__ import annotations

from collections import defaultdict


def signature(record) -> tuple:
    """What makes two failures the SAME failure.

    Category, failure mode and the normalised head of the grader's reason. Deliberately
    coarse: two failures that differ only in a filename are one failure met twice, and
    treating them as two is how a coreset fills up with duplicates.
    """
    reason = str((record.get("details") or {}).get("reason") or "")
    # the head of the reason before any path, id or number -- those are what differ between
    # instances of the same failure
    head = []
    for token in reason.replace(":", " ").split():
        if any(ch.isdigit() for ch in token) or "/" in token or "\\" in token:
            break
        head.append(token.lower())
        if len(head) >= 6:
            break
    return (record.get("category") or "unknown",
            _mode(record),
            " ".join(head))


def _mode(record) -> str:
    if record.get("infra_failure"):
        return "infra"
    if record.get("security_score", 1.0) < 1.0:
        return "security"
    if record.get("side_effect_score", 1.0) < 1.0:
        return "side_effect"
    return "functional"


def cluster(records) -> dict:
    """{signature: [record, ...]} over the failures only.

    Successes are excluded on purpose. A replay set is for measuring whether a change fixes
    what is broken; padding it with cases everything already passes dilutes the difference it
    is meant to detect.
    """
    out = defaultdict(list)
    for r in records or []:
        if r.get("success"):
            continue
        out[signature(r)].append(r)
    return dict(out)


def difficulty(record) -> float:
    """How hard this case is, from what was recorded. Higher is harder.

    A crude ordering rather than a model: a security failure outranks a side-effect failure
    outranks a functional one, and within a tier a case that took longer is treated as harder.
    An infra failure is NOT difficulty -- it is absence of measurement -- and sorts last.
    """
    if record.get("infra_failure"):
        return -1.0
    tier = {"security": 3.0, "side_effect": 2.0, "functional": 1.0}.get(_mode(record), 1.0)
    latency = min(float(record.get("latency_s") or 0.0), 60.0) / 60.0
    return tier + latency


def select(records, *, budget=12, min_clusters=3) -> dict:
    """A diverse, difficult coreset, plus what was left out and why.

    Round-robin across clusters, hardest first within each: the budget is spent on DIFFERENT
    failures before it is spent on more of one. Returns the set and an account of the
    reduction, because "we replayed 12 of 340" is a claim whose second number matters.
    """
    clusters = cluster(records)
    if not clusters:
        return {"coreset": [], "clusters": 0, "considered": len(records or []),
                "reason": "no failures to replay"}

    ordered = {sig: sorted(rows, key=difficulty, reverse=True)
               for sig, rows in clusters.items()}
    # Clusters are visited most-populous first: a failure met forty times is more of the
    # product's real behaviour than one met once, and should not be crowded out by a
    # long tail of singletons.
    order = sorted(ordered, key=lambda s: (-len(ordered[s]), s))

    coreset, exhausted, i = [], set(), 0
    while len(coreset) < budget and len(exhausted) < len(order):
        sig = order[i % len(order)]
        i += 1
        rows = ordered[sig]
        if not rows:
            exhausted.add(sig)
            continue
        coreset.append(rows.pop(0))

    if len(clusters) < min_clusters:
        note = ("only %d distinct failure(s) in this history; a coreset cannot be more "
                "diverse than the failures it is drawn from" % len(clusters))
    else:
        note = ""

    return {
        "coreset": coreset,
        "clusters": len(clusters),
        "considered": len(records or []),
        "failures": sum(len(v) for v in clusters.values()),
        "note": note,
        # Stated rather than left implicit: a reader who sees 12 cases should be able to see
        # how many were dropped to get there without going back to the source.
        "dropped": sum(len(v) for v in clusters.values()) - len(coreset),
    }


def summarise(clusters) -> list:
    """The failure landscape, most common first. Reported for its own sake.

    A history whose failures are one cluster is telling you something specific -- fix that
    one thing -- and it is invisible in any aggregate pass rate.
    """
    return [{"signature": " / ".join(sig), "count": len(rows)}
            for sig, rows in sorted(clusters.items(), key=lambda kv: -len(kv[1]))]

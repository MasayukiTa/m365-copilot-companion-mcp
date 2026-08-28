"""pass^k: how often a scaffold solves the SAME instance every time it is asked.

WHY A SECOND NUMBER. pass@1 is a per-attempt capability and it is the wrong number to act on
alone, because a scaffold that solves a different 40% of a slice on every run and a scaffold
that solves the same 40% every time report identically. They are opposite findings: one is a
reliable tool with a known limit, the other is a coin. Which one is in front of you is exactly
the thing that decides whether a retry helps, and pass@1 cannot say.

pass^k is that question: the fraction of instances solved in ALL k attempts. It falls as k
rises, always, so it never replaces pass@1 -- reported alone it would make a scaffold that
attempts hard problems look worse than one that refuses them. BOTH, or neither is honest.

WHAT THIS MODULE WILL NOT DO. It will not estimate pass^k from a rate. Deriving pass^k from
pass@1 requires assuming instances are independent and identically hard, and the falseness of
that assumption is the entire reason anyone wants pass^k. With no repeated runs the answer
here is "not measured", and "not measured" is returned as None -- never as 1.0, never as
pass@1 to some power, never as a number at all.
"""
from __future__ import annotations

import math


def wilson(k, n, z=1.96):
    """The interval, because a pass^k over two runs of fifty is a very wide number.

    Printed without one it reads as a measurement; the whole risk with a second headline
    metric is that it gets compared peak-to-peak the way the first one was.
    """
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round((c - m) / d, 4), round((c + m) / d, 4)]

def live(entries):
    """Entries with corrections dropped and replicates kept.

    THE SAME RULE THE TREND USES, and for the same reason. Two rows sharing an id and a
    replicate marker are one measurement made twice, the second replacing a grade known to be
    wrong; counting both would report a grading-host artifact as instability. Two rows sharing
    an id with DIFFERENT replicate markers are two independent measurements of one scaffold,
    which is exactly what a reliability figure is made of.

    Getting this backwards is not a rounding error in either direction: read corrections as
    repeats and a broken grader looks like a flaky scaffold; read repeats as corrections and
    no reliability figure can ever be computed.
    """
    latest = {}
    for i, e in enumerate(entries or []):
        if isinstance(e, dict):
            latest[(e.get("id"), e.get("replicate"))] = i
    return [e for i, e in enumerate(entries or [])
            if isinstance(e, dict) and latest.get((e.get("id"), e.get("replicate"))) == i]


def _resolved_sets(entries):
    """(slice, [resolved set per run]) for every slice measured with per-instance results.

    Rows predating `resolved_ids` carry None and are DROPPED rather than read as an empty set:
    an old row would otherwise contribute k attempts in which nothing was solved, and pass^k
    would fall for every slice that happens to have history.
    """
    by_key = {}
    for e in live(entries):
        ids = e.get("slice_ids")
        res = e.get("resolved_ids")
        if not ids or res is None:
            continue
        # SCOPED TO THE SCAFFOLD, not only to the slice. Grouping by slice alone intersected
        # DIFFERENT genomes measured on the same slice as though they were repeated attempts
        # of one -- so the reported figure was not a reliability of anything, and k was just
        # "how many archive rows mention this slice". A reliability figure that is not scoped
        # to what produced it is a number about nothing.
        by_key.setdefault((e.get("id"), tuple(sorted(ids))), []).append(set(res))
    return by_key


def pass_hat_k(entries):
    """Per-slice reliability, or the reason there is none.

    Returns a list of dicts, one per slice that has at least one usable run. `k` is how many
    times the slice was measured; a slice measured once reports k=1 with pass_hat_k equal to
    its pass@1, and `enough` False -- because pass^1 IS pass@1 and printing it as a
    reliability number invites exactly the reading it cannot support.
    """
    out = []
    for (gid, key), runs in sorted(_resolved_sets(entries).items(),
                                   key=lambda kv: (str(kv[0][0]), kv[0][1])):
        n = len(key)
        k = len(runs)
        if not n:
            continue
        always = set(key)
        ever = set()
        for r in runs:
            always &= r
            ever |= r
        out.append({
            "genome_id": gid,
            "n": n,
            "k": k,
            "enough": k >= 2,
            "pass_hat_k": len(always) / n,
            # THE INTERVAL TRAVELS WITH THE NUMBER. Wilson existed in this module and was
            # attached to nothing, so the headline was rendered bare -- and a bare rate is
            # what invites the peak-to-peak comparison a rate in this repository has already
            # suffered once. At 20/50 the interval spans 0.28-0.54; printing 0.40 alone
            # states a precision the sample does not have.
            #
            # It is an interval over INSTANCES, not over runs: with k=2 or 3 there is almost
            # no information about run-to-run variance, and this number does not pretend to
            # carry any.
            "ci_instances": wilson(len(always), n),
            "pass_any": len(ever) / n,
            "per_run_pass_at_1": [len(r & set(key)) / n for r in runs],
            # The gap between "solved every time" and "solved at least once" IS the
            # instability. Zero means every run solved the same instances.
            "flaky": (len(ever) - len(always)) / n,
        })
    return out


def spread(entries):
    """How far apart repeated measurements of the SAME slice landed.

    Cheaper than pass^k and available from the aggregate alone, so it is worth reporting on
    its own: two runs of one scaffold that disagree by twenty points say the slice is being
    measured badly, whatever the per-instance answers turn out to be.

    A slice measured once has no spread. That is None, not 0.0 -- reporting zero spread for a
    single run states stability that was never observed, which is the specific overclaim this
    file exists to prevent.
    """
    by_slice = {}
    for e in live(entries):
        ids = e.get("slice_ids")
        p = e.get("pass_at_1")
        if not ids or p is None:
            continue
        try:
            # Same scoping as pass^k: two genomes measured on one slice are not two
            # measurements of one thing, and their difference is not a spread.
            by_slice.setdefault((e.get("id"), tuple(sorted(ids))), []).append(float(p))
        except (TypeError, ValueError):
            continue

    out = []
    for (gid, key), rates in sorted(by_slice.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        k = len(rates)
        out.append({
            "genome_id": gid,
            "n": len(key),
            "k": k,
            "rates": sorted(rates),
            "spread": (max(rates) - min(rates)) if k >= 2 else None,
            "mean": (sum(rates) / k) if k else None,
        })
    return out




def summary(entries):
    """What a dashboard should show, including the case where it should show nothing.

    `measured` False is the honest state today and must stay reportable: no slice has been run
    twice with per-instance results, so there is no reliability figure to print. A dashboard
    that renders 'pass^k: 1.00' from one run has invented the finding it was added to check.
    """
    rows = [r for r in pass_hat_k(entries) if r["enough"]]
    return {
        "measured": bool(rows),
        "slices": rows,
        "spread": [s for s in spread(entries) if s["k"] >= 2],
        "why_not": None if rows else
                   "no slice has been measured more than once with per-instance results",
    }

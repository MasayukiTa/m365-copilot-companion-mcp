# -*- coding: utf-8 -*-
"""Sealed-holdout registry and contamination report for genotype benchmarks (slice E7).

WHAT DECISION THIS SERVES. Whether a holdout score can still be trusted. A holdout item that was
ever seen while tuning, generating candidates or developing, or that was spent on more candidate
evaluations than declared, no longer measures generalisation; a holdout list that differs from the
one sealed earlier is a different benchmark. This module only compares data it is handed. It starts
no job, submits nothing to the fleet, opens no network connection, reads no clock and writes no
file. Nothing in production calls it (default off): only scripts/org_genotype_holdout.py, run by
hand, and only to print a verdict.

SEAL. seal() records the sha256 of the sorted, de-duplicated holdout ids plus their count. The ids
themselves are not stored in the seal, so the seal can be shown without revealing the holdout.

EXPOSURE. {"item": str, "genotype": str, "purpose": str, "at": str}. "at" is passed in by the
caller (this module has no clock) and is only echoed back. Purposes:
  evaluation          the item was used to score a candidate genotype (allowed on a holdout item,
                      but counted against the reuse budget, see below)
  dev, tuning, candidate_generation
                      the item informed how candidates are made or tuned
Any other purpose string is treated like a tuning purpose: an unknown use of a holdout item is
assumed to leak it.

VERDICT. state is one of
  unsealed      no usable seal (missing, malformed, or the sealed holdout is empty): nothing can be
                claimed about contamination
  contaminated  at least one finding below
  clean         sealed, and no finding
Findings: digest_mismatch (the current holdout does not hash to the seal), overlap (an id is in
both dev and holdout), leaked (a holdout item has a non-evaluation exposure) and over_budget (a
holdout item was evaluated for more distinct genotypes than the budget allows; the default budget
is 1, i.e. one candidate evaluation per holdout item, "no reuse").
"""
from __future__ import annotations

import hashlib
import json

VERSION = 1
EVALUATION = "evaluation"
DEFAULT_BUDGET = 1
STATES = ("clean", "contaminated", "unsealed")


class HoldoutError(ValueError):
    """The input is not a list of ids, a seal or an exposure list."""


def _ids(value, what):
    if not isinstance(value, (list, tuple)):
        raise HoldoutError("%s must be a list of ids" % what)
    out = set()
    for v in value:
        if not isinstance(v, str) or not v:
            raise HoldoutError("%s must contain only non-empty strings" % what)
        out.add(v)
    return sorted(out)


def digest(holdout_ids):
    """Deterministic sha256 hex of the sorted unique ids (order and duplicates do not matter)."""
    ids = _ids(holdout_ids, "holdout")
    blob = json.dumps(ids, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def seal(holdout_ids):
    """The seal record for a holdout list: version, count and digest (no ids)."""
    ids = _ids(holdout_ids, "holdout")
    return {"version": VERSION, "count": len(ids), "digest": digest(ids)}


def _valid_seal(s):
    return (isinstance(s, dict) and isinstance(s.get("digest"), str) and s["digest"] != ""
            and isinstance(s.get("count"), int) and not isinstance(s.get("count"), bool)
            and s["count"] > 0)


def _exposure(e):
    if not isinstance(e, dict):
        raise HoldoutError("an exposure must be an object")
    for k in ("item", "genotype", "purpose"):
        if not isinstance(e.get(k), str) or not e[k]:
            raise HoldoutError("an exposure needs a non-empty string %r" % k)
    return {"item": e["item"], "genotype": e["genotype"], "purpose": e["purpose"],
            "at": e["at"] if isinstance(e.get("at"), str) else ""}


def record_exposure(exposures, item, genotype, purpose, at):
    """Return a new exposure list with one event appended (the argument is not mutated)."""
    ev = _exposure({"item": item, "genotype": genotype, "purpose": purpose, "at": at})
    return list(exposures or []) + [ev]


def verdict(sealed, dev_ids, holdout_ids, exposures, budget=DEFAULT_BUDGET):
    """The contamination verdict for the current holdout against its seal.

    Returns {"state", "offending_items", "findings", "budget", "sealed_digest",
    "current_digest", "holdout_count"}; findings is {"digest_mismatch": bool, "overlap": [...],
    "leaked": [{"item", "genotypes", "purposes"}], "over_budget": [{"item", "genotypes"}]}.
    """
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise HoldoutError("budget must be an integer >= 1")
    dev = _ids(dev_ids if dev_ids is not None else [], "dev")
    hold = _ids(holdout_ids if holdout_ids is not None else [], "holdout")
    events = [_exposure(e) for e in (exposures or [])]
    hold_set = set(hold)
    current = digest(hold)

    overlap = sorted(set(dev) & hold_set)
    leaked_by, evals_by = {}, {}
    for e in events:
        if e["item"] not in hold_set:
            continue
        if e["purpose"] == EVALUATION:
            evals_by.setdefault(e["item"], set()).add(e["genotype"])
        else:
            rec = leaked_by.setdefault(e["item"], {"genotypes": set(), "purposes": set()})
            rec["genotypes"].add(e["genotype"])
            rec["purposes"].add(e["purpose"])
    leaked = [{"item": i, "genotypes": sorted(r["genotypes"]), "purposes": sorted(r["purposes"])}
              for i, r in sorted(leaked_by.items())]
    over = [{"item": i, "genotypes": sorted(g)}
            for i, g in sorted(evals_by.items()) if len(g) > budget]

    usable = _valid_seal(sealed)
    mismatch = bool(usable and sealed["digest"] != current)
    findings = {"digest_mismatch": mismatch, "overlap": overlap, "leaked": leaked,
                "over_budget": over}
    offending = sorted(set(overlap) | {x["item"] for x in leaked} | {x["item"] for x in over})
    if not usable or not hold:
        state = "unsealed"
    elif mismatch or offending:
        state = "contaminated"
    else:
        state = "clean"
    return {"version": VERSION, "state": state, "offending_items": offending,
            "findings": findings, "budget": budget,
            "sealed_digest": sealed.get("digest") if isinstance(sealed, dict) else None,
            "current_digest": current, "holdout_count": len(hold)}


def to_json(obj):
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, indent=2)

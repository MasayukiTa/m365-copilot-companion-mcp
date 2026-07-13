"""Pure finding-state transitions for resilient review runs."""
from __future__ import annotations

from enum import Enum


class FindingState(str, Enum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    CONTESTED = "contested"
    DISPROVED = "disproved"
    REPRODUCED = "reproduced"
    FIXED = "fixed"
    REGRESSION_VERIFIED = "regression_verified"
    FIX_FAILED = "fix_failed"


def derive_finding_state(producer_present: bool, refuter_verdict: str | None,
                         adjudicator_verdict: str | None,
                         behavioral_verdict: str | None) -> FindingState:
    if not producer_present:
        return FindingState.CANDIDATE

    rv = (refuter_verdict or "").upper()
    av = (adjudicator_verdict or "").upper()
    bv = (behavioral_verdict or "").lower()

    if rv == "UPHELD":
        state = FindingState.CONFIRMED
    elif rv in ("REFUTED", "INCONCLUSIVE", "UNCLEAR"):
        if av == "CONFIRM":
            state = FindingState.CONFIRMED
        elif av == "DISPROVE":
            state = FindingState.DISPROVED
        else:
            state = FindingState.CONTESTED
    else:
        state = FindingState.CANDIDATE

    if state == FindingState.CONFIRMED:
        if bv == "reproduced":
            return FindingState.REPRODUCED
        if bv == "inconclusive":
            return FindingState.CONTESTED
        # not_reproduced alone is not a disproof.
    return state

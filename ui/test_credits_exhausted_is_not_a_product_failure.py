# -*- coding: utf-8 -*-
"""Out-of-credits is a CAPACITY LIMIT, shown in the cockpit with its own plain line and NEVER
counted as a product failure.

The environment running out of Copilot Studio credits is returned as plain prose in place of an
answer (relay/credits_exhausted.py). relay/relay_fleet.py settles it as the outcome
CREDITS_EXHAUSTED, carries the one plain line (credits_exhausted.MESSAGE) as the worker `reason`,
and relay/outcomes.py keeps it OUT of the failure totals. This test pins the matching cockpit
behaviour, which a comment in outcomes.py promised but the C# did not yet do:

1. Theme.CountsAsNeedsAttention -- the ONE shared definition every screen counts through --
   excludes CREDITS_EXHAUSTED exactly as it excludes INFRA_STUCK, so a missing-capacity
   environment is never tallied as needs-attention / a product failure on either screen.
2. The cockpit gives it its own lane (not the red stuck/error recovery surface) and shows the
   worker's `reason` verbatim -- which is credits_exhausted.MESSAGE, the same words the doctor
   path prints -- so the operator reads the one plain line of what to do.
3. It is not retryable in the C# retry mirror (retrying cannot assign capacity).

The C# side is read as source text (WPF, not unit-compilable), in the style of the sibling
ui/test_status_labels_needs_human_only.py.
"""
import io
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with io.open(os.path.join(REPO, rel), encoding="utf-8-sig") as fh:
        return fh.read()


THEME = _read("ui/Theme.cs")
STATE = _read("ui/FleetCockpit.WorkerState.cs")
CARD = _read("ui/FleetCockpit.Card.cs")


# -- 1. the shared needs-attention definition excludes it, like INFRA_STUCK ------------------

def test_credits_exhausted_is_not_counted_as_needs_attention():
    fn = THEME[THEME.index("public static bool CountsAsNeedsAttention("):]
    fn = fn[:fn.index("\n    }") + 6]
    # mirrors the INFRA_STUCK carve-out, in the same function, so both screens agree
    assert 'INFRA_STUCK' in fn
    assert 'CREDITS_EXHAUSTED' in fn
    assert 'return false' in fn
    # the structural guard the sibling test freezes must be untouched
    assert '!= "stuck" && s != "maxturns"' in fn


# -- 2. its own cockpit lane, reason shown verbatim (not the red failure surface) -------------

def test_credits_has_its_own_non_failure_lane_in_the_card():
    # a predicate parallel to IsInfraStuck, keyed on the outcome
    assert 'IsCreditsExhausted' in STATE
    assert 'CREDITS_EXHAUSTED' in STATE
    # the card computes an isCredits flag and routes it OUT of the red attention lane
    assert 'isCredits' in CARD
    # the lane renders the worker reason verbatim (OneLine(reason)) -- the same plain line
    # the doctor path prints; no paraphrase in the cockpit
    assert 'OneLine(reason)' in CARD


def test_credits_is_carved_out_of_operator_attention():
    # IsOperatorAttention must not flag a credits worker (it is not a person's lane here)
    fn = STATE[STATE.index("static bool IsOperatorAttention("):]
    fn = fn[:fn.index("\n    }") + 6]
    assert 'IsCreditsExhausted' in fn


# -- 3. not retryable in the C# mirror -------------------------------------------------------

def test_credits_is_not_in_the_csharp_retryable_set():
    fn = STATE[STATE.index("_retryableOutcomes"):]
    fn = fn[:fn.index(";")]
    assert 'CREDITS_EXHAUSTED' not in fn


# -- 4. the Python contract the cockpit mirrors is real --------------------------------------

def test_python_side_excludes_credits_from_product_failure():
    import sys
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    from relay import outcomes, credits_exhausted
    # the one plain line exists and is what the worker reason carries
    assert credits_exhausted.MESSAGE.strip()
    # excluded from failure totals at zero turns
    assert outcomes.scoring_of("CREDITS_EXHAUSTED", 0) == "excluded"
    assert outcomes.is_retryable("CREDITS_EXHAUSTED") is False

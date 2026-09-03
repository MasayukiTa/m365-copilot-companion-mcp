# -*- coding: utf-8 -*-
"""What a verdict means, defined ONCE.

WHY THIS MODULE EXISTS. "This row is not a measurement" was written five times, as a literal
comparison against the string EVALERR, in five files that each had to be found and changed
together. They were not. Adding NOPATCH to the vocabulary broke four of the five silently, and
in the worst way available: pro_cycle.graded_ids() correctly left a NOPATCH instance
outstanding so the cycle would run it again, while pro_grade_remote.ingest() counted that same
row as a verdict already held and DISCARDED the real one when it arrived. The instance would
have been re-run for ever and never recorded.

That is the identical defect aa32af8 fixed for EVALERR, reintroduced within the hour by adding
a second value to a rule that lived in five copies. A sixth copy would have been the same
mistake again, so the rule now has one home and the copies import it.

THE VOCABULARY:

    RESOLVED   an evaluation ran; the patch fixed the bug.
    not        an evaluation ran; it did not.
    EVALERR    no evaluation ran -- no image, no host, a read-only disk. Says nothing about
               the patch.
    NOPATCH    there was nothing to evaluate, because no patch was produced.

Only the first two are measurements. The other two must never enter a resolve rate, and must
never retire an instance, because retiring one on a non-measurement is how work is dropped
without anyone deciding to drop it.
"""
from __future__ import annotations

#: Verdicts that are NOT a statement about a patch. Compare through is_measurement(); the set is
#: exported for the readers that need to name it, not so it can be copied.
NOT_A_MEASUREMENT = frozenset({"EVALERR", "NOPATCH", ""})


def normalise(verdict) -> str:
    """A verdict as a comparable string. None, False and 0 all become "" rather than raising.

    A BOOLEAN IS A GRADE. Some producers write {instance_id: bool}, and `str(v or "")` collapses
    False to "" -- which is in NOT_A_MEASUREMENT -- so an instance that was graded and did not
    resolve read as never graded and was re-run. That is a benchmark re-rolling its failures,
    which is the drift this whole vocabulary exists to prevent.
    """
    if isinstance(verdict, bool):
        return "RESOLVED" if verdict else "not"
    return str(verdict or "").strip().upper()


def is_measurement(verdict) -> bool:
    """Whether this verdict says something about a patch, and may therefore be counted."""
    return normalise(verdict) not in NOT_A_MEASUREMENT


def is_resolved(verdict) -> bool:
    return normalise(verdict) == "RESOLVED"

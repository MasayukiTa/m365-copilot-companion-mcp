# -*- coding: utf-8 -*-
"""Every knob `transport_policy.evolvable_fields()` declares must change an answer.

THE DEFECT. `evolvable_fields()` returned `transport_explore_rate`, and a scan over
`git ls-files` found that name in exactly one place: the return tuple itself. Nothing read it.
The version table twelve lines above says what that is -- *"until this existed, `transport` was
a name a genome could carry and nothing read -- the defect this repository has now found in four
separate components"* -- so this was the fifth, inside the fix for the first four.

WHY THE GUARD THAT EXISTS DID NOT SEE IT. `relay/selfimprove/test_parameters_have_effect.py`
asks exactly this question and asks it of `manifest.DEFAULT_PARAMETERS`. Neither transport knob
is declared there. Its docstring states the stake better than this one can:

    不活性な座標は欠けている座標より悪い -- ループはそれを調整し、ノイズを測り、KEEP しうる。

An inactive coordinate is worse than a missing one, because the loop tunes it, measures noise,
and may keep it.

SO THIS ASKS THE SAME QUESTION OF WHATEVER `evolvable_fields()` HAPPENS TO RETURN, rather than of
a list written out here. A list would have to be kept in step with the declaration by hand, which
is the failure one level up.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import transport_policy as TP  # noqa: E402

#: Values to try for a knob whose type we do not know. A knob that changes nothing across all of
#: these, for any version and any kind, is inactive -- which is the only verdict this test makes.
_CANDIDATES = (None, [], ["code"], ["doc"], ["code", "doc"], 0, 1, 0.0, 1.0, True, False, "")
_KINDS = ("", "code", "doc", "route", "unknown")


def _answers(field, value):
    """Every answer the policy versions give for one knob value, across kinds and explore."""
    out = []
    for name, impl in sorted(TP.TRANSPORT_VERSIONS.items()):
        for kind in _KINDS:
            for explore in (False, True):
                try:
                    got = impl("a goal", kind=kind, knobs={field: value}, explore=explore)
                except Exception as exc:                     # a knob that crashes is not inert
                    got = "raised:%s" % type(exc).__name__
                out.append((name, kind, explore, got))
    return tuple(out)


def test_every_declared_knob_changes_an_answer():
    """Change the value, and something observable changes. That is the whole contract."""
    inert = []
    for field in TP.evolvable_fields():
        seen = {_answers(field, v) for v in _CANDIDATES}
        if len(seen) < 2:
            inert.append(field)
    assert not inert, (
        "declared as evolvable and changes nothing, for any value, version or kind: %s. "
        "A genome that moves one of these produces an arm identical to its control, and the "
        "loop scores it as a result." % (inert,))


def test_the_probe_can_tell_an_active_knob_from_an_inert_one():
    """A test that passes because its instrument is blunt is worse than no test.

    `transport_eligible_kinds` IS active, so the probe above must separate it from a name that
    no policy has ever heard of. If both look the same, the probe is measuring nothing."""
    active = {_answers(TP.ELIGIBLE_KINDS, v) for v in _CANDIDATES}
    invented = {_answers("a_knob_no_policy_reads", v) for v in _CANDIDATES}
    assert len(active) >= 2, "the probe cannot see the one knob that is known to work"
    assert len(invented) == 1, "the probe reports a difference for a knob nothing reads"


def test_the_knob_is_spelled_once():
    """`_policy_v2` read the string, `evolvable_fields` returned the string, and nothing tied
    them together -- "written twice, these drift", which is the argument patch_hash makes about
    itself. The declaration is now the name the policy reads."""
    assert TP.ELIGIBLE_KINDS in TP.evolvable_fields()
    assert TP.choose("g", kind="doc", knobs={TP.ELIGIBLE_KINDS: ["doc"]}) in (TP.SOCKET, TP.TAB)


def test_the_retired_knob_does_not_come_back_without_a_reader():
    """It was declared and read by nothing. If it returns, it returns with a reader -- which
    the test above will confirm, and this one names so a re-add is a deliberate act."""
    assert "transport_explore_rate" not in TP.evolvable_fields()


def test_a_knob_of_the_wrong_type_still_raises_and_that_is_recorded_not_fixed():
    """MEASURED WHILE WRITING THE PROBE ABOVE, and deliberately left standing.

    `_policy_v2` does `kind not in set(eligible)`, so a `transport_eligible_kinds` that is not
    iterable raises TypeError out of a transport decision -- and "a policy that can crash the
    caller is worse than no policy" is this repository's own rule, from fleet_toolset.

    IT IS NOT REACHABLE TODAY: the one production call site passes no knobs at all, and the
    genome path that would pass them has no driver. And the ROOT of it is not a missing
    try/except -- it is that `evolvable_fields()` declares names without types, where
    `manifest.PARAMETER_TYPES` declares (low, high) for every parameter it owns. Wrapping the
    comparison would hide the disagreement between the two declarations instead of settling it.
    Recorded in docs/unreached_burndown.md alongside that disagreement; asserted here so the
    behaviour is a known one rather than a surprise to whoever wires the loop.
    """
    import pytest

    with pytest.raises(TypeError):
        TP._policy_v2("a goal", kind="code", knobs={TP.ELIGIBLE_KINDS: 0})


# ── the genome's side of the same rule ────────────────────────────────────────────────────

def test_a_knob_no_policy_reads_is_recorded_rather_than_ignored(monkeypatch, tmp_path):
    """Nothing checked the genome's side: a campaign could set any key and the policy would
    quietly ignore it while the run was scored as a variant.

    RECORDED, NOT REFUSED -- dropping the knob would change what the policy sees, and raising
    would let a bookkeeping mistake fail a run."""
    from relay import invariants as INV

    path = tmp_path / "invariants.jsonl"
    monkeypatch.setattr(INV, "LOG", str(path), raising=False)
    TP.choose("a goal", kind="code", knobs={"a_knob_no_policy_reads": 3})
    body = path.read_text(encoding="utf-8") if path.exists() else ""
    assert "transport_knob_is_declared" in body, body[:400]
    assert "a_knob_no_policy_reads" in body


def test_a_declared_knob_records_nothing(monkeypatch, tmp_path):
    from relay import invariants as INV

    path = tmp_path / "invariants.jsonl"
    monkeypatch.setattr(INV, "LOG", str(path), raising=False)
    TP.choose("a goal", kind="code", knobs={TP.ELIGIBLE_KINDS: ["code"]})
    assert not path.exists() or path.read_text(encoding="utf-8").strip() == ""


def test_a_transport_decision_never_fails_over_its_own_bookkeeping(monkeypatch):
    """A policy that can crash the caller is worse than no policy -- fleet_toolset's words."""
    def boom(*a, **k):
        raise RuntimeError("ledger is gone")

    monkeypatch.setattr(TP._invariants, "assert_invariant", boom, raising=False)
    assert TP.choose("a goal", kind="code", knobs={"nope": 1}) in (TP.SOCKET, TP.TAB)

"""The coordinate sweep: what it proposes, what it refuses to propose, and what wins.

The sweep's job is choosing what to TRY. Nothing here can keep anything -- every candidate
goes through the controller and its gates -- so these tests are about the quality of the
search rather than the safety of the outcome, with one exception: a sweep that carries
unproven changes forward as winners turns noise into a lineage, and that is tested.
"""
from __future__ import annotations

import pytest

from relay import provenance as PROV
from relay.selfimprove import campaign as C
from relay.selfimprove import manifest as M


class _Agent:
    """An agent that reads only the memory coordinate, like the in-process target."""
    covered_fields = frozenset({"components.memory", "parameters.memory_max_items"})


def test_a_variant_that_would_be_refused_is_never_generated():
    """apply_genome が弾く候補を作れば、評価枠1つ分をかけて照会の答えを知るだけ。"""
    for coord in C.coordinates():
        for genome in C.variants_for(coord):
            M.apply_genome(M.base_manifest(), genome)     # must not raise


def test_no_variant_repeats_the_current_value():
    """現在値との A/A は同じプログラムを2回走らせること。"""
    base = M.base_manifest()
    for coord in C.coordinates():
        for genome in C.variants_for(coord, base):
            assert M.diff(base, M.apply_genome(base, genome)), coord


def test_coordinates_are_filtered_by_what_the_target_can_exercise():
    """最初の実走で13候補中8つが INFRA_ABORT になった -- in_process が読まない
    フィールドだったので契約が正しく拒否した。拒否は正常、生成したのが無駄。"""
    all_coords = C.coordinates()
    assert "max_retries" in all_coords
    narrowed = C.coordinates(_Agent())
    assert "max_retries" not in narrowed
    assert "memory" in narrowed and "memory_max_items" in narrowed


def test_components_are_swept_before_parameters():
    """つまみは仕組みの上に乗る。仕組みを決める前に回すと、古い仕組みを測ることになる。"""
    coords = C.coordinates()
    assert coords.index("memory") < coords.index("memory_max_items")


def test_an_unknown_coordinate_is_refused():
    with pytest.raises(ValueError):
        C.variants_for("something_nobody_implemented")


# ---- the sweep itself, with a stub controller -------------------------------------------

class _StubController:
    """Records what it was asked to run and returns the states it was scripted with."""

    def __init__(self, states):
        self.states = list(states)
        self.calls = []

    def run_candidate(self, *, genome, hypothesis, target_failure_class, evaluate,
                      evidence=None, base=None):
        # The real controller refuses a proposal that cites nothing. A double that accepted
        # one would let the sweep stop passing evidence without a test noticing.
        assert evidence, "sweep が証拠なしで候補を出している"
        self.calls.append({"genome": genome, "hypothesis": hypothesis,
                           "coord": target_failure_class})
        state = self.states.pop(0) if self.states else "REJECT"
        return {"decision": {"state": state, "reason": "stub"}}


def test_only_a_keep_wins_a_coordinate():
    """INCONCLUSIVE が勝てば、証明されていない変更が系統に積み上がる。
    それは進歩ではなくノイズの蓄積。"""
    ctl = _StubController(["INCONCLUSIVE"])
    out = C.sweep(ctl, evaluate=lambda *a, **k: {}, coords=["memory"])
    assert out["winners"] == {}
    ctl = _StubController(["KEEP"])
    out = C.sweep(ctl, evaluate=lambda *a, **k: {}, coords=["memory"])
    assert out["winners"] == {"memory": {"components": {"memory": "memory/v2"}}}


def test_every_candidate_carries_a_hypothesis():
    """台帳は仮説の無い提案を拒否する。sweep がそれを作れなければ、
    最初の候補で campaign が止まる。"""
    ctl = _StubController([])
    C.sweep(ctl, evaluate=lambda *a, **k: {}, coords=["memory"])
    assert ctl.calls and ctl.calls[0]["hypothesis"].strip()
    assert "memory" in ctl.calls[0]["hypothesis"]


def test_the_combination_is_a_candidate_and_not_an_installation():
    """個別の勝者は合成できるとは限らない。誰も走らせていない組み合わせを
    『既知最良』として記録したら、測定の置き場に推測を書くことになる。"""
    winners = {"memory": {"components": {"memory": "memory/v2"}},
               "memory_max_items": {"parameters": {"memory_max_items": 9}}}
    combined = C._combine(winners)
    assert combined == {"components": {"memory": "memory/v2"},
                        "parameters": {"memory_max_items": 9}}

    # AND IT IS RUN. This assertion used to be its opposite -- it checked that the sweep did
    # NOT evaluate the combination -- which fixed in place the one thing wrong with the
    # phase: the genome most likely to be adopted was the only one nobody had measured.
    # memory has one variant, memory_max_items has four; the combination is the sixth call.
    ctl = _StubController(["KEEP"] + ["INCONCLUSIVE"] * 3 + ["KEEP", "KEEP"])
    out = C.sweep(ctl, evaluate=lambda *a, **k: {},
                  coords=["memory", "memory_max_items"])
    assert out["combined"] is not None
    assert out["combined_decision"]["state"] == "KEEP"
    assert ctl.calls[-1]["coord"] == "combined"
    assert ctl.calls[-1]["genome"] == out["combined"], "合成genomeが候補として走っていない"


def test_a_combination_whose_parts_won_but_whose_whole_did_not_is_dropped():
    """個別に効いた2つが互いを打ち消すのは、まさにありふれた結果。
    部品が勝ったことを根拠に合成を採るなら、測っていないものを採っている。"""
    ctl = _StubController(["KEEP"] + ["INCONCLUSIVE"] * 3 + ["KEEP", "INCONCLUSIVE"])
    out = C.sweep(ctl, evaluate=lambda *a, **k: {},
                  coords=["memory", "memory_max_items"])
    assert len(out["winners"]) == 2
    assert out["combined"] is None
    assert out["combined_decision"]["state"] == "INCONCLUSIVE"


def test_a_single_winner_produces_no_combination():
    ctl = _StubController(["KEEP"])
    out = C.sweep(ctl, evaluate=lambda *a, **k: {}, coords=["memory"])
    assert out["combined"] is None

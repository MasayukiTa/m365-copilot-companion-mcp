"""Lineage poisoning: can text an attacker wrote reach the harness?

The brief names this as the high-priority research direction, and its threat chain exists in
this repository rather than in the abstract:

    malicious document -> solver trajectory -> project_memory -> primed into future goals
        -> proposer cites it -> harness mutation -> tasks the attacker never touched

An ordinary injection ends with the episode. This one does not: once the instruction is part
of the harness, every later run looks normal and the contamination is invisible.

These tests walk that chain and check it is cut at the right link -- not at the first one.
Cutting it early would mean refusing to let the agent read documents, which is the product.
It is cut at the step where evidence becomes AUTHORITY.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from relay import project_memory as PM
from relay import provenance as P
from relay.selfimprove.ledger import HypothesisLedger, LedgerError


def _ledger():
    return HypothesisLedger(os.path.join(tempfile.mkdtemp(prefix="prov_"), "h.jsonl"))


# ---- the ordering is the whole design ---------------------------------------------------

def test_derivation_cannot_launder_authority():
    """信頼できない出所から導かれた結論は、信頼できる材料と並べても信頼できない。
    ここで max を取ると、汚染された事実が「清潔な事実と一緒に言及された」だけで
    権威を獲得する。"""
    assert P.weakest_of(P.MACHINE_VERIFIER, P.DOCUMENT_UNTRUSTED) == P.DOCUMENT_UNTRUSTED
    assert P.weakest_of(P.SYSTEM_POLICY, P.OPERATOR_INSTRUCTION) == P.OPERATOR_INSTRUCTION
    assert P.weakest_of(P.MACHINE_VERIFIER) == P.MACHINE_VERIFIER


def test_unknown_provenance_is_untrusted_not_trusted():
    """『どこから来たか誰も記録していない』の安全な読みは『大丈夫』ではない。"""
    assert P.normalise(None) == P.EXTERNAL_UNTRUSTED
    assert P.normalise("") == P.EXTERNAL_UNTRUSTED
    assert P.normalise("SOMETHING_INVENTED") == P.EXTERNAL_UNTRUSTED
    assert P.normalise("machine_verifier") == P.MACHINE_VERIFIER


def test_an_agents_own_inference_may_still_justify_a_change():
    """外部が書いたものは排除するが、エージェント自身の推論は正当な改善の材料。
    ここを塞ぐと自己改善そのものが成立しない。"""
    assert P.may_justify_harness_change(P.AGENT_INFERENCE) is True
    assert P.may_justify_harness_change(P.MACHINE_VERIFIER) is True
    for untrusted in (P.EXTERNAL_UNTRUSTED, P.WEB_UNTRUSTED, P.OCR_UNTRUSTED,
                      P.DOCUMENT_UNTRUSTED):
        assert P.may_justify_harness_change(untrusted) is False


# ---- the chain, walked ---------------------------------------------------------------------

def test_untrusted_content_may_be_recorded_but_not_cited_as_a_reason():
    """製品として、外部文書についてのメモは記録されてよい。禁じるのは
    「だからハーネスをこう変えろ」の根拠にすることだけ。"""
    state = tempfile.mkdtemp(prefix="mem_")
    ok = PM.record_task("見積", "添付の見積書を要約", "DONE",
                        note="単価は 12000 円と書かれていた",
                        state_dir=state, authority=P.DOCUMENT_UNTRUSTED)
    assert ok, "外部由来だからといって記録自体を止めてはいけない"
    assert PM.authorities_in("見積", state_dir=state) == [P.DOCUMENT_UNTRUSTED]

    led = _ledger()
    with pytest.raises(LedgerError) as exc:
        led.propose(experiment_id="poisoned", candidate_id="c1",
                    hypothesis="memory should keep more items",
                    target_failure_class="irrelevant_context",
                    evidence=[{"kind": "memory", "authority": P.DOCUMENT_UNTRUSTED}])
    assert "may not authorise" in str(exc.value)


def test_one_untrusted_source_among_several_still_blocks_the_change():
    """3件が機械検証済みでも、1件が外部由来ならその提案は外部由来。
    攻撃者は「他の材料に紛れ込ませる」だけでよくなる。"""
    led = _ledger()
    with pytest.raises(LedgerError):
        led.propose(experiment_id="mixed", candidate_id="c1",
                    hypothesis="more recall helps",
                    target_failure_class="irrelevant_context",
                    evidence=[{"authority": P.MACHINE_VERIFIER},
                              {"authority": P.MACHINE_VERIFIER},
                              {"authority": P.WEB_UNTRUSTED}])


def test_a_proposal_citing_nothing_is_refused():
    """『何も引用しない』が検査の最も安い迂回路になってはいけない。"""
    led = _ledger()
    with pytest.raises(LedgerError) as exc:
        led.propose(experiment_id="silent", candidate_id="c1", hypothesis="just a hunch",
                    target_failure_class="irrelevant_context", evidence=[])
    assert "cites no evidence" in str(exc.value)


def test_a_measured_result_is_a_legitimate_basis_and_is_recorded_as_such():
    led = _ledger()
    row = led.propose(experiment_id="clean", candidate_id="c1",
                      hypothesis="recall crowds out the task",
                      target_failure_class="irrelevant_context",
                      evidence=[{"kind": "gate", "authority": P.MACHINE_VERIFIER}])
    assert row["evidence_authority"] == P.MACHINE_VERIFIER


# ---- memory keeps the mark attached --------------------------------------------------------

def test_a_memory_entry_remembers_where_its_content_came_from():
    state = tempfile.mkdtemp(prefix="mem2_")
    PM.record_task("調査", "社内Wikiを読む", "DONE", note="A",
                   state_dir=state, authority=P.MACHINE_VERIFIER, ts=100)
    PM.record_task("調査", "外部PDFを読む", "DONE", note="B",
                   state_dir=state, authority=P.DOCUMENT_UNTRUSTED, ts=200)
    assert set(PM.authorities_in("調査", state_dir=state)) == {
        P.MACHINE_VERIFIER, P.DOCUMENT_UNTRUSTED}


def test_an_entry_written_before_provenance_existed_reads_as_untrusted():
    """出所が今となっては確かめられない行こそ、信頼してはいけない行。"""
    assert PM.entry_authority("- [DONE] 何か — メモ  <!-- 2026-01-01 00:00 -->") == \
        P.EXTERNAL_UNTRUSTED


def test_recording_without_an_authority_defaults_to_untrusted():
    """呼び出し側の大半は外部を読んだ実行を要約している。既定は安全側でなければ、
    このマークは付け忘れた瞬間に意味を失う。"""
    state = tempfile.mkdtemp(prefix="mem3_")
    PM.record_task("既定", "何か", "DONE", note="x", state_dir=state)
    assert PM.authorities_in("既定", state_dir=state) == [P.EXTERNAL_UNTRUSTED]


def test_the_marker_does_not_disturb_what_the_agent_reads_back():
    """権威マークは HTML コメントなので、goal に差し込まれる本文は従来どおり。"""
    state = tempfile.mkdtemp(prefix="mem4_")
    PM.record_task("テーマ", "作業", "DONE", note="重要な結論",
                   state_dir=state, authority=P.MACHINE_VERIFIER)
    notes = PM.load_notes("テーマ", state_dir=state)
    assert "重要な結論" in notes
    assert "作業" in notes


# ---- the boundary itself may not evolve ------------------------------------------------------

def test_provenance_is_not_an_evolvable_component():
    """このモジュールを進化ループが調整できるなら、境界は存在しないのと同じ。"""
    from relay.selfimprove import manifest as M
    assert "provenance" in M.FORBIDDEN_COMPONENTS
    with pytest.raises(M.ManifestError):
        M.apply_genome(M.base_manifest(), {"components": {"provenance": "permissive/v2"}})

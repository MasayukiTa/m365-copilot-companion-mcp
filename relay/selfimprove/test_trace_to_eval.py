"""Production corrections: what becomes an episode, and what must not.

These tests are mostly about REFUSAL, because that is what the pipeline mostly does. The
brief's instruction -- do not assume every correction means the harness is wrong -- is the
design, and each refusal below corresponds to a way an unfiltered pipeline would make the
system worse while appearing to make it better.
"""
from __future__ import annotations

import pytest

from relay import provenance as P
from relay.selfimprove import trace_to_eval as T


def _sig(kind="said_result_wrong", authority=P.HUMAN_CORRECTION, **kw):
    kw.setdefault("task_class", "excel_edit")
    return T.signal(kind, authority=authority, **kw)


def _evidence(authority=P.MACHINE_VERIFIER):
    return [{"kind": "verifier", "authority": authority}]


# ---- the classification, which is the whole point -----------------------------------------

def test_a_correct_refusal_never_becomes_an_episode():
    """最も危険な変換。上書きされた拒否を学習信号にすると、
    オプティマイザは『拒否は損』と学び、拒否しなくなる。"""
    v = T.classify(_sig("rejected_output"), evidence=_evidence(),
                   refusal_was_correct=True)
    assert v["class"] == T.SECURITY_REFUSAL and v["may_promote"] is False
    with pytest.raises(T.PromotionRefused) as exc:
        T.promote(_sig("rejected_output"), v, evidence=_evidence(), support=99)
    assert "refusing costs it" in str(exc.value)


def test_an_unhealthy_environment_is_not_evidence_about_the_harness():
    v = T.classify(_sig("retried_task"), evidence=_evidence(), environment_healthy=False)
    assert v["class"] == T.ENVIRONMENT_DRIFT and v["may_promote"] is False


def test_an_ambiguous_instruction_sends_the_fix_to_the_prompt():
    v = T.classify(_sig("said_result_wrong"), evidence=_evidence(),
                   instruction_was_unambiguous=False)
    assert v["class"] == T.TASK_AMBIGUITY and v["may_promote"] is False


def test_an_unsupported_edit_is_a_preference():
    """根拠なしの編集を『ハーネスの誤り』にすると、一人の好みが全員の既定になる。"""
    v = T.classify(_sig("edited_artifact"), evidence=[])
    assert v["class"] == T.USER_PREFERENCE and v["may_promote"] is False


def test_an_unexplained_correction_is_not_blamed_on_the_harness():
    """『何かおかしかった』の正直な読みは『原因は言えない』であって
    『ハーネスが悪い』ではない。既定が後者だと、全ての訂正が実験になる。"""
    v = T.classify(_sig("retried_task"), evidence=[])
    assert v["class"] == T.MODEL_LIMITATION and v["may_promote"] is False


def test_a_supported_failure_is_classified_as_one():
    v = T.classify(_sig(), evidence=_evidence())
    assert v["class"] == T.HARNESS_FAILURE and v["may_promote"] is True


# ---- the gates on promotion ---------------------------------------------------------------

def test_a_single_observation_is_not_enough():
    v = T.classify(_sig(), evidence=_evidence())
    with pytest.raises(T.PromotionRefused) as exc:
        T.promote(_sig(), v, evidence=_evidence(), support=1)
    assert "bad day" in str(exc.value)


def test_two_observations_promote():
    v = T.classify(_sig(), evidence=_evidence())
    out = T.promote(_sig(), v, evidence=_evidence(), support=2)
    assert out["failure_class"] == T.HARNESS_FAILURE
    assert out["support"] == 2
    assert out["needs_human_grader"] is True


def test_a_correction_traced_to_untrusted_content_cannot_change_policy():
    """本番トレースは、攻撃者の書いた文章が進化システムに入る新しい扉。
    『あなたは間違っている、常にXしろ』と書いた文書が方針になってはいけない。"""
    v = T.classify(_sig(authority=P.DOCUMENT_UNTRUSTED), evidence=_evidence())
    with pytest.raises(T.PromotionRefused) as exc:
        T.promote(_sig(authority=P.DOCUMENT_UNTRUSTED), v,
                  evidence=_evidence(), support=5)
    assert "may not authorise" in str(exc.value)


def test_a_correction_with_no_recorded_origin_is_untrusted():
    v = T.classify(_sig(authority=None), evidence=_evidence())
    with pytest.raises(T.PromotionRefused):
        T.promote(_sig(authority=None), v, evidence=_evidence(), support=5)


def test_the_proposal_is_not_an_episode_and_says_so():
    """トレースから生成したグレーダは『起きたこと』を採点する。
    起きたことこそ疑わしいので、それを正解として焼き込んだら不合格にできない課題になる。"""
    v = T.classify(_sig(), evidence=_evidence())
    out = T.promote(_sig(), v, evidence=_evidence(), support=3)
    assert out["needs_human_grader"] is True
    assert "write the grader by hand" in out["note"]


# ---- reporting -------------------------------------------------------------------------------

def test_the_summary_keeps_the_shape_rather_than_a_score():
    """訂正の大半が曖昧さや好みなら、それは実在の情報であって
    『ハーネスが悪い』ではない。1つの数字に潰すと、その情報だけが消える。"""
    rows = [{"class": T.TASK_AMBIGUITY}, {"class": T.TASK_AMBIGUITY},
            {"class": T.USER_PREFERENCE},
            {"class": T.HARNESS_FAILURE, "promoted": True}]
    s = T.summarise(rows)
    assert s["total"] == 4 and s["promoted"] == 1
    assert s["counts"][T.TASK_AMBIGUITY] == 2
    assert abs(s["harness_share"] - 0.25) < 1e-9


def test_an_unknown_signal_kind_is_refused_at_the_door():
    with pytest.raises(ValueError):
        T.signal("user_seemed_annoyed", task_class="x", authority=P.HUMAN_CORRECTION)

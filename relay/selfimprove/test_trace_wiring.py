"""Phase 8 wired in: reading recorded corrections, classifying a batch, and making
promotion durable across runs -- the part that had no caller before this.

These tests are about the WIRING, not about re-proving `classify`/`promote`'s own gates
(that is `test_trace_to_eval.py`). What matters here: a correction the classifier cannot
promote is COUNTED, not silently dropped; a promoted proposal is tagged for a pool that is
never the sealed holdout; and re-processing the same corrections a second time (a scheduled
run reading the same log again) does not promote the same trace twice.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from bench.companionbench import pools as POOLS
from relay import provenance as P
from relay.selfimprove import trace_to_eval as T


def _tmpdir():
    return tempfile.mkdtemp(prefix="trace_wiring_")


def _reviewed(kind="said_result_wrong", *, task_class="excel_edit",
             authority=P.HUMAN_CORRECTION, evidence=None, **judgement):
    sig = T.signal(kind, task_class=task_class, authority=authority)
    row = {"signal": sig, "evidence": evidence}
    row.update(judgement)
    return row


def _verifier_evidence():
    return [{"kind": "verifier", "authority": P.MACHINE_VERIFIER}]


# ---- (a) an unclassifiable trace is counted, not promoted, and not dropped ----------------

def test_a_trace_with_no_evidence_is_counted_not_promoted():
    """根拠なしの訂正は『モデルの限界』として記録されるが、昇格はされない。
    記録から消えてはいけない -- 消えたら夜間実行のレポートから見えなくなる。"""
    reviewed = [_reviewed("retried_task", evidence=None)]
    out = T.promote_from_corrections(reviewed)
    assert out["promoted"] == []
    assert out["considered"] == 1
    assert out["summary"]["total"] == 1
    assert out["summary"]["counts"][T.MODEL_LIMITATION] == 1
    assert out["summary"]["promoted"] == 0


def test_an_edit_with_no_evidence_is_a_preference_and_is_counted():
    reviewed = [_reviewed("edited_artifact", evidence=None)]
    out = T.promote_from_corrections(reviewed)
    assert out["promoted"] == []
    assert out["summary"]["counts"][T.USER_PREFERENCE] == 1


def test_a_refusal_that_was_correct_is_counted_and_never_promotable():
    """上書きされた拒否は、証拠があっても昇格経路に入らない。"""
    reviewed = [_reviewed("rejected_output", evidence=_verifier_evidence(),
                          refusal_was_correct=True)]
    out = T.promote_from_corrections(reviewed)
    assert out["promoted"] == []
    assert out["summary"]["counts"][T.SECURITY_REFUSAL] == 1


def test_a_single_supported_observation_is_counted_but_not_enough_to_promote():
    """support=1 は promote() 自身の閾値で拒否される -- それも『カウントはされる』側。"""
    reviewed = [_reviewed("said_result_wrong", evidence=_verifier_evidence())]
    out = T.promote_from_corrections(reviewed)
    assert out["promoted"] == []
    assert out["summary"]["counts"][T.HARNESS_FAILURE] == 1
    # classify() said HARNESS_FAILURE (may_promote=True); promote() itself then refused for
    # want of support. The row is still counted as HARNESS_FAILURE by classify's verdict --
    # the refusal happens one gate later, and is not swept back into "unclassifiable".
    assert out["summary"]["promoted"] == 0


def test_a_correction_traced_to_untrusted_content_is_counted_not_promoted():
    """本番トレース中の攻撃者制御文書起源の訂正は、二重観測があっても方針にならない。"""
    reviewed = [_reviewed("said_result_wrong", authority=P.DOCUMENT_UNTRUSTED,
                          evidence=_verifier_evidence())
               for _ in range(3)]
    out = T.promote_from_corrections(reviewed)
    assert out["promoted"] == []
    assert out["summary"]["counts"][T.HARNESS_FAILURE] == 3


def test_an_unknown_row_with_no_kind_is_dropped_by_the_adapter_not_forced_through_classify():
    """kind の無い行(壊れた/手編集された行)は classify に投げて例外にするのではなく、
    そもそも signal として扱わない。"""
    out = T.reviewed_from_corrections([{"task_class": "x"}, {"kind": "retried_task",
                                                              "task_class": "y",
                                                              "authority": P.HUMAN_CORRECTION}])
    assert len(out) == 1
    assert out[0]["signal"]["kind"] == "retried_task"


# ---- (b) a promoted case lands in a pool that is not the sealed holdout -------------------

def test_a_promoted_case_is_tagged_for_the_evolution_pool_never_sealed():
    reviewed = [_reviewed("said_result_wrong", evidence=_verifier_evidence())
               for _ in range(2)]
    out = T.promote_from_corrections(reviewed)
    assert len(out["promoted"]) == 1
    proposal = out["promoted"][0]
    assert proposal["pool"] == POOLS.EVOLUTION
    assert proposal["pool"] != POOLS.SEALED
    assert proposal["needs_human_grader"] is True


def test_promotion_pool_is_never_sealed_by_construction():
    assert T.promotion_pool() == POOLS.EVOLUTION
    assert T.promotion_pool() != POOLS.SEALED


# ---- (c) running the wiring twice does not promote the same trace twice -------------------

def test_running_promote_from_corrections_twice_with_the_ledger_does_not_double_promote():
    reviewed = [_reviewed("said_result_wrong", evidence=_verifier_evidence())
               for _ in range(2)]
    first = T.promote_from_corrections(reviewed)
    assert len(first["promoted"]) == 1
    already = {p["proposal_id"] for p in first["promoted"]}

    second = T.promote_from_corrections(reviewed, already_promoted=already)
    assert second["promoted"] == []
    # still counted the second time -- the corrections were reviewed and classified again,
    # they just did not mint a second proposal.
    assert second["summary"]["counts"][T.HARNESS_FAILURE] == 2


def test_run_wiring_persists_the_ledger_so_a_second_process_also_does_not_double_promote():
    """2回目の呼び出しは別プロセス・別呼び出しのつもりで、in-memory の状態を共有しない。
    冪等性は元帳ファイルで担保される。"""
    ledger_path = os.path.join(_tmpdir(), "promoted_traces.jsonl")
    reviewed = [_reviewed("said_result_wrong", evidence=_verifier_evidence())
               for _ in range(2)]

    first = T.run_wiring(reviewed, ledger_path=ledger_path)
    assert len(first["promoted"]) == 1
    assert os.path.isfile(ledger_path)

    second = T.run_wiring(reviewed, ledger_path=ledger_path)
    assert second["promoted"] == []
    assert second["already_promoted_count"] == 1

    # the ledger itself has exactly one line -- it did not grow on the second run.
    with open(ledger_path, encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    assert len(lines) == 1


def test_a_new_distinct_correction_still_promotes_after_the_ledger_has_one_entry():
    """元帳は『何かが昇格した』ではなく『この提案は既に昇格した』を記録する。
    別の訂正まで止めてはいけない。"""
    ledger_path = os.path.join(_tmpdir(), "promoted_traces.jsonl")
    first_kind = [_reviewed("said_result_wrong", task_class="excel_edit",
                           evidence=_verifier_evidence()) for _ in range(2)]
    T.run_wiring(first_kind, ledger_path=ledger_path)

    second_kind = [_reviewed("fixed_file_manually", task_class="powerpoint_edit",
                            evidence=_verifier_evidence()) for _ in range(2)]
    out = T.run_wiring(second_kind, ledger_path=ledger_path)
    assert len(out["promoted"]) == 1
    assert out["promoted"][0]["task_class"] == "powerpoint_edit"


# ---- the recording/reading round trip ------------------------------------------------------

def test_record_and_read_corrections_round_trip():
    d = _tmpdir()
    sig = T.signal("said_result_wrong", task_class="excel_edit", authority=P.HUMAN_CORRECTION)
    T.record_correction(sig, evidence=_verifier_evidence(), dir_=d, day="2026-08-19")
    rows = T.read_corrections(dir_=d, days=1)
    assert len(rows) == 1
    assert rows[0]["kind"] == "said_result_wrong"
    assert rows[0]["evidence"] == _verifier_evidence()


def test_reading_a_directory_with_no_corrections_log_yet_returns_nothing():
    """production の是正ログがまだ何も書いていない状態は、空リストであって例外ではない。
    Phase 8 が呼び出し元を得た今、書き手がまだいないことは配線の欠陥ではない。"""
    d = _tmpdir()
    assert T.read_corrections(dir_=d) == []
    out = T.nightly_step(dir_=d, ledger_path=os.path.join(d, "ledger.jsonl"))
    assert out["promoted"] == []
    assert out["considered"] == 0


def test_read_corrections_skips_unparsable_lines_rather_than_raising():
    d = _tmpdir()
    path = os.path.join(d, "corrections_2026-08-19.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write(json.dumps({"kind": "retried_task", "task_class": "x",
                             "authority": P.HUMAN_CORRECTION}) + "\n")
    rows = T.read_corrections(dir_=d, days=1)
    assert len(rows) == 1
    assert rows[0]["kind"] == "retried_task"


# ---- nightly() actually calls this, and its output is reachable ---------------------------

def test_nightly_includes_the_trace_to_eval_report(monkeypatch):
    """夜間実行の戻り値に trace_to_eval が現れる -- どこからも呼ばれない段階には戻らない。"""
    from relay.selfimprove import campaign as C
    from relay.selfimprove import frozen as F
    from relay.selfimprove import scheduler as S

    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (True, []))
    monkeypatch.setattr(C, "sweep", lambda *a, **k: {"stub": True})

    d = _tmpdir()
    sig = T.signal("said_result_wrong", task_class="excel_edit", authority=P.HUMAN_CORRECTION)
    T.record_correction(sig, evidence=_verifier_evidence(), dir_=d)
    T.record_correction(sig, evidence=_verifier_evidence(), dir_=d)

    empty_archive = os.path.join(d, "archive_missing.jsonl")
    lock_path = os.path.join(d, "campaign.lock")
    ledger_path = os.path.join(d, "promoted_traces.jsonl")

    out = S.nightly(budget_candidates=1, archive_path=empty_archive, lock_path=lock_path,
                    trace_dir=d, trace_ledger_path=ledger_path,
                    evaluate=lambda *a, **k: {})

    assert out["ran"] is True
    assert "trace_to_eval" in out
    assert len(out["trace_to_eval"]["promoted"]) == 1
    assert out["trace_to_eval"]["promoted"][0]["pool"] == POOLS.EVOLUTION

# -*- coding: utf-8 -*-
"""Failing-first tests for the final-step validity-judge harness (owner task 2026-10-27,
re-scoped 2026-10-30).

OWNER INSTRUCTION (2026-10-27, verbatim): 「データ解析系は、すべて最終でこの判定器を通す
ハーネスを入れ、databricks を圧倒す。これが先週与えたタスクだ。」

WHAT THIS PINS. relay/judge_gate.py is the orchestration seam that makes every DATA-ANALYSIS
goal pass through the frozen validity engine before its final answer is delivered:

  (1) a classifier -- is_data_analysis_goal(goal) -- decides WHICH goals are data analysis
      (DBB/analysis db queries turned into a table / correlation / group comparison), and which
      are NOT (M365 lookups, code changes, prose). Misclassification examples are pinned below.
  (2) a footer builder -- build_judge_footer(...) -- that, for a data-analysis final answer,
      records the result+data hash and fetch time, registers+locks the claim (order recorded),
      runs the engine, and surfaces verdict / reason codes / interval / n on the final answer.
      When the engine CANNOT be run, it must say WHY on the final answer -- never silently omit.
  (3) the refuter must additionally check "did it pass the judge, and if not why" -- pinned by
      test_refuter_has_judge_check.

The verdict engine itself lives OUTSIDE this repo (Desktop/AHA資料_参照一式/証明/公開データ検証/
engine_v1_8c2), is secret, remote-less and byte-identical-locked; this harness only ORCHESTRATES
it and NEVER edits its rules, thresholds or registered claims. No secret data, measurement row
or employee id appears here -- fixtures are synthetic.

These tests are expected to FAIL until relay/judge_gate.py implements the contract.
"""
from __future__ import annotations

import pytest

jg = pytest.importorskip(
    "relay.judge_gate",
    reason="final-step judge harness relay/judge_gate.py not implemented yet",
)
from relay.judge_gate import (build_judge_footer, run_engine_judge,
                              confirm_overlap_fraction, apply_confirm_independence,
                              confirm_independence_note)


# ---- (1) classifier: which goals are data analysis -------------------------

@pytest.mark.parametrize("goal", [
    "analysis dbを照会し、1月の不良率を号機ごとに群镙3較して",
    "DBB からピール強度のデータを取って相関を出して",
    "odbc_query で検査件数を集計して表にする",
    "ワニスごとの引きはがし強度のロット間比較",
])
def test_data_analysis_goals_are_flagged(goal):
    assert jg.is_data_analysis_goal(goal) is True, goal


@pytest.mark.parametrize("goal", [
    "今日の予定を教えて",                     # M365 calendar lookup
    "上司は誰ですか",                         # M365 org lookup
    "relay/fleet_runner.py の small fix",       # code change
    "薄い冊子を書いて（deliverable）",         # prose / writing
])
def test_non_analysis_goals_are_not_flagged(goal):
    assert jg.is_data_analysis_goal(goal) is False, goal


def test_misclassification_examples_are_documented():
    """The module must spell out borderline MISCLASSIFICATION cases so the boundary is a
    decision, not an accident (owner: write the rule and its failure examples)."""
    text = (jg.MISCLASSIFICATION_NOTE or "")
    assert isinstance(text, str) and len(text) >= 80
    # names at least one false-positive and one false-negative shape
    assert "誤検知" in text or "誤判定" in text


def test_empty_or_nontext_goal_is_not_analysis():
    assert jg.is_data_analysis_goal("") is False
    assert jg.is_data_analysis_goal(None) is False
    assert jg.is_data_analysis_goal(123) is False


# ---- (2) footer builder: judge is run, or the reason is surfaced -----------

def test_footer_surfaces_verdict_reasons_interval_n():
    verdict = {
        "id": "SYN1", "verdict": "HOLD", "verdict_ja": "保留",
        "finding": None, "claim_supported": None,
        "reasons": ["D_FAIL_NO_LOCK"], "notes": ["LOT_INDEPENDENCE_NOT_CHECKED"],
        "evidence": {"K": 3, "df": 2, "ci_std": [-0.4, 0.9], "n": 42},
        "hashes": {"input_sha256": "0" * 64},
    }
    footer = build_judge_footer(verdict=verdict, engine_available=True)
    assert "HOLD" in footer
    assert "D_FAIL_NO_LOCK" in footer           # reason code surfaced
    assert "LOT_INDEPENDENCE_NOT_CHECKED" in footer
    assert "K" in footer and "3" in footer       # n / K surfaced
    assert "ci_std" in footer or "区間" in footer   # interval surfaced


def test_footer_when_engine_unavailable_states_reason_not_silent():
    footer = jg.build_judge_footer(
        verdict=None, engine_available=False,
        unavailable_reason="engine root not found: engine_v1_8c2")
    assert footer.strip(), "footer must not be empty when the judge could not run"
    assert "engine root not found" in footer
    # it must be explicit that the judge did NOT run (never silently omitted)
    assert ("判定器" in footer) or ("judge" in footer.lower())


def test_engine_missing_root_reports_reason(tmp_path):
    result = run_engine_judge("SYN1", root=str(tmp_path / "missing"))
    assert result["engine_available"] is False
    assert "engine root not found" in result["unavailable_reason"]


def test_footer_never_claims_proof():
    """Skill (e): a GO/verdict is 'judgeable', never 'proven'/'correct'. The footer must not
    assert mathematical proof or causal proof."""
    verdict = {"id": "SYN2", "verdict": "GO", "verdict_ja": "採用",
               "finding": "EFFECT", "claim_supported": True,
               "reasons": [], "notes": [], "evidence": {"K": 6, "n": 120},
               "hashes": {"input_sha256": "1" * 64}}
    footer = jg.build_judge_footer(verdict=verdict, engine_available=True)
    low = footer
    assert "数学的に証明" not in low
    assert "因果の証明" not in low
    assert "証明された" not in low


# ---- (3) refuter must check the judge was passed ---------------------------

def test_refuter_has_judge_check():
    from relay import refuter
    assert hasattr(refuter, "JUDGE_CHECK_RULE")
    rule = refuter.JUDGE_CHECK_RULE
    assert isinstance(rule, str) and "判定器" in rule
    # it is actually wired into the composed prompt
    prompt = refuter.build_refuter_prompt(
        "analysis dbを照会して不良率を群镙3較する", "完了しました DONE")
    assert "判定器" in prompt


# ---- the relay-loop gate: rework when a data-analysis answer lacks the judge ----

def test_needs_rework_for_analysis_answer_without_judge():
    goal = "analysis dbを照会して不良率を号機ごとに群镙3較する"
    answer = "号機Aの不良率は12%、Bは8%でした。DONE"  # no judge block
    assert jg.needs_judge_rework(goal, answer) is True
    assert jg.judge_block_present(answer) is False


def test_no_rework_once_judge_block_is_present():
    goal = "analysis dbを照会して不良率を群镙3較する"
    verdict = {"id": "SYN1", "verdict": "HOLD", "verdict_ja": "保留",
               "finding": None, "claim_supported": None, "reasons": ["D_FAIL_NO_LOCK"],
               "notes": [], "evidence": {"K": 3, "n": 42, "ci_std": [-0.4, 0.9]},
               "hashes": {"input_sha256": "0" * 64}}
    answer = "結果...。DONE" + jg.build_judge_footer(verdict=verdict, engine_available=True)
    assert jg.judge_block_present(answer) is True
    assert jg.needs_judge_rework(goal, answer) is False


def test_no_rework_when_reason_for_unavailable_is_stated():
    """An honest 'the judge could not run because ...' satisfies the gate; silence does not."""
    goal = "DBB からピール強度を取って相関を出す"
    answer = "相関係数...。DONE" + jg.build_judge_footer(
        verdict=None, engine_available=False, unavailable_reason="未登録ID")
    assert jg.needs_judge_rework(goal, answer) is False


def test_non_analysis_answer_never_needs_rework():
    assert jg.needs_judge_rework("今日の予定を教えて", "9時に会議。DONE") is False


def test_rework_job_names_the_three_steps_and_forbids_fabrication():
    job = jg.rework_job_text()
    assert "validity_harness.record_manifest" in job
    assert "validity_harness.register_and_lock" in job
    assert "判定エンジン" in job
    assert "自作・捏造" in job   # 「自作・捏造しないこと」


# ---- the gate is actually wired into the relay loop (regression guard) --------

def test_relay_loop_calls_the_judge_gate():
    """The candidate-DONE path in copilot_autopilot_relay.py must consult the gate, so a
    data-analysis answer with no judge block is sent back -- not accepted. Pinned by source
    inspection: importing the relay module drags in a browser stack we do not want in a unit
    test, so we assert the call site exists and feeds it the goal and the final response."""
    import os
    here = os.path.dirname(__file__)
    src = open(os.path.join(here, "copilot_autopilot_relay.py"), encoding="utf-8").read()
    assert "from .judge_gate import needs_judge_rework" in src
    assert "needs_judge_rework(goal, resp)" in src
    # and it must gate BEFORE the DONE is finalised (the judge block sits above refuter/outcome)
    i_gate = src.index("needs_judge_rework(goal, resp)")
    i_done = src.index('outcome = "DONE"', i_gate)
    assert i_gate < i_done


# ---- (fixed condition 2) confirmation-data independence: opened-before-registration ----
# A confirmation run must be judged on data fetched AFTER the claim was registered. When a
# share of the confirmation lots (by exposure*response) was already opened by a PRIOR analysis
# before this claim's trusted registration time, the data is not independent: the harness must
# force order_ok false and SAY SO on the footer. These pin that behaviour, and that the screen
# only ever STOPS (true->false), never relaxes.

def test_confirm_overlap_fraction_counts_only_openings_before_registration():
    reg = "2026-11-02T20:56:46+09:00"
    # three confirmation lots; two were opened BEFORE registration, one AFTER.
    confirm_lots = ["A|0", "A|1", "B|0"]
    prior_opened = {
        "A|0": "2026-10-01T00:00:00+09:00",   # before reg -> overlap
        "A|1": "2026-10-02T00:00:00+09:00",   # before reg -> overlap
        "B|0": "2026-11-03T00:00:00+09:00",   # AFTER reg -> not an overlap
    }
    out = confirm_overlap_fraction(confirm_lots, prior_opened, registered_at=reg)
    assert out["n_lots"] == 3
    assert out["n_overlap"] == 2
    assert abs(out["fraction"] - (2 / 3)) < 1e-9
    assert out["overlap_lots"] == ["A|0", "A|1"]


def test_confirm_overlap_fraction_zero_when_all_opened_after_registration():
    reg = "2026-01-01T00:00:00+09:00"
    confirm_lots = ["L1", "L2"]
    prior_opened = [{"lot": "L1", "opened_at": "2026-06-01T00:00:00+09:00"},
                    {"lot": "L2", "opened_at": "2026-06-02T00:00:00+09:00"}]
    out = confirm_overlap_fraction(confirm_lots, prior_opened, registered_at=reg)
    assert out["fraction"] == 0.0 and out["n_overlap"] == 0


def test_confirm_overlap_fraction_empty_is_zero():
    out = confirm_overlap_fraction([], {}, registered_at="2026-01-01T00:00:00+09:00")
    assert out == {"fraction": 0.0, "n_lots": 0, "n_overlap": 0, "overlap_lots": []}


def test_confirm_overlap_triple_lot_keys_are_normalised():
    reg = "2026-11-02T20:56:46+09:00"
    # [exposure, response, lot] triples; the first was opened before registration.
    confirm_lots = [["Ftype", "loss", "w53"], ["Ftype", "loss", "w01"]]
    prior_opened = {"Ftype|loss|w53": "2026-10-20T00:00:00+09:00"}
    out = confirm_overlap_fraction(confirm_lots, prior_opened, registered_at=reg)
    assert out["n_overlap"] == 1 and out["overlap_lots"] == ["Ftype|loss|w53"]


def test_apply_confirm_independence_forces_order_ok_false_when_overlap():
    order = {"order_ok": True, "order_reason": "registration precedes mtime"}
    overlap = {"fraction": 0.5, "n_lots": 4, "n_overlap": 2, "overlap_lots": ["a", "b"]}
    apply_confirm_independence(order, overlap)
    assert order["order_ok"] is False            # stricter: true forced to false
    assert order["confirm_independence"]["independent"] is False
    assert order["confirm_independence"]["opened_before_registration_fraction"] == 0.5
    assert "NOT independent" in order["order_reason"]


def test_apply_confirm_independence_is_stricter_only_never_relaxes():
    # an order already false stays false even with zero overlap; never flipped to true.
    order = {"order_ok": False, "order_reason": "post-hoc"}
    apply_confirm_independence(order, {"fraction": 0.0, "n_lots": 2, "n_overlap": 0,
                                       "overlap_lots": []})
    assert order["order_ok"] is False
    assert order["confirm_independence"]["independent"] is True


def test_apply_confirm_independence_zero_overlap_leaves_true_true():
    order = {"order_ok": True, "order_reason": "ok"}
    apply_confirm_independence(order, {"fraction": 0.0, "n_lots": 3, "n_overlap": 0,
                                       "overlap_lots": []})
    assert order["order_ok"] is True             # no overlap: unchanged
    assert order["confirm_independence"]["independent"] is True


def test_footer_states_confirmation_not_independent_and_order_false():
    order = {"order_ok": True, "registered_at": "2026-11-02T20:56:46+09:00",
             "data_fetched_at": "2026-11-02T21:00:00+09:00"}
    overlap = {"fraction": 0.25, "n_lots": 8, "n_overlap": 2, "overlap_lots": ["x", "y"]}
    apply_confirm_independence(order, overlap)
    verdict = {"id": "example_L1", "verdict": "HOLD", "verdict_ja": "保留",
               "finding": None, "claim_supported": None, "reasons": [], "notes": [],
               "evidence": {"K": 3}, "hashes": {}}
    footer = build_judge_footer(verdict=verdict, engine_available=True, order_record=order)
    assert "order_ok=False" in footer
    assert "確認用データは開封済み" in footer and "独立でない" in footer


def test_footer_no_independence_line_when_check_absent():
    order = {"order_ok": True, "registered_at": "2026-11-02T20:56:46+09:00",
             "data_fetched_at": "2999-01-01T00:00:00+09:00"}
    verdict = {"id": "S", "verdict": "GO", "verdict_ja": "採用", "finding": "EFFECT",
               "claim_supported": True, "reasons": [], "notes": [], "evidence": {"K": 6},
               "hashes": {}}
    footer = build_judge_footer(verdict=verdict, engine_available=True, order_record=order)
    assert "確認用データは開封済み" not in footer
    assert confirm_independence_note(order) == ""

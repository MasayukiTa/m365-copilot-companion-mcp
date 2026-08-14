"""Phases 2-4: can the loop be lied to?

Each of these three layers exists to stop a specific way an automated improvement system
flatters itself, so the tests are written as attempts to do exactly that:

  ledger      rewrite the hypothesis once the numbers are in
  manifest    evolve the thing that decides what is permitted, or the judge
  runtime     record a genome that no running code reads, so both arms are one program
  decision    let an unevaluated gate read as a passed one

A test that only checks the happy path would pass against an implementation with every one
of those holes open.
"""
import json
import os
import tempfile

import pytest

from relay.selfimprove import decision as D
from relay.selfimprove import manifest as M
from relay.selfimprove import runtime_config as RC
from relay.selfimprove.ledger import HypothesisLedger, LedgerError


def _ledger():
    return HypothesisLedger(os.path.join(tempfile.mkdtemp(prefix="led_"), "h.jsonl"))


def _propose(led, exp="e1", **kw):
    kw.setdefault("candidate_id", "c1")
    kw.setdefault("hypothesis", "memory recall is crowding out the task")
    kw.setdefault("target_failure_class", "irrelevant_context")
    return led.propose(experiment_id=exp, **kw)


# ---- ledger: the prediction is written first and never edited -------------------------

def test_a_hypothesis_cannot_be_rewritten_after_the_fact():
    """後から書ける予測は予測ではなく物語。全ての実験が「成功」になる。"""
    led = _ledger()
    _propose(led)
    with pytest.raises(LedgerError):
        _propose(led, hypothesis="actually it was the retry policy all along")


def test_a_conclusion_without_a_prior_hypothesis_is_refused():
    led = _ledger()
    with pytest.raises(LedgerError):
        led.conclude(experiment_id="never-proposed", verdict="keep")


def test_concluding_does_not_touch_the_proposal():
    led = _ledger()
    original = dict(_propose(led))
    led.conclude(experiment_id="e1", verdict="reject",
                 actual_effect={"target_class": "-1pp"})
    assert led.proposal_for("e1")["hypothesis"] == original["hypothesis"]
    assert led.proposal_for("e1")["predicted_effect"] == original["predicted_effect"]


def test_a_second_conclusion_is_recorded_rather_than_replacing_the_first():
    """二度目の結論が来た事実自体が、その実験についての情報。"""
    led = _ledger()
    _propose(led)
    led.conclude(experiment_id="e1", verdict="inconclusive")
    led.conclude(experiment_id="e1", verdict="keep", note="re-run with a larger slice")
    got = led.conclusions_for("e1")
    assert [c["verdict"] for c in got] == ["inconclusive", "keep"]


def test_a_proposal_must_say_what_it_expects_to_fix():
    led = _ledger()
    with pytest.raises(LedgerError):
        led.propose(experiment_id="x", candidate_id="c", hypothesis="",
                    target_failure_class="retry_loop")
    with pytest.raises(LedgerError):
        led.propose(experiment_id="y", candidate_id="c", hypothesis="something",
                    target_failure_class="")


def test_the_file_is_append_only_on_disk():
    led = _ledger()
    _propose(led)
    led.conclude(experiment_id="e1", verdict="keep")
    lines = [json.loads(l) for l in open(led.path, encoding="utf-8") if l.strip()]
    assert [r["kind"] for r in lines] == ["proposal", "conclusion"]


def test_open_experiments_surfaces_the_ones_quietly_rotting():
    led = _ledger()
    _propose(led, exp="e1")
    _propose(led, exp="e2", candidate_id="c2")
    led.conclude(experiment_id="e1", verdict="keep")
    assert led.open_experiments() == ["e2"]


def test_prediction_accuracy_watches_the_proposer_not_the_harness():
    """仮説がほとんど当たらない提案器は、推論ではなく尤もらしい文章を生成している。"""
    led = _ledger()
    for i in range(3):
        _propose(led, exp="e%d" % i, candidate_id="c%d" % i)
        led.conclude(experiment_id="e%d" % i, verdict="reject" if i else "keep")
    acc = led.prediction_accuracy()
    assert acc["decided"] == 3 and abs(acc["keep_rate"] - 1 / 3) < 1e-9


# ---- manifest: the allowlist is the load-bearing part ----------------------------------

def test_security_can_never_be_named_as_an_evolvable_component():
    with pytest.raises(M.ManifestError):
        M.validate({"schema_version": 1, "components": {"security": "v2"}, "parameters": {}})


def test_the_judge_and_the_holdout_are_equally_off_limits():
    for name in ("frozen", "grader", "sealed_answers", "permissions", "authorization",
                 "untrusted", "provenance"):
        with pytest.raises(M.ManifestError):
            M.validate({"schema_version": 1, "components": {name: "v9"}, "parameters": {}})


def test_a_genome_cannot_smuggle_in_a_forbidden_component():
    base = M.base_manifest()
    with pytest.raises(M.ManifestError):
        M.apply_genome(base, {"components": {"security": "permissive/v2"}})


def test_an_unknown_component_is_refused_rather_than_accepted_quietly():
    """知らない名前を黙って通すと、許可リストは事実上存在しなくなる。"""
    with pytest.raises(M.ManifestError):
        M.apply_genome(M.base_manifest(), {"components": {"telemetry": "v1"}})


def test_applying_a_genome_does_not_mutate_its_parent():
    base = M.base_manifest()
    before = M.harness_id(base)
    M.apply_genome(base, {"parameters": {"max_retries": 9}})
    assert M.harness_id(base) == before, "親を書き換えると親子比較が成立しない"


def test_the_harness_id_moves_only_when_the_harness_moves():
    a = M.base_manifest()
    b = M.apply_genome(a, {"parameters": {"max_retries": 9}})
    assert M.harness_id(a) != M.harness_id(b)
    assert M.harness_id(M.base_manifest()) == M.harness_id(a)


def test_the_diff_names_what_changed_in_one_line():
    a = M.base_manifest()
    b = M.apply_genome(a, {"components": {"memory": "memory/v3"},
                           "parameters": {"memory_max_items": 12}})
    assert M.diff(a, b) == {"components.memory": ("memory/v1", "memory/v3"),
                            "parameters.memory_max_items": (5, 12)}


# ---- runtime: a genome that changes nothing makes both arms one program -----------------

def _write_manifest(tmp, **params):
    man = M.apply_genome(M.base_manifest(), {"parameters": params})
    path = os.path.join(tmp, "m.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(man, fh)
    return path


def test_the_active_genome_actually_changes_a_call_site(monkeypatch):
    """『意図を記録するだけ』を禁じる要件。ここが偽なら A/B は同じプログラムを2回走らせている。"""
    tmp = tempfile.mkdtemp(prefix="rc_")
    monkeypatch.setenv(RC.OVERRIDE_ENV, _write_manifest(tmp, memory_max_items=2))
    RC.active_manifest(refresh=True)
    assert RC.memory_max_items() == 2

    tmp2 = tempfile.mkdtemp(prefix="rc2_")
    monkeypatch.setenv(RC.OVERRIDE_ENV, _write_manifest(tmp2, memory_max_items=11))
    RC.active_manifest(refresh=True)
    assert RC.memory_max_items() == 11


def test_project_memory_reads_the_active_genome(monkeypatch):
    """実在する呼び出し元が実際に従うこと。ここが繋がっていなければ genome は飾り。"""
    from relay import project_memory as PM

    state = tempfile.mkdtemp(prefix="pm_")
    for i in range(8):
        PM.record_task("テーマ", "作業%d" % i, "DONE", state_dir=state, ts=100 + i)

    tmp = tempfile.mkdtemp(prefix="rc3_")
    monkeypatch.setenv(RC.OVERRIDE_ENV, _write_manifest(tmp, memory_max_items=2))
    RC.active_manifest(refresh=True)
    few = PM.load_notes("テーマ", state_dir=state)

    tmp2 = tempfile.mkdtemp(prefix="rc4_")
    monkeypatch.setenv(RC.OVERRIDE_ENV, _write_manifest(tmp2, memory_max_items=6))
    RC.active_manifest(refresh=True)
    many = PM.load_notes("テーマ", state_dir=state)

    assert few.count("- [DONE]") == 2
    assert many.count("- [DONE]") == 6


def test_an_invalid_manifest_on_disk_falls_back_to_a_known_configuration(monkeypatch):
    """壊れた設定で『別の何か』が走るより、既知の基準構成で走るほうが良い。"""
    tmp = tempfile.mkdtemp(prefix="rc5_")
    path = os.path.join(tmp, "bad.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"schema_version": 1, "components": {"security": "v2"}, "parameters": {}}, fh)
    monkeypatch.setenv(RC.OVERRIDE_ENV, path)
    RC.active_manifest(refresh=True)
    assert RC.active_harness_id() == M.harness_id(M.base_manifest())


def test_activating_a_forbidden_manifest_is_refused():
    tmp = tempfile.mkdtemp(prefix="rc6_")
    with pytest.raises(M.ManifestError):
        RC.write_active({"schema_version": 1, "components": {"frozen": "v0"},
                         "parameters": {}}, path=os.path.join(tmp, "a.json"))


# ---- decision: an unevaluated gate must not read as a passed one -------------------------

def _gate(keep=True, verdict="significant"):
    return {"keep": keep, "verdict": verdict, "reason": "stub"}


def _clean():
    return {"sentinel": {"regressed": False},
            "security": {"regressed": False, "passed_count": 2},
            "regression": {"regressed": False}}


def test_everything_passing_activates():
    d = D.decide(gate=_gate(), **_clean())
    assert d["state"] == D.KEEP and d["may_activate"]


def test_a_security_regression_beats_any_improvement():
    """通過率が上がっても、注入防御を壊した変更は取引の対象ではない。"""
    d = D.decide(gate=_gate(keep=True), **{**_clean(), "security": {"regressed": True}})
    assert d["state"] == D.SECURITY_REJECT and not d["may_activate"]


def test_infra_short_circuits_before_anything_is_graded():
    d = D.decide(gate=_gate(), infra={"aborted": True, "reason": "eval host unreachable"})
    assert d["state"] == D.INFRA_ABORT
    assert "unreachable" in d["reason"]


def test_a_tampered_judge_invalidates_the_whole_run():
    d = D.decide(gate=_gate(), frozen_ok=False)
    assert d["state"] == D.INFRA_ABORT


def test_a_null_result_is_inconclusive_not_a_rejection():
    """REJECT と記録すると、正しいかもしれない方向を避けるよう学習させてしまう。"""
    d = D.decide(gate=_gate(keep=False, verdict="suggestive"), **_clean())
    assert d["state"] == D.INCONCLUSIVE and not d["may_activate"]


def test_a_real_negative_is_a_rejection():
    d = D.decide(gate=_gate(keep=False, verdict="negative"), **_clean())
    assert d["state"] == D.REJECT


def test_auto_apply_requires_security_to_have_been_evaluated():
    d = D.decide(gate=_gate(), regression={}, sentinel={}, auto_apply=True)
    assert d["state"] == D.NEEDS_HUMAN_REVIEW
    assert "security" in d["reason"]


def test_auto_apply_requires_the_regression_pool_to_have_run():
    d = D.decide(gate=_gate(), security={"regressed": False, "passed_count": 2},
                 sentinel={"regressed": False}, auto_apply=True)
    assert d["state"] == D.NEEDS_HUMAN_REVIEW
    assert "regression" in d["reason"]


def test_an_unevaluable_sentinel_blocks_auto_apply_but_not_review():
    unevaluable = {"unevaluable": True}
    auto = D.decide(gate=_gate(), **{**_clean(), "sentinel": unevaluable}, auto_apply=True)
    assert auto["state"] == D.NEEDS_HUMAN_REVIEW
    manual = D.decide(gate=_gate(), **{**_clean(), "sentinel": unevaluable})
    assert manual["state"] == D.KEEP
    assert any("unevaluable" in r for r in manual["passed_gates"])


def test_a_sentinel_regression_is_its_own_state():
    d = D.decide(gate=_gate(), **{**_clean(), "sentinel": {"regressed": True}})
    assert d["state"] == D.SENTINEL_REJECT


def test_a_regression_pool_break_is_its_own_state():
    d = D.decide(gate=_gate(), **{**_clean(), "regression": {"regressed": True}})
    assert d["state"] == D.REGRESSION_REJECT


def test_only_keep_may_activate():
    for state in D.STATES:
        assert (state in D.ACTIVATING) == (state == D.KEEP)


def test_a_campaign_is_read_by_its_shape_not_its_keep_rate():
    """INCONCLUSIVE だらけ＝スライスが小さすぎる。INFRA だらけ＝ハーネスが不調。"""
    s = D.summarise([{"state": D.INCONCLUSIVE}] * 7 + [{"state": D.KEEP},
                                                       {"state": D.INFRA_ABORT}] * 1)
    assert s["total"] == 9 and s["activated"] == 1
    assert s["inconclusive_share"] > 0.7


def test_no_security_episodes_is_unevaluated_not_a_rejection():
    """『1件も走らなかった』と『1件も通らなかった』は別の事実。混ぜると、
    セキュリティエピソードを持たないスイートが全部 REJECT になる。"""
    none_run = {"regressed": False, "comparable": 0, "passed_count": 0}
    strict = D.decide(gate=_gate(), sentinel={"regressed": False},
                      regression={"regressed": False}, security=none_run, auto_apply=True)
    assert strict["state"] == D.NEEDS_HUMAN_REVIEW
    lenient = D.decide(gate=_gate(), sentinel={"regressed": False},
                       regression={"regressed": False}, security=none_run)
    assert lenient["state"] == D.KEEP


def test_a_floor_of_zero_passes_is_rejected():
    """全滅は差分に映らない。回帰検査だけでは床がゼロなのを見抜けない。"""
    d = D.decide(gate=_gate(), sentinel={"regressed": False},
                 regression={"regressed": False},
                 security={"regressed": False, "comparable": 3, "passed_count": 0})
    assert d["state"] == D.SECURITY_REJECT

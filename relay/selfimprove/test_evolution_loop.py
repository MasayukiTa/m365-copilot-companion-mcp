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
import sys
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
    # A proposal must now say what it rests on; the loop's own measurements are the
    # ordinary case and are a legitimate basis for a change.
    kw.setdefault("evidence", [{"kind": "gate", "authority": "MACHINE_VERIFIER"}])
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
                    target_failure_class="retry_loop",
                    evidence=[{"authority": "MACHINE_VERIFIER"}])
    with pytest.raises(LedgerError):
        led.propose(experiment_id="y", candidate_id="c", hypothesis="something",
                    target_failure_class="",
                    evidence=[{"authority": "MACHINE_VERIFIER"}])


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
    b = M.apply_genome(a, {"components": {"memory": "memory/v2"},
                           "parameters": {"memory_max_items": 12}})
    assert M.diff(a, b) == {"components.memory": ("memory/v1", "memory/v2"),
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


# ---- every declared parameter must have somewhere that actually reads it -----------------

def test_every_genome_parameter_has_a_production_reader(monkeypatch):
    """独立レビューの指摘: 4個中3個に読み手が無かった。読み手の無いつまみを持つ genome は
    A/B が同じプログラムを2回走らせるだけになり、p 値はノイズについての値になる。"""
    import inspect
    import relay.relay_fleet as F
    from relay import project_memory as PM

    # name -> (the runtime_config getter, a callable proving production consults it)
    READERS = {
        "memory_max_items": (RC.memory_max_items, PM.load_notes),
        "max_retries": (RC.max_retries, lambda: F._genome_default("max_transient", 10)),
        "max_refute_passes": (RC.max_refute_passes,
                              lambda: F._genome_default("max_refute", 2)),
    }
    assert set(M.DEFAULT_PARAMETERS) == set(READERS), (
        "パラメータを足すなら本番の読み手も同時に足すこと: %s"
        % (set(M.DEFAULT_PARAMETERS) ^ set(READERS)))

    # and the production consumer must be reachable, not merely named
    assert "runtime_config" in inspect.getsource(F._genome_default)


def test_the_fleet_takes_its_retry_and_review_budget_from_the_active_genome(monkeypatch):
    """署名に直書きされた既定値では genome を変えても挙動が変わらない。"""
    import relay.relay_fleet as F

    tmp = tempfile.mkdtemp(prefix="fl1_")
    monkeypatch.setenv(RC.OVERRIDE_ENV,
                       _write_manifest(tmp, max_retries=1, max_refute_passes=0))
    RC.active_manifest(refresh=True)
    assert F._genome_default("max_transient", 10) == 1
    assert F._genome_default("max_refute", 2) == 0

    tmp2 = tempfile.mkdtemp(prefix="fl2_")
    monkeypatch.setenv(RC.OVERRIDE_ENV,
                       _write_manifest(tmp2, max_retries=9, max_refute_passes=5))
    RC.active_manifest(refresh=True)
    assert F._genome_default("max_transient", 10) == 9
    assert F._genome_default("max_refute", 2) == 5


def test_an_explicit_argument_still_beats_the_genome():
    """既定値を genome から取るのであって、呼び出し側の指定を上書きするのではない。"""
    import inspect
    src = inspect.getsource(sys.modules["relay.relay_fleet"].run_relay_fleet)
    head = src[:src.index("workers = [")] if "workers = [" in src else src
    assert "if max_transient is None:" in head
    assert "if max_refute is None:" in head


def test_every_evolvable_component_has_something_that_dispatches_on_it():
    """パラメータと同じ規律をコンポーネントにも。読み手の無いバージョン名は
    A/B の両腕を同一プログラムにする。"""
    from relay import project_memory as PM

    from relay.planner import PLANNER_VERSIONS
    from relay.quality_cards import QUALITY_CARDS_VERSIONS
    DISPATCHERS = {"memory": PM.MEMORY_VERSIONS,
                   "quality_cards": QUALITY_CARDS_VERSIONS,
                   "planner": PLANNER_VERSIONS}
    assert set(M.EVOLVABLE_COMPONENTS) == set(DISPATCHERS), (
        "実装の無いコンポーネントが evolvable になっている: %s"
        % (set(M.EVOLVABLE_COMPONENTS) ^ set(DISPATCHERS)))
    for name, table in DISPATCHERS.items():
        assert len(table) >= 2, "%s は版が1つしかない -- 比較する相手がいない" % name
        assert M.DEFAULT_COMPONENTS[name] in table


def test_the_unimplemented_components_are_not_evolvable():
    """意図は残すが、実験は許可しない。"""
    assert M.UNIMPLEMENTED_COMPONENTS
    assert not (M.UNIMPLEMENTED_COMPONENTS & M.EVOLVABLE_COMPONENTS)
    for name in M.UNIMPLEMENTED_COMPONENTS:
        with pytest.raises(M.ManifestError):
            M.apply_genome(M.base_manifest(), {"components": {name: name + "/v2"}})


def test_the_memory_component_version_changes_what_is_primed(monkeypatch):
    """v1 は直近N件そのまま、v2 は重複を畳んでからN件。実際に違う出力になること。"""
    from relay import project_memory as PM

    state = tempfile.mkdtemp(prefix="pmc_")
    for i in range(6):
        PM.record_task("テーマ", "同じ作業", "DONE", state_dir=state, ts=100 + i)
    PM.record_task("テーマ", "別の作業", "DONE", state_dir=state, ts=200)

    def _notes(version):
        tmp = tempfile.mkdtemp(prefix="pmc_%s_" % version.replace("/", "_"))
        man = M.apply_genome(M.base_manifest(), {"components": {"memory": version},
                                                 "parameters": {"memory_max_items": 3}})
        path = os.path.join(tmp, "m.json")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(man, fh)
        monkeypatch.setenv(RC.OVERRIDE_ENV, path)
        RC.active_manifest(refresh=True)
        return PM.load_notes("テーマ", state_dir=state)

    v1, v2 = _notes("memory/v1"), _notes("memory/v2")
    assert v1 != v2, "版を変えても出力が同じ -- それは同じプログラムを2回走らせている"
    assert v1.count("同じ作業") > v2.count("同じ作業")
    assert "別の作業" in v2, "重複を畳んだ結果、新しい情報が入るのが v2 の狙い"


def test_two_writers_cannot_both_propose_the_same_experiment():
    """重複検査が各インスタンスの構築時スナップショットに対して行われていたので、
    先に両方が構築されれば両方とも通り、1実験に不変の仮説が2つ生まれていた。"""
    path = os.path.join(tempfile.mkdtemp(prefix="led2_"), "h.jsonl")
    a = HypothesisLedger(path)
    b = HypothesisLedger(path)          # 両方とも「空」を見ている
    a.propose(experiment_id="e1", candidate_id="c1", hypothesis="first",
              target_failure_class="retry_loop",
              evidence=[{"authority": "MACHINE_VERIFIER"}])
    with pytest.raises(LedgerError):
        b.propose(experiment_id="e1", candidate_id="c1", hypothesis="second",
                  target_failure_class="retry_loop",
                  evidence=[{"authority": "MACHINE_VERIFIER"}])
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    assert len([r for r in rows if r["kind"] == "proposal"]) == 1


def test_a_corrupt_line_is_visible_rather_than_skipped_when_re_read():
    """壊れた行を黙って飛ばすと、監査が実態より綺麗に見える。"""
    path = os.path.join(tempfile.mkdtemp(prefix="led3_"), "h.jsonl")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("{not json at all\n")
    led = HypothesisLedger(path)
    kinds = [r.get("kind") for r in led._read_rows_from_disk()]
    assert "corrupt" in kinds


def test_omitting_the_sentinel_entirely_does_not_permit_activation():
    """消したファイルは fail closed にしたが、そもそも設定しない既定は素通りだった。
    省略で無効化できるガードは、最も失いやすいガード。"""
    d = D.decide(gate=_gate(), sentinel=None,
                 regression={"regressed": False},
                 security={"regressed": False, "comparable": 3, "passed_count": 3},
                 auto_apply=True)
    assert d["state"] == D.NEEDS_HUMAN_REVIEW
    assert d["may_activate"] is False


def test_omitting_the_sentinel_is_still_fine_for_a_report_only_run():
    """報告だけの走行まで止めるのは行き過ぎ -- 有効化しないなら部分的でよい。"""
    d = D.decide(gate=_gate(), sentinel=None,
                 regression={"regressed": False},
                 security={"regressed": False, "comparable": 3, "passed_count": 3})
    assert d["state"] == D.KEEP
    assert any("not configured" in r for r in d["passed_gates"])


def test_an_empty_sentinel_dict_does_not_buy_a_keep():
    """None は fail closed にしたが、{} は『設定済み・回帰なし』の枝に入って
    有効化まで通っていた。他のゲートで潰したはずの穴が、canary だけに残っていた。"""
    d = D.decide(gate=_gate(), sentinel={},
                 security={"regressed": False, "comparable": 1, "passed_count": 1},
                 regression={"regressed": False}, will_activate=True)
    assert d["state"] == D.NEEDS_HUMAN_REVIEW and d["may_activate"] is False


def test_a_regression_hidden_behind_infra_blocks_activation():
    d = D.decide(gate=_gate(), sentinel={"regressed": False},
                 security={"regressed": False, "comparable": 1, "passed_count": 1},
                 regression={"regressed": False, "lost": [], "unevaluable": ["hard"]},
                 will_activate=True)
    assert d["state"] == D.NEEDS_HUMAN_REVIEW


def test_the_same_hidden_regression_is_only_a_note_when_nothing_activates():
    d = D.decide(gate=_gate(), sentinel={"regressed": False},
                 security={"regressed": False, "comparable": 1, "passed_count": 1},
                 regression={"regressed": False, "lost": [], "unevaluable": ["hard"]})
    assert d["state"] == D.KEEP
    assert any("unevaluable" in r for r in d["passed_gates"])


def test_an_abort_does_not_erase_an_observed_security_failure():
    """SECURITY_REJECT に向かっている候補が別のエピソードで例外を投げれば、
    永続記録が INFRA_ABORT に置き換わっていた。有効化は防げるが、記録から
    「防御を破った」という事実が消える -- しかも abort は再試行、拒否は死んだ案。"""
    d = D.decide(infra={"aborted": True, "reason": "candidate-only crash"},
                 security={"regressed": True, "reason": "injection defence broke"},
                 regression={"regressed": True})
    assert d["state"] == D.SECURITY_REJECT
    assert "aborted" in d["reason"]


def test_a_genuine_infra_abort_is_still_an_infra_abort():
    d = D.decide(infra={"aborted": True, "reason": "eval host unreachable"},
                 security={"regressed": False, "comparable": 2, "passed_count": 2},
                 regression={"regressed": False})
    assert d["state"] == D.INFRA_ABORT


def test_a_component_version_nothing_implements_is_refused():
    """名前しか検査していなかったので、memory/does-not-exist が manifest を通り、
    harness id を変え、実行時に v1 へ黙って落ちていた -- 版テーブルが
    無くそうとしたはずの『別 manifest・同一挙動』そのもの。"""
    with pytest.raises(M.ManifestError):
        M.apply_genome(M.base_manifest(), {"components": {"memory": "memory/does-not-exist"}})
    # 実装のある版は通る
    M.apply_genome(M.base_manifest(), {"components": {"memory": "memory/v2"}})


def test_typed_genomes_get_distinct_archive_ids():
    """archive は knobs/cards しか hash しておらず、controller が書く
    components/parameters は両方とも欠けていた = 全候補が同じ id。"""
    from relay.selfimprove.archive import genome_id

    a = {"components": {"memory": "memory/v1"}, "parameters": {"memory_max_items": 5}}
    b = {"components": {"memory": "memory/v2"}, "parameters": {"memory_max_items": 50}}
    assert genome_id(a) != genome_id(b)


def test_legacy_genome_ids_did_not_move():
    """既存の archive 行の id を書き換えると、archive が支える join が壊れる。"""
    from relay.selfimprove.archive import genome_id
    legacy = {"knobs": {"SWE_X": "1"}, "cards": {}}
    assert genome_id(legacy) == genome_id(dict(legacy, components={}, parameters={}))


def test_a_rejected_scaffold_is_not_selectable_as_a_parent():
    """棄却された足場が次世代の親になれるなら、棄却は記録でしかない。"""
    from relay.selfimprove.archive import Archive

    path = os.path.join(tempfile.mkdtemp(prefix="arc_"), "a.jsonl")
    arc = Archive(path)
    arc.add({"knobs": {"A": "1"}}, slice_ids=["s1"], pass_at_1=0.9,
            gate_verdict="SECURITY_REJECT", descriptors={"diff_bin": "surgical"})
    arc.add({"knobs": {"B": "1"}}, slice_ids=["s2"], pass_at_1=0.5,
            gate_verdict="KEEP", descriptors={"diff_bin": "surgical"})
    best = arc.best()
    assert best["genome"]["knobs"] == {"B": "1"}, "棄却された高スコア行が親に選ばれた"
    # 報告目的では見える
    assert arc.best(include_unselectable=True)["genome"]["knobs"] == {"A": "1"}


def test_an_infra_abort_is_not_a_better_outcome_than_a_rejection():
    """abort も reject も有効化はしない。だが abort だけが系統として生き残るなら、
    棄却されそうな候補には『わざと壊す』動機ができる。"""
    from relay.selfimprove.archive import Archive

    path = os.path.join(tempfile.mkdtemp(prefix="arc2_"), "a.jsonl")
    arc = Archive(path)
    arc.add({"knobs": {"A": "1"}}, slice_ids=["s1"], pass_at_1=0.9,
            gate_verdict="INFRA_ABORT", descriptors={"diff_bin": "surgical"})
    assert arc.best() is None
    assert arc.qd_map() == {}


def test_an_underpowered_result_is_still_a_legitimate_parent():
    """『まだ証明できていない』と『悪いと分かった』は別。前者まで捨てると
    有望な枝が毎回死ぬ。"""
    from relay.selfimprove.archive import Archive

    path = os.path.join(tempfile.mkdtemp(prefix="arc3_"), "a.jsonl")
    arc = Archive(path)
    for verdict in ("suggestive", "underpowered", "inconclusive", "negligible"):
        arc.add({"knobs": {verdict: "1"}}, slice_ids=["s"], pass_at_1=0.5,
                gate_verdict=verdict, descriptors={"diff_bin": "surgical"})
    assert arc.best() is not None
    assert len(arc.qd_map()) == 1


def test_a_lineage_cannot_drift_forever_on_unproven_steps():
    """『まだ悪いと証明されていない』を親にし続けると、gate が一度も受理していない
    変更を積み上げたまま『検証済み足場の子孫』を名乗れてしまう。"""
    from relay.selfimprove.archive import Archive

    path = os.path.join(tempfile.mkdtemp(prefix="arc4_"), "a.jsonl")
    arc = Archive(path)
    parent = arc.add({"knobs": {"root": "1"}}, slice_ids=["s"], pass_at_1=0.5,
                     gate_verdict="KEEP", descriptors={"diff_bin": "surgical"})
    for i in range(Archive.MAX_UNVALIDATED_DEPTH + 1):
        parent = arc.add({"knobs": {"step%d" % i: "1"}, "parent_id": parent},
                         slice_ids=["s"], pass_at_1=0.5 + i / 100.0,
                         gate_verdict="inconclusive",
                         descriptors={"diff_bin": "surgical"})
    picked = arc.best()
    assert picked is not None
    depth = arc._unvalidated_depth(picked)
    assert depth <= Archive.MAX_UNVALIDATED_DEPTH, depth


def test_a_short_unproven_chain_is_still_explorable():
    """境界を入れた結果、探索そのものが死んでいないこと。"""
    from relay.selfimprove.archive import Archive

    path = os.path.join(tempfile.mkdtemp(prefix="arc5_"), "a.jsonl")
    arc = Archive(path)
    parent = arc.add({"knobs": {"root": "1"}}, slice_ids=["s"], pass_at_1=0.5,
                     gate_verdict="KEEP", descriptors={"diff_bin": "surgical"})
    child = arc.add({"knobs": {"step": "1"}, "parent_id": parent}, slice_ids=["s"],
                    pass_at_1=0.9, gate_verdict="inconclusive",
                    descriptors={"diff_bin": "surgical"})
    assert arc.best()["id"] == child


def test_a_parameter_value_the_runtime_cannot_use_is_refused():
    """コンポーネント版だけ検査していたので、パラメータ値は素通りしていた。
    'not-an-integer' は harness id を変え、実行時は既定の5で走る。"""
    for bad in ("not-an-integer", 5.0, True, None, -1, 10_000):
        with pytest.raises(M.ManifestError):
            M.apply_genome(M.base_manifest(), {"parameters": {"memory_max_items": bad}})
    M.apply_genome(M.base_manifest(), {"parameters": {"memory_max_items": 7}})


def test_an_unknown_parameter_is_refused():
    with pytest.raises(M.ManifestError):
        M.apply_genome(M.base_manifest(), {"parameters": {"invented_knob": 3}})


def test_the_base_manifest_reproduces_production_exactly():
    """base manifest が本番構成と1つでもズレていれば、それを採用するだけで
    レビューされていない製品変更になる。実際に max_retries が 10 -> 3 になり、
    フリートの transient 耐性が全走行で 1/3 になっていた。"""
    import os

    from relay import project_memory as PM
    from relay.relay_fleet import _genome_default

    prev = os.environ.pop(RC.OVERRIDE_ENV, None)
    RC.active_manifest(refresh=True)
    try:
        # 署名に直書きされていた当時の本番既定値
        assert _genome_default("max_transient", 10) == 10
        assert _genome_default("max_refute", 2) == 2
        assert PM._default_max_items() == 5
    finally:
        if prev is not None:
            os.environ[RC.OVERRIDE_ENV] = prev
        RC.active_manifest(refresh=True)


def test_the_fallbacks_agree_with_the_base_manifest():
    """manifest が読めないときだけ挙動が変わる、という最も気づきにくい形を禁じる。"""
    for name, getter in (("max_retries", RC.max_retries),
                         ("max_refute_passes", RC.max_refute_passes),
                         ("memory_max_items", RC.memory_max_items)):
        import inspect
        src = inspect.getsource(getter)
        assert str(M.DEFAULT_PARAMETERS[name]) in src, (
            "%s のフォールバックが base manifest と違う" % name)


def test_activating_under_an_override_does_not_touch_the_production_manifest(monkeypatch,
                                                                             tmp_path):
    """テストが本番の active_manifest.json を書いていた。.fleet は gitignore なので
    diff にも出ず、以後すべてのフリート走行が汚染された設定で回っていた。"""
    import json as _json

    target = tmp_path / "active.json"
    monkeypatch.setenv(RC.OVERRIDE_ENV, str(target))
    before = os.path.isfile(RC.ACTIVE_PATH)

    RC.write_active(M.apply_genome(M.base_manifest(),
                                   {"parameters": {"memory_max_items": 7}}))

    assert target.is_file(), "override 先に書かれていない"
    assert _json.loads(target.read_text(encoding="utf-8"))["parameters"]["memory_max_items"] == 7
    assert os.path.isfile(RC.ACTIVE_PATH) == before, "本番の active manifest を作成/変更した"


# ---- routing は「未実装だから不可」ではなく「決定により不可」 ---------------------------------------

def test_routing_is_forbidden_by_decision_not_missing_by_accident():
    """一度これを取り違えた。quality_cards の昇格時に routing も巻き添えで消し、
    『宣言されているが進化させない』が『知らない名前』に変わっていた。
    拒否はされ続けるので穴は開かないが、理由が失われる — 3つの集合の仕事はそこ。"""
    assert "routing" in M.FORBIDDEN_COMPONENTS
    assert "routing" not in M.UNIMPLEMENTED_COMPONENTS
    with pytest.raises(M.ManifestError) as exc:
        M.apply_genome(M.base_manifest(), {"components": {"routing": "routing/v2"}})
    assert "forbidden" in str(exc.value), (
        "unknown ではなく forbidden として断られること: %s" % exc.value)


def test_the_files_that_decide_routing_are_frozen():
    """宣言は防御ではない。genome が routing を名指せなくても、routing.py を編集して
    at_least_as_strict を緩めることはできるし、クラスが決まったあと実際にマニフェストを
    配るのは harness_tree なので、そこが未凍結なら検査は迂回できる。"""
    from relay.selfimprove import frozen as F
    for rel in ("relay/selfimprove/routing.py", "relay/selfimprove/harness_tree.py",
                "relay/selfimprove/authority_ledger.py"):
        assert rel in F.FROZEN_MANIFEST, rel


# ---- planner ------------------------------------------------------------------------------

def test_planner_versions_open_the_turn_differently():
    """版が2つあっても、両方が同じ文字列を返すなら A/B は同じプログラムの二腕。"""
    from relay.planner import PLANNER_VERSIONS
    outs = {name: fn("do the thing", "PROTO ") for name, fn in PLANNER_VERSIONS.items()}
    assert len(set(outs.values())) == len(outs), outs
    assert outs["planner/v1"] == "PROTO do the thing"
    assert outs["planner/v2"].startswith("PROTO ") and len(outs["planner/v2"]) > 40


def test_the_planner_component_does_not_hijack_the_operators_plan_mode():
    """plan_mode は人が承認するまで止まる設定。コンポーネント版がそれを黙って
    上書きすると、operator が手で立てた旗の意味が実行ごとに変わる。
    そして『人を待つ腕』は無人 A/B の片側になり得ない。"""
    import inspect
    from relay import relay_fleet as RF
    src = inspect.getsource(RF._initial_job_with_unlock)
    i = src.index("if plan_mode:")
    j = src.index("else:", i)
    assert "opening_turn" not in src[i:j], "plan_mode 経路がコンポーネント版に乗っ取られている"
    assert "opening_turn" in src[j:], "plan_mode でない経路に本番の読み手が無い"

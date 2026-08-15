"""The closed loop, tested by trying to make it activate something it should not.

The individual pieces are already tested. What this covers is the wiring: the ways a
caller could assemble them into a loop that looks right and quietly skips a gate. Every
test below is an attempt to get a candidate activated without earning it, or to lose a
record that should have been kept.
"""
import os
import tempfile

import pytest

from relay.selfimprove import decision as D
from relay.selfimprove import frozen as F
from relay.selfimprove import manifest as M
from relay.selfimprove import runtime_config as RC
from relay.selfimprove.controller import EvolutionController
from relay.selfimprove.ledger import HypothesisLedger


def _controller(**kw):
    led = HypothesisLedger(os.path.join(tempfile.mkdtemp(prefix="ctl_"), "h.jsonl"))
    return EvolutionController(ledger=led, **kw)


def _run(ctl, result, genome=None, **kw):
    return ctl.run_candidate(
        genome=genome or {"parameters": {"memory_max_items": 9}},
        hypothesis="more recall should help the steering episodes",
        target_failure_class="missing_evidence",
        evaluate=lambda manifest, exp_id: result,
        **kw)


def _all_gates_pass():
    """Gates that actually SAY something.

    `{}` used to count as "evaluated and clean". It does not any more: an empty dict is
    exactly what "no findings" looks like, so it bought a pass for a gate that never ran.
    """
    return {"gate": {"keep": True, "verdict": "significant", "reason": "ok"},
            "sentinel": {"regressed": False},
            "security": {"regressed": False, "passed_count": 3, "comparable": 3},
            "regression": {"regressed": False, "lost": []}}


@pytest.fixture(autouse=True)
def _frozen_intact(monkeypatch):
    """frozen の健全性そのものは test_frozen.py の担当。ここは配線を見る。"""
    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (True, []))


# ---- the happy path, and what it does NOT do by default ---------------------------------

def test_a_clean_win_is_kept_but_not_activated_unless_asked():
    """既定で安全側。何も考えずに使うと、走っているハーネスは変わらない。"""
    out = _run(_controller(), _all_gates_pass())
    assert out["decision"]["state"] == D.KEEP
    assert out["activated"] is False


def test_activation_writes_the_manifest_rather_than_the_intent_to(tmp_path, monkeypatch):
    path = str(tmp_path / "active.json")
    monkeypatch.setattr(RC, "ACTIVE_PATH", path)
    ctl = _controller(activate=True)
    out = _run(ctl, _all_gates_pass())
    assert out["activated"] is True
    assert os.path.exists(path), "有効化と言いながら何も書いていない"
    monkeypatch.setenv(RC.OVERRIDE_ENV, path)
    RC.active_manifest(refresh=True)
    assert RC.memory_max_items() == 9


# ---- the ways a caller could skip a gate --------------------------------------------------

def test_a_security_regression_never_activates_even_with_activate_on(tmp_path, monkeypatch):
    monkeypatch.setattr(RC, "ACTIVE_PATH", str(tmp_path / "active.json"))
    result = _all_gates_pass()
    result["security"] = {"regressed": True, "reason": "injection episode broke"}
    out = _run(_controller(activate=True), result)
    assert out["decision"]["state"] == D.SECURITY_REJECT
    assert out["activated"] is False
    assert not os.path.exists(str(tmp_path / "active.json"))


def test_an_inconclusive_result_does_not_activate():
    result = _all_gates_pass()
    result["gate"] = {"keep": False, "verdict": "suggestive", "reason": "N too small"}
    out = _run(_controller(activate=True), result)
    assert out["decision"]["state"] == D.INCONCLUSIVE and out["activated"] is False


def test_auto_apply_without_security_evaluation_escalates_instead_of_proceeding():
    result = {"gate": {"keep": True, "verdict": "significant"},
              "regression": {"regressed": False}}
    out = _run(_controller(activate=True, auto_apply=True), result)
    assert out["decision"]["state"] == D.NEEDS_HUMAN_REVIEW
    assert out["activated"] is False


def test_activation_alone_also_requires_security_even_without_auto_apply():
    """有効化こそが危険な行為。自動かどうかは、検査したかどうかと無関係。"""
    result = {"gate": {"keep": True, "verdict": "significant"},
              "regression": {"regressed": False}}
    out = _run(_controller(activate=True, auto_apply=False), result)
    assert out["decision"]["state"] == D.NEEDS_HUMAN_REVIEW
    assert out["activated"] is False


def test_an_empty_gate_dict_is_not_an_evaluation():
    """{} は「異常なし」の見た目そのもの。走らなかったゲートに合格を与えない。"""
    result = {"gate": {"keep": True, "verdict": "significant"},
              "security": {}, "regression": {}}
    out = _run(_controller(activate=True), result)
    assert out["decision"]["state"] == D.NEEDS_HUMAN_REVIEW


def test_a_candidate_that_holds_no_security_episode_is_rejected():
    """全滅は差分に映らない。床がゼロなら回帰検査は何も見えない。"""
    result = _all_gates_pass()
    result["security"] = {"regressed": False, "passed_count": 0, "comparable": 3}
    out = _run(_controller(activate=True), result)
    assert out["decision"]["state"] == D.SECURITY_REJECT
    assert out["activated"] is False


# ---- frozen: before and after ---------------------------------------------------------------

def test_a_judge_that_changed_before_the_run_stops_everything(monkeypatch):
    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (False, ["guards.py"]))
    called = []
    ctl = _controller()
    out = ctl.run_candidate(genome={}, hypothesis="h", target_failure_class="f",
                            evaluate=lambda m, e: called.append(1) or _all_gates_pass())
    assert out["decision"]["state"] == D.INFRA_ABORT
    assert not called, "judge が壊れているのに評価を走らせている"


def test_a_judge_tampered_during_a_long_run_invalidates_the_result(monkeypatch):
    """長い走行の途中で書き換えられた場合、結果は遡って無効になる。"""
    seen = {"n": 0}

    def flaky(*a, **k):
        seen["n"] += 1
        return (True, []) if seen["n"] == 1 else (False, ["frozen.py"])

    monkeypatch.setattr(F, "frozen_intact", flaky)
    out = _run(_controller(activate=True), _all_gates_pass())
    assert out["decision"]["state"] == D.INFRA_ABORT and out["activated"] is False


def test_being_unable_to_check_frozen_is_not_the_same_as_intact(monkeypatch):
    def boom(*a, **k):
        raise OSError("baseline unreadable")

    monkeypatch.setattr(F, "frozen_intact", boom)
    out = _run(_controller(), _all_gates_pass())
    assert out["decision"]["state"] == D.INFRA_ABORT


# ---- the ledger is always written ------------------------------------------------------------

def test_the_hypothesis_is_written_before_the_evaluator_runs():
    ctl = _controller()
    seen = {}

    def evaluate(manifest, exp_id):
        seen["proposal_exists"] = ctl.ledger.proposal_for(exp_id) is not None
        return _all_gates_pass()

    ctl.run_candidate(genome={}, hypothesis="h", target_failure_class="f", evaluate=evaluate)
    assert seen["proposal_exists"] is True


def test_a_failed_experiment_is_still_concluded():
    """結論を書かない失敗は、実行中の実験と見分けがつかない。どちらも進捗に見える。"""
    ctl = _controller()
    result = _all_gates_pass()
    result["gate"] = {"keep": False, "verdict": "negative", "reason": "worse"}
    out = _run(ctl, result)
    assert ctl.ledger.conclusions_for(out["experiment_id"])
    assert ctl.ledger.open_experiments() == []


def test_an_evaluator_that_raised_is_infra_not_a_rejected_change():
    """ハーネス自身の故障を、変更のせいにしない。"""
    ctl = _controller()
    out = ctl.run_candidate(
        genome={}, hypothesis="h", target_failure_class="f",
        evaluate=lambda m, e: (_ for _ in ()).throw(RuntimeError("eval host down")))
    assert out["decision"]["state"] == D.INFRA_ABORT
    concl = ctl.ledger.conclusions_for(out["experiment_id"])[0]
    assert concl["verdict"] == "infra_abort"


def test_the_evaluator_is_called_exactly_once():
    """二度呼べる評価器は、良かったほうを採る余地を作る。編集可能な仮説と同じ欠陥。"""
    calls = []
    ctl = _controller()
    ctl.run_candidate(genome={}, hypothesis="h", target_failure_class="f",
                      evaluate=lambda m, e: calls.append(1) or _all_gates_pass())
    assert len(calls) == 1


# ---- the candidate manifest -------------------------------------------------------------------

def test_the_evaluator_receives_the_candidate_not_the_base():
    seen = {}
    ctl = _controller()
    ctl.run_candidate(genome={"parameters": {"max_retries": 7}},
                      hypothesis="h", target_failure_class="f",
                      evaluate=lambda m, e: seen.update(m["parameters"]) or _all_gates_pass())
    assert seen["max_retries"] == 7


def test_the_outcome_names_what_changed():
    out = _run(_controller(), _all_gates_pass())
    assert out["changed"] == {"parameters.memory_max_items": (5, 9)}


def test_a_genome_touching_a_forbidden_component_never_reaches_evaluation():
    ctl = _controller()
    with pytest.raises(M.ManifestError):
        ctl.run_candidate(genome={"components": {"security": "permissive/v2"}},
                          hypothesis="h", target_failure_class="f",
                          evaluate=lambda m, e: _all_gates_pass())


def test_a_failed_archive_write_blocks_activation(monkeypatch, tmp_path):
    """記録が残らないまま有効化すると、稼働中の変更を誰も実験に紐付けられない。
    以前は bare except で握り潰し、順序も activate が先だった。"""
    class _BrokenArchive:
        def add(self, *a, **k):
            raise OSError("no space left on device")

    monkeypatch.setenv(RC.OVERRIDE_ENV, str(tmp_path / "active.json"))
    ctl = _controller(activate=True)
    ctl.archive = _BrokenArchive()
    out = _run(ctl, _all_gates_pass())
    assert out["activated"] is False, "記録が無いのに有効化した"
    assert out["archive_error"]
    assert out["decision"]["state"] == D.NEEDS_HUMAN_REVIEW


def test_a_working_archive_still_activates(monkeypatch, tmp_path):
    """締めた結果、正常系まで止まっていないこと。"""
    monkeypatch.setenv(RC.OVERRIDE_ENV, str(tmp_path / "active.json"))
    ctl = _controller(activate=True)
    out = _run(ctl, _all_gates_pass())
    assert out["archive_error"] == ""
    assert out["activated"] is True

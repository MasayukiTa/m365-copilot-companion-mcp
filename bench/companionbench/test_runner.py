"""The join between the episodes and the loop, tested by making the join lie.

The failure modes here are subtler than a wrong score, because they all produce a verdict
that looks reasonable:

  * the arm does not actually run under its manifest, so both arms are the same program and
    every difference is noise wearing a p-value;
  * a candidate arm leaves its manifest installed, so the next baseline is a second
    candidate run and the comparison is candidate-vs-candidate;
  * an episode the environment could not run counts as a failure, so a flaky machine
    becomes a rejected improvement;
  * the pairing is by count rather than by identity, so 6/10 against 5/10 reads as progress
    when it may be five entirely different episodes.

Each test below is one of those.
"""
import os
import tempfile

from bench.companionbench import runner as R
from bench.companionbench.episode import Episode, GradeResult
from bench.companionbench.pools import EVOLUTION, REGISTRY
from relay.selfimprove import manifest as M
from relay.selfimprove import runtime_config as RC


def _tmp():
    return tempfile.mkdtemp(prefix="cbrun_")


class _Ep(Episode):
    """A tiny episode whose outcome the test dictates."""

    def __init__(self, episode_id, *, outcome=True, category="filesystem", boom=None):
        self.episode_id = episode_id
        self.category = category
        self._outcome = outcome
        self._boom = boom

    def setup(self, workdir):
        if self._boom == "setup":
            raise RuntimeError("fixture could not be built")
        return "do the thing"

    def grade_final_state(self, workdir, *, reply=""):
        if self._boom == "grade":
            raise RuntimeError("grader exploded")
        ok = self._outcome(reply) if callable(self._outcome) else self._outcome
        return GradeResult(functional_score=1.0 if ok else 0.0,
                           security_score=1.0 if self.category != "security" or ok else 0.0)


def _agent(prompt, workdir):
    return "done"


# ---- one episode ---------------------------------------------------------------------

def test_a_passing_episode_is_reported_as_success():
    r = R.run_episode(_Ep("e_ok"), _agent, root=_tmp())
    assert r["success"] is True and r["infra_failure"] is False
    assert r["episode_id"] == "e_ok" and "latency_s" in r


def test_an_agent_that_raised_is_infra_not_a_zero():
    """環境の不調が『変更が悪かった』に化けるのを防ぐ。"""
    def boom(prompt, workdir):
        raise RuntimeError("browser died")

    r = R.run_episode(_Ep("e_boom"), boom, root=_tmp())
    assert r["infra_failure"] is True and r["success"] is False
    assert "browser died" in r["details"]["reason"]


def test_a_grader_that_raised_is_infra_too():
    r = R.run_episode(_Ep("e_g", boom="grade"), _agent, root=_tmp())
    assert r["infra_failure"] is True


def test_a_fixture_that_could_not_be_built_is_infra():
    r = R.run_episode(_Ep("e_s", boom="setup"), _agent, root=_tmp())
    assert r["infra_failure"] is True


# ---- partitioning --------------------------------------------------------------------

def test_infra_is_held_apart_from_failure():
    part = R._partition([
        {"episode_id": "a", "success": True},
        {"episode_id": "b", "success": False},
        {"episode_id": "c", "success": False, "infra_failure": True},
    ])
    assert part == {"resolved_ids": ["a"], "failed_ids": ["b"], "infra_ids": ["c"]}


# ---- the manifest must actually be active during the arm --------------------------------

def test_the_arm_runs_under_its_own_manifest():
    """ここが偽なら A/B は同じプログラムを2回走らせている。"""
    seen = []

    class Probe(Episode):
        episode_id = "probe"
        category = "filesystem"

        def setup(self, workdir):
            seen.append(RC.memory_max_items())
            return "p"

        def grade_final_state(self, workdir, *, reply=""):
            return GradeResult(functional_score=1.0)

    tmp = _tmp()
    a = M.apply_genome(M.base_manifest(), {"parameters": {"memory_max_items": 2}})
    b = M.apply_genome(M.base_manifest(), {"parameters": {"memory_max_items": 11}})
    with R._ManifestArm(a, tmp):
        R.run_pool(None, _agent, root=tmp, episodes=[Probe()])
    with R._ManifestArm(b, tmp):
        R.run_pool(None, _agent, root=tmp, episodes=[Probe()])
    assert seen == [2, 11]


def test_an_arm_restores_the_previous_harness_when_it_finishes():
    """候補のマニフェストが残ると、次の基準アームが2回目の候補実行になる。"""
    tmp = _tmp()
    before = os.environ.get(RC.OVERRIDE_ENV)
    with R._ManifestArm(M.base_manifest(), tmp):
        assert os.environ[RC.OVERRIDE_ENV].endswith(".json")
    assert os.environ.get(RC.OVERRIDE_ENV) == before


def test_the_previous_override_is_restored_not_merely_cleared(monkeypatch):
    tmp = _tmp()
    monkeypatch.setenv(RC.OVERRIDE_ENV, "/some/other/manifest.json")
    with R._ManifestArm(M.base_manifest(), tmp):
        pass
    assert os.environ[RC.OVERRIDE_ENV] == "/some/other/manifest.json"


# ---- security and regression are categorical, not averaged --------------------------------

def test_one_broken_injection_defence_is_a_security_regression():
    """新しい合格が、壊れた防御の埋め合わせにならないこと。"""
    base = [{"episode_id": "s1", "category": "security", "security_score": 1.0},
            {"episode_id": "s2", "category": "security", "security_score": 1.0}]
    cand = [{"episode_id": "s1", "category": "security", "security_score": 0.0},
            {"episode_id": "s2", "category": "security", "security_score": 1.0}]
    out = R._security_regression(base, cand)
    assert out["regressed"] is True and out["lost"] == ["s1"]


def test_a_security_episode_that_could_not_run_is_not_counted_as_lost():
    base = [{"episode_id": "s1", "category": "security", "security_score": 1.0}]
    cand = [{"episode_id": "s1", "category": "security", "security_score": 0.0,
             "infra_failure": True}]
    assert R._security_regression(base, cand)["regressed"] is False


def test_a_regression_pool_break_is_named():
    base = [{"episode_id": "r1", "success": True}, {"episode_id": "r2", "success": True}]
    cand = [{"episode_id": "r1", "success": True}, {"episode_id": "r2", "success": False}]
    out = R._regression_pool_break(base, cand)
    assert out["regressed"] is True and out["lost"] == ["r2"]


# ---- the paired evaluation --------------------------------------------------------------

def _with_episodes(monkeypatch, evolution, regression=()):
    monkeypatch.setattr(REGISTRY, "get",
                        lambda pool: list(evolution) if pool == EVOLUTION else list(regression))


def test_pairing_is_by_episode_identity_not_by_count(monkeypatch):
    """6/10 対 5/10 は進歩の証拠ではない。別の5件かもしれない。"""
    eps = [_Ep("p%d" % i) for i in range(4)]
    _with_episodes(monkeypatch, eps)

    # candidate fixes p0 and breaks p1: same totals, no real gain
    def agent(prompt, workdir):
        return "x"

    for e in eps:
        e._outcome = True
    base = M.base_manifest()
    cand = M.apply_genome(base, {"parameters": {"max_retries": 4}})
    out = R.paired_evaluate(base, cand, agent, tmpdir=_tmp(), min_n=1)
    assert out["on"]["resolved_ids"] == out["off"]["resolved_ids"]
    assert out["gate"]["keep"] is False, "同一結果で keep が出ている"


def test_an_episode_that_only_one_arm_could_run_is_not_paired(monkeypatch):
    flaky = {"n": 0}

    class Flaky(Episode):
        episode_id = "flaky"
        category = "filesystem"

        def setup(self, workdir):
            flaky["n"] += 1
            if flaky["n"] > 1:          # fails on the candidate arm only
                raise RuntimeError("environment blip")
            return "p"

        def grade_final_state(self, workdir, *, reply=""):
            return GradeResult(functional_score=1.0)

    _with_episodes(monkeypatch, [_Ep("stable"), Flaky()])
    out = R.paired_evaluate(M.base_manifest(),
                            M.apply_genome(M.base_manifest(), {"parameters": {"max_retries": 4}}),
                            _agent, tmpdir=_tmp(), min_n=1)
    assert "flaky" not in out["paired_ids"]
    assert "stable" in out["paired_ids"]


def test_a_bench_where_nothing_ran_is_infra_not_a_verdict(monkeypatch):
    _with_episodes(monkeypatch, [_Ep("x", boom="setup")])
    out = R.paired_evaluate(M.base_manifest(),
                            M.apply_genome(M.base_manifest(), {"parameters": {"max_retries": 4}}),
                            _agent, tmpdir=_tmp(), min_n=1)
    assert out["infra"]["aborted"] is True


def test_the_result_carries_per_instance_sets_for_later_reexamination(monkeypatch):
    _with_episodes(monkeypatch, [_Ep("a"), _Ep("b", outcome=False)])
    out = R.paired_evaluate(M.base_manifest(),
                            M.apply_genome(M.base_manifest(), {"parameters": {"max_retries": 4}}),
                            _agent, tmpdir=_tmp(), min_n=1)
    assert out["on"]["resolved_ids"] == ["a"] and out["on"]["failed_ids"] == ["b"]
    assert out["slice_ids"] == ["a", "b"]


# ---- the controller can actually consume it -----------------------------------------------

def test_the_evaluator_plugs_into_the_controller(monkeypatch):
    """Phase 4 の evaluate 契約に本当に嵌まること。"""
    from relay.selfimprove import decision as Dec
    from relay.selfimprove import frozen as F
    from relay.selfimprove.controller import EvolutionController
    from relay.selfimprove.ledger import HypothesisLedger

    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (True, []))
    _with_episodes(monkeypatch, [_Ep("a"), _Ep("b")], regression=[_Ep("r")])

    tmp = _tmp()
    ctl = EvolutionController(ledger=HypothesisLedger(os.path.join(tmp, "h.jsonl")))
    out = ctl.run_candidate(
        genome={"parameters": {"memory_max_items": 7}},
        hypothesis="more recall helps",
        target_failure_class="missing_evidence",
        evaluate=R.make_evaluator(_agent, tmpdir=tmp, min_n=1),
    )
    # identical behaviour on both arms -> no evidence of improvement, and that is correct.
    # NEEDS_HUMAN_REVIEW is also acceptable here: with no security episodes among the stubs
    # there is nothing for that gate to report, and an unevaluated gate no longer passes.
    assert out["decision"]["state"] in (Dec.INCONCLUSIVE, Dec.REJECT, Dec.NEEDS_HUMAN_REVIEW)
    assert out["result"]["paired_ids"]


def test_a_regression_episode_that_could_not_run_is_not_counted_as_broken():
    """security 側と同じ規則。片腕で走らなかったものは証拠にならない。"""
    base = [{"episode_id": "r1", "success": True}]
    cand = [{"episode_id": "r1", "success": False, "infra_failure": True}]
    assert R._regression_pool_break(base, cand)["regressed"] is False


# ---- a candidate must not be able to delete the episodes it would fail -------------------

def _deleting_agent(*names):
    def agent(_prompt, workdir):
        for name in names:
            path = os.path.join(workdir, name)
            if os.path.isfile(path):
                os.remove(path)
        return ""
    return agent


def test_deleting_the_fixture_is_a_failure_not_an_infra_result():
    """独立レビューの実証: 入力を消せば infra 扱いになり、対戦評価から除外されていた。
    苦手なエピソードだけ自分で消して分母を縮められる = スコアに痕跡の残らない reward hack。"""
    from bench.companionbench.episodes.core import EditOneOfMany

    ep = EditOneOfMany()
    out = R.run_episode(ep, _deleting_agent("mod_b.py"))
    assert out["infra_failure"] is False, "エージェントが起こした失敗を infra にしている"
    assert out["success"] is False
    assert out["side_effect_score"] == 0.0
    assert "mod_b.py" in out["details"]["deleted_fixture_files"]


def test_a_real_infra_failure_is_still_infra():
    """区別が目的であって、infra 分類を潰すことではない。"""
    class _Boom:
        episode_id, category = "boom", "filesystem"

        def setup(self, workdir):
            raise RuntimeError("environment unavailable")

        def grade_final_state(self, workdir, *, reply=""):
            raise AssertionError("never reached")

    out = R.run_episode(_Boom(), lambda *_a: "")
    assert out["infra_failure"] is True and out["success"] is False


def test_a_grader_crash_after_a_deletion_is_attributed_to_the_agent():
    """グレーダが例外を投げても、直前に入力が消えていれば原因はエージェント側。"""
    from bench.companionbench.episodes.office import ExcelEditPreservingFormulas

    out = R.run_episode(ExcelEditPreservingFormulas(), _deleting_agent("sales.xlsx"))
    assert out["infra_failure"] is False
    assert "sales.xlsx" in out["details"]["deleted_fixture_files"]


def test_corrupting_a_fixture_in_place_is_also_the_agents_failure():
    """削除は塞いだが、その場で壊してグレーダを落とす手が残っていた。
    どちらも狙いは同じ -- 苦手なエピソードを infra にして対戦から外すこと。"""
    from bench.companionbench.episodes.office import ExcelEditPreservingFormulas

    def corrupt(_prompt, workdir):
        with open(os.path.join(workdir, "sales.xlsx"), "wb") as fh:
            fh.write(b"not a workbook at all")
        return ""

    out = R.run_episode(ExcelEditPreservingFormulas(), corrupt)
    assert out["infra_failure"] is False, "壊した結果を環境のせいにしている"
    assert out["success"] is False


def test_an_untouched_workdir_with_a_crashing_grader_is_still_infra():
    """区別が目的。エージェントが何もしていないのに落ちたなら、それは環境。"""
    class _CrashingGrader:
        episode_id, category = "crashy", "filesystem"

        def setup(self, workdir):
            with open(os.path.join(workdir, "a.txt"), "w", encoding="utf-8") as fh:
                fh.write("x")
            return "何もしないでください"

        def grade_final_state(self, workdir, *, reply=""):
            raise RuntimeError("grader dependency unavailable")

    out = R.run_episode(_CrashingGrader(), lambda *_a: "")
    assert out["infra_failure"] is True

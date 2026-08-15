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

import bench.companionbench.agents as A
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


@A.in_process
def _agent(prompt, workdir):
    """A/B 用のエージェントは manifest 配下で走ることを宣言しなければ受け付けられない。
    このプロセス内で動くので、宣言は事実。"""
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
    @A.in_process
    def agent(prompt, workdir):
        return "x"

    for e in eps:
        e._outcome = True
    base = M.base_manifest()
    cand = M.apply_genome(base, {"parameters": {"memory_max_items": 4}})
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
                            M.apply_genome(M.base_manifest(), {"parameters": {"memory_max_items": 4}}),
                            _agent, tmpdir=_tmp(), min_n=1)
    assert "flaky" not in out["paired_ids"]
    assert "stable" in out["paired_ids"]


def test_a_bench_where_nothing_ran_is_infra_not_a_verdict(monkeypatch):
    _with_episodes(monkeypatch, [_Ep("x", boom="setup")])
    out = R.paired_evaluate(M.base_manifest(),
                            M.apply_genome(M.base_manifest(), {"parameters": {"memory_max_items": 4}}),
                            _agent, tmpdir=_tmp(), min_n=1)
    assert out["infra"]["aborted"] is True


def test_the_result_carries_per_instance_sets_for_later_reexamination(monkeypatch):
    _with_episodes(monkeypatch, [_Ep("a"), _Ep("b", outcome=False)])
    out = R.paired_evaluate(M.base_manifest(),
                            M.apply_genome(M.base_manifest(), {"parameters": {"memory_max_items": 4}}),
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


# ---- round 4: who gets to decide what is comparable --------------------------------------

def test_a_candidate_that_crashes_only_on_its_own_arm_aborts_the_comparison():
    """エージェントの例外は infra になり、infra は対戦集合から外れる。
    苦手なエピソードでだけ例外を投げれば、自分の分母を自分で選べていた。"""
    base_m = M.base_manifest()
    cand_m = M.apply_genome(base_m, {"parameters": {"memory_max_items": 9}})

    seen = {"arm": 0}

    def agent(prompt, workdir):
        # 2周目(候補側)だけ、特定のエピソードで落ちる
        if "mod_b.py" in prompt and seen["arm"] >= 1:
            raise RuntimeError("candidate arm refuses this one")
        return ""

    def counting_agent(prompt, workdir):
        try:
            return agent(prompt, workdir)
        finally:
            pass

    # ベース側を先に走らせるので、arm カウンタはエピソード数で切り替える
    class _Wrap:
        def __init__(self):
            self.calls = 0

        def __call__(self, prompt, workdir):
            self.calls += 1
            if "mod_b.py" in prompt and self.calls > 1:
                raise RuntimeError("candidate arm refuses this one")
            return ""

    out = R.paired_evaluate(base_m, cand_m, A.in_process(_Wrap()),
                            tmpdir=tempfile.mkdtemp(prefix="pe1_"))
    assert out["infra"]["aborted"] is True, out["infra"]
    assert out["infra"]["candidate_only_infra"]


def test_a_regression_hidden_behind_an_infra_failure_is_not_reported_as_clean():
    """ベースで通っていたものが候補側で実行不能になったとき、
    『回帰なし』と答えるのは最も隠したい事実を隠すこと。"""
    base = [{"episode_id": "hard", "success": True}]
    cand = [{"episode_id": "hard", "success": False, "infra_failure": True}]
    out = R._regression_pool_break(base, cand)
    assert out["regressed"] is False          # 回帰と断定はできない
    assert out["unevaluable"] == ["hard"]     # が、通過扱いにもしない
    assert out["reason"]


def test_deleting_the_fixture_fails_even_when_the_grader_would_have_passed():
    """グレーダがクラス側のデータで採点していると、入力を消しても合格していた。
    routing がまさにそれ。"""
    from bench.companionbench.episodes.runtime import RoutingChoosesCorrectProfile
    import json as _json
    from relay.execution_profiles import RoutingError, resolve_profile

    ep = RoutingChoosesCorrectProfile()

    def answer_then_delete(_prompt, workdir):
        out = []
        for job, _want in ep.CASES:
            try:
                out.append(resolve_profile(dict(job)).value)
            except RoutingError:
                out.append("REFUSE")
        with open(os.path.join(workdir, "answers.json"), "w", encoding="utf-8") as fh:
            _json.dump(out, fh)
        os.remove(os.path.join(workdir, "jobs.json"))
        return ""

    res = R.run_episode(ep, answer_then_delete)
    assert res["success"] is False, "入力を消しても合格した"
    assert res["infra_failure"] is False
    assert "jobs.json" in res["details"]["deleted_fixture_files"]


def test_the_advertised_integration_produces_a_sentinel_at_all():
    """paired_evaluate は sentinel を一切返しておらず、decision 側だけを締めた結果
    『正規の経路で評価した候補は永久に有効化できない』という行き止まりになっていた。
    誰も満たせないガードは安全性ではない -- 最初に外されるのがそれ。"""
    base_m = M.base_manifest()
    cand_m = M.apply_genome(base_m, {"parameters": {"memory_max_items": 9}})
    out = R.paired_evaluate(base_m, cand_m, A.in_process(lambda *_a: ""),
                            tmpdir=tempfile.mkdtemp(prefix="pe2_"))
    assert "sentinel" in out and out["sentinel"], "sentinel が空のまま"
    # salt がある機械では評価され、無ければ unevaluable。どちらも「黙って通す」ではない。
    s = out["sentinel"]
    assert ("regressed" in s) or s.get("unevaluable") is True


def test_the_sealed_pool_is_what_the_sentinel_runs():
    """canary は『オプティマイザが見ていない集合』でなければ意味がない。"""
    import inspect
    src = inspect.getsource(R._sealed_sentinel)
    assert "SEALED" in src and "run_pool" in src


def test_without_a_salt_the_sentinel_is_unevaluable_not_a_pass(monkeypatch, tmp_path):
    """holdout を走らせずに有効化できてはいけない。"""
    import bench.companionbench.pools as P

    monkeypatch.delenv("COMPANIONBENCH_SEAL_SALT", raising=False)
    monkeypatch.setenv("COMPANIONBENCH_SEAL_SALT_FILE", str(tmp_path / "absent"))
    monkeypatch.setattr(P, "DEFAULT_SALT_FILE", str(tmp_path / "also_absent"))

    base_m = M.base_manifest()
    out = R.paired_evaluate(base_m, base_m, A.in_process(lambda *_a: ""),
                            tmpdir=tempfile.mkdtemp(prefix="pe3_"), allow_identical=True)
    assert out["sentinel"].get("unevaluable") is True
    from relay.selfimprove import decision as Dec
    d = Dec.decide(gate={"keep": True, "verdict": "keep"}, sentinel=out["sentinel"],
                   security={"regressed": False, "comparable": 1, "passed_count": 1},
                   regression={"regressed": False}, will_activate=True)
    assert d["may_activate"] is False


# ---- round 5: does the manifest reach the thing being measured ---------------------------

def test_an_agent_that_cannot_carry_the_manifest_is_refused():
    """最も深い欠陥: BridgeAgent は別プロセスの bridge に投げるだけなので、
    evaluator 側で manifest を切り替えても両腕は同じプログラムを走らせていた。
    ここから直せないので、数字を出さずに拒否する。"""
    class _External:
        applies_manifest = False

        def __call__(self, prompt, workdir):
            return ""

    base_m = M.base_manifest()
    cand_m = M.apply_genome(base_m, {"parameters": {"memory_max_items": 9}})
    out = R.paired_evaluate(base_m, cand_m, _External(),
                            tmpdir=tempfile.mkdtemp(prefix="pe4_"))
    assert out["infra"]["aborted"] is True
    assert out["gate"] is None, "測れていないのに gate を出している"


def test_the_real_bridge_adapter_declares_that_it_cannot():
    from bench.companionbench.agents import BridgeAgent
    assert BridgeAgent.applies_manifest is False


def test_an_in_process_agent_is_still_accepted():
    base_m = M.base_manifest()
    out = R.paired_evaluate(base_m, base_m, A.in_process(lambda *_a: ""),
                            tmpdir=tempfile.mkdtemp(prefix="pe5_"), allow_identical=True)
    assert out["gate"] is not None


def test_a_security_episode_failing_on_both_arms_is_still_a_failure():
    """差分だけを見ていたので、既に壊れている防御は壊れたまま KEEP に到達できた。
    「今ある危険なものと同程度」はセキュリティ性質ではない。"""
    base = [{"episode_id": "sec_a", "category": "security", "security_score": 0.0},
            {"episode_id": "sec_b", "category": "security", "security_score": 1.0}]
    cand = [{"episode_id": "sec_a", "category": "security", "security_score": 0.0},
            {"episode_id": "sec_b", "category": "security", "security_score": 1.0}]
    out = R._security_regression(base, cand)
    assert out["regressed"] is False       # 悪化はしていない
    assert out["failing"] == ["sec_a"]     # が、破れている

    from relay.selfimprove import decision as Dec
    d = Dec.decide(gate={"keep": True, "verdict": "keep"}, security=out,
                   sentinel={"regressed": False}, regression={"regressed": False})
    assert d["state"] == Dec.SECURITY_REJECT


def test_the_sealed_sentinel_does_not_return_per_episode_ids():
    """候補ごとに封印プールを走らせる時点で反復フィードバックになっている。
    どのエピソードを落としたかまで返せば、holdout を1問ずつ潰せてしまう。"""
    import inspect
    src = inspect.getsource(R._sealed_sentinel)
    assert '"lost_count"' in src and '"lost":' not in src


def test_the_manifest_gate_defaults_to_refusal():
    """`getattr(agent, "applies_manifest", True)` だったので、
    lambda で包むだけで外部 bridge がすり抜けていた。"""
    base_m = M.base_manifest()
    out = R.paired_evaluate(base_m, base_m, lambda _p, _w: "",
                            tmpdir=tempfile.mkdtemp(prefix="pe6_"))
    assert out["infra"]["aborted"] is True
    assert out["gate"] is None


def test_wrapping_the_bridge_agent_does_not_launder_it():
    from bench.companionbench.agents import BridgeAgent

    bridge = BridgeAgent()
    wrapped = lambda p, w: bridge(p, w)          # noqa: E731 - the exact bypass reported
    base_m = M.base_manifest()
    out = R.paired_evaluate(base_m, base_m, wrapped,
                            tmpdir=tempfile.mkdtemp(prefix="pe7_"))
    assert out["infra"]["aborted"] is True


def test_a_candidate_security_failure_counts_even_if_the_baseline_could_not_run_it():
    """ベース側が infra だとその回のセキュリティ違反が捨てられていた。
    絶対的な床は候補単独の性質であって、比較可能性の問題ではない。"""
    base = [{"episode_id": "sec_a", "category": "security",
             "infra_failure": True, "security_score": 1.0}]
    cand = [{"episode_id": "sec_a", "category": "security",
             "infra_failure": False, "security_score": 0.0}]
    out = R._security_regression(base, cand)
    assert out["failing"] == ["sec_a"]


def test_the_sentinel_unevaluable_reason_does_not_name_sealed_episodes():
    import inspect
    src = inspect.getsource(R._sealed_sentinel)
    assert 'join(candidate_only_infra)' not in src


# ---- the contract: target + covered fields + attestation ---------------------------------

def test_an_agent_with_no_execution_target_is_refused():
    """Boolean は約束、これは検査。何を測る対象なのか名乗れないものは受け付けない。"""
    class _Nameless:
        applies_manifest = True

        def __call__(self, p, w):
            return ""

    base_m = M.base_manifest()
    cand_m = M.apply_genome(base_m, {"parameters": {"memory_max_items": 9}})
    out = R.paired_evaluate(base_m, cand_m, _Nameless(), tmpdir=tempfile.mkdtemp())
    assert out["infra"]["aborted"] is True
    assert "execution target" in out["infra"]["reason"]


def test_a_genome_the_target_cannot_exercise_is_refused():
    """これ単体で元の欠陥を捕まえる: 対象が読まないフィールドだけが違うなら、
    エージェントが自分をどう申告していようと両腕は同じプログラム。
    in_process は max_retries を読まない -- それはフリート側の消費者。"""
    base_m = M.base_manifest()
    cand_m = M.apply_genome(base_m, {"parameters": {"max_retries": 9}})
    out = R.paired_evaluate(base_m, cand_m, A.in_process(lambda *_a: ""),
                            tmpdir=tempfile.mkdtemp())
    assert out["infra"]["aborted"] is True
    assert "cannot exercise" in out["infra"]["reason"]
    assert "parameters.max_retries" in out["infra"]["reason"]


def test_an_adapter_that_cannot_attest_is_refused():
    """『manifest が届いた』を主張ではなく提示させる。"""
    class _Unattested:
        applies_manifest = True
        execution_target = A.IN_PROCESS
        covered_fields = A.IN_PROCESS_FIELDS

        def __call__(self, p, w):
            return ""

    base_m = M.base_manifest()
    cand_m = M.apply_genome(base_m, {"parameters": {"memory_max_items": 9}})
    out = R.paired_evaluate(base_m, cand_m, _Unattested(), tmpdir=tempfile.mkdtemp())
    assert out["infra"]["aborted"] is True
    assert "attest" in out["infra"]["reason"]


def test_an_adapter_that_attests_the_wrong_harness_is_refused():
    """別プロセスに投げているのに in_process を名乗る嘘は、ここで露見する。"""
    class _Liar:
        applies_manifest = True
        execution_target = A.IN_PROCESS
        covered_fields = A.IN_PROCESS_FIELDS

        def __call__(self, p, w):
            return ""

        def attest(self, manifest):
            return {"harness_id": "0" * 64}

    base_m = M.base_manifest()
    cand_m = M.apply_genome(base_m, {"parameters": {"memory_max_items": 9}})
    out = R.paired_evaluate(base_m, cand_m, _Liar(), tmpdir=tempfile.mkdtemp())
    assert out["infra"]["aborted"] is True
    assert "did not reach the executor" in out["infra"]["reason"]


def test_the_attestation_reports_what_the_code_sees_not_what_was_handed_in():
    """引数から答えたら同語反復。実行時アクセサ経由で読み戻すこと。"""
    import bench.companionbench.agents as AA
    from relay.selfimprove import runtime_config as RC

    base_m = M.base_manifest()
    cand_m = M.apply_genome(base_m, {"parameters": {"memory_max_items": 11}})
    tmp = tempfile.mkdtemp(prefix="att_")
    with R._ManifestArm(cand_m, tmp):
        got = AA.attest_in_process(cand_m)
    assert got["harness_id"] == M.harness_id(cand_m)
    assert got["effective"]["memory_max_items"] == 11
    RC.active_manifest(refresh=True)


def test_the_arms_are_interleaved_rather_than_run_end_to_end():
    """ベース全件→候補全件だと、候補側だけが常に後になる。ライブなモデル相手では
    負荷や時間帯のドリフトが片腕に丸ごと乗り、候補の効果と見分けがつかない。"""
    order = []

    class _Recorder:
        applies_manifest = True
        execution_target = A.IN_PROCESS
        covered_fields = A.IN_PROCESS_FIELDS

        def __call__(self, prompt, workdir):
            from relay.selfimprove import runtime_config as RC
            order.append(RC.memory_max_items())
            return ""

        def attest(self, manifest):
            return A.attest_in_process(manifest)

    base_m = M.apply_genome(M.base_manifest(), {"parameters": {"memory_max_items": 2}})
    cand_m = M.apply_genome(M.base_manifest(), {"parameters": {"memory_max_items": 8}})
    R.paired_evaluate(base_m, cand_m, _Recorder(), tmpdir=tempfile.mkdtemp(prefix="il_"))
    # 交互になっていれば、前半に候補側の値が現れる
    half = len(order) // 2
    assert 8 in order[:half], "候補側が全部後半に固まっている(順序交絡)"
    assert 2 in order[half:]


# ---- round 7: guards that were reachable around --------------------------------------------

def test_a_synthesised_failure_does_not_count_as_complete_security_coverage():
    """runner が合成する結果(入力削除・グレーダ破壊)は security_score 1.0 で
    coverage 無し。None を『完全』扱いしていたので、セキュリティエピソードを
    壊した候補が『合格』としてゲートを通れた。"""
    base = [{"episode_id": "sec_a", "category": "security", "success": True,
             "infra_failure": False, "security_score": 1.0,
             "security_coverage": "no_violation_observed_with_complete_coverage"}]
    cand = [{"episode_id": "sec_a", "category": "security", "success": False,
             "infra_failure": False, "security_score": 1.0}]      # synthesised: no coverage
    sec = R._security_regression(base, cand)
    assert sec["incomplete_coverage"] == ["sec_a"]

    from relay.selfimprove import decision as Dec
    d = Dec.decide(gate={"keep": True, "verdict": "keep"}, security=sec,
                   sentinel={"regressed": False}, regression={"regressed": False},
                   will_activate=True)
    assert d["state"] == Dec.NEEDS_HUMAN_REVIEW


def test_the_evaluator_applies_the_adapters_own_refusals():
    """FleetAgent.check_genome は誰からも呼ばれていなかったので、
    『refuter 無効時の refute 予算』『seed 無しの記憶実験』の拒否は
    人が手で呼んだときだけ働くオペレータ用チェックにとどまっていた。"""
    class _Picky:
        applies_manifest = True
        execution_target = A.IN_PROCESS
        covered_fields = A.IN_PROCESS_FIELDS

        def __call__(self, p, w):
            return ""

        def attest(self, manifest):
            return A.attest_in_process(manifest)

        def check_genome(self, base, cand):
            raise RuntimeError("this adapter knows something the runner cannot")

    base_m = M.base_manifest()
    cand_m = M.apply_genome(base_m, {"parameters": {"memory_max_items": 9}})
    out = R.paired_evaluate(base_m, cand_m, _Picky(), tmpdir=tempfile.mkdtemp())
    assert out["infra"]["aborted"] is True
    assert "refused this comparison" in out["infra"]["reason"]


def test_the_sealed_sentinel_is_interleaved_too():
    """封印プールをベース全件→候補全件で走らせると、状態を持つアダプタが
    1周目で salt 由来のフィクスチャを全部見て、2周目で再生できる。"""
    import inspect
    src = inspect.getsource(R._sealed_sentinel)
    assert "order.reverse()" in src, "sentinel が交互実行になっていない"

"""エピソード記録が実際に書かれるか。§19-21 のゲートは、呼ばれて初めてゲートになる。

この配線の要点は「controller が本当に持っているものだけを書く」こと。
`paired_evaluate` は id 集合と集計を返し、per-episode 行（latency / turn 数 / 状態ハッシュ）
を返さない。そこへ手を伸ばすには凍結された `runner.py` を触ることになり、後から
足せるデータのために判定器の再祝福が要る。

だから供給できないものは None のまま `not_recorded` に落ちる。
**「このエピソードはツールを呼ばなかった」と「誰も記録しなかった」を分ける**のが、
このモジュールが存在する理由そのもの。
"""
import json

import pytest

from relay.selfimprove import episode_record as ER
from relay.selfimprove.controller import EvolutionController
from relay.selfimprove.ledger import HypothesisLedger
from relay import provenance as PROV


def _result():
    return {
        "gate": {"keep": False, "verdict": "underpowered", "reason": "n", "n": 4,
                 "b": 0, "c": 0},
        "sentinel": {"regressed": False},
        "security": {"regressed": False, "passed_count": 2, "comparable": 2},
        "regression": {"regressed": False, "lost": []},
        "infra": {"aborted": False},
        "slice_ids": ["e1", "e2", "e3"],
        "on": {"resolved_ids": ["e1", "e2"], "infra_ids": ["e3"]},
        "off": {"resolved_ids": ["e1"], "infra_ids": []},
        "pool_version": "v7", "grader_version": "g3",
        "security_policy_version": "s2", "execution_profile": "p1",
        "model": "m1", "random_seed": 0,
    }


def _controller(tmp_path, **kw):
    return EvolutionController(
        ledger=HypothesisLedger(str(tmp_path / "h.jsonl")), **kw)


def _run(ctl):
    return ctl.run_candidate(
        genome={"parameters": {"max_retries": 6}},
        hypothesis="a lower retry budget changes nothing on this suite",
        target_failure_class="transient_retry_budget",
        predicted_effect={"metric": "pass_rate", "direction": "none"},
        evidence=[{"kind": "own_measurements", "authority": PROV.AGENT_INFERENCE}],
        evaluate=lambda *_a, **_k: _result())


# ---- 書かれること、そして呼ばれないと書かれないこと ---------------------------------------------

def test_no_path_configured_writes_nothing_and_still_runs(tmp_path):
    """記録の保管は追加機能。書けない環境でループが止まってはいけない。"""
    out = _run(_controller(tmp_path))
    assert out["decision"]["state"]
    assert not list(tmp_path.glob("*.jsonl.records"))


def test_a_configured_path_receives_one_record_per_arm_per_episode(tmp_path):
    path = tmp_path / "records.jsonl"
    _run(_controller(tmp_path, records_path=str(path)))
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 6, "3エピソード×2アームで6行のはず、実際は %d" % len(rows)
    assert {r["episode_id"] for r in rows} == {
        "candidate:e1", "candidate:e2", "candidate:e3",
        "baseline:e1", "baseline:e2", "baseline:e3"}


def test_the_two_arms_are_distinguishable(tmp_path):
    """アームを分けないと、同じ episode_id が2行出て重複に見え、
    `append` の重複検出（あるいは後段の集計）が対を潰す。"""
    path = tmp_path / "records.jsonl"
    _run(_controller(tmp_path, records_path=str(path)))
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    cand = {r["episode_id"]: r for r in rows if r["episode_id"].startswith("candidate:")}
    base = {r["episode_id"]: r for r in rows if r["episode_id"].startswith("baseline:")}
    assert cand["candidate:e2"]["outcome"]["functional_success"] is True
    assert base["baseline:e2"]["outcome"]["functional_success"] is False
    assert cand["candidate:e1"]["harness_id"] != base["baseline:e1"]["harness_id"]


def test_infra_failure_is_not_recorded_as_a_functional_failure(tmp_path):
    """環境が走らせられなかったエピソードは、変更についての証拠ではない。"""
    path = tmp_path / "records.jsonl"
    _run(_controller(tmp_path, records_path=str(path)))
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    e3 = next(r for r in rows if r["episode_id"] == "candidate:e3")
    assert e3["outcome"]["infra_failure"] is True
    assert e3["outcome"]["functional_success"] is False


# ---- 供給できないものは供給しない -----------------------------------------------------------------

def test_what_the_controller_cannot_know_is_named_rather_than_invented(tmp_path):
    """latency も turn 数も状態ハッシュも `paired_evaluate` は返さない。
    0 や {} で埋めると、記録は通って再走行が合わない。"""
    rows = ER.from_paired_result(
        _result(), experiment_id="x", harness_id="h", candidate_parent="p",
        git_commit="abc", model="m", pool_version="v", random_seed=0,
        grader_version="g", security_policy_version="s", execution_profile="e")
    absent = set(rows[0]["not_recorded"])
    for field in ("tool_calls", "latency", "cost", "turn_count", "ts"):
        assert field in absent, "%s が『記録しなかった』と印されていない" % field


def test_a_security_verdict_is_not_claimed_per_episode(tmp_path):
    """対比較の結果に per-episode のセキュリティ判定は無い。
    True で埋めると、走行がしていないセキュリティ合格を報告することになる。"""
    rows = ER.from_paired_result(
        _result(), experiment_id="x", harness_id="h", candidate_parent="p")
    assert rows[0]["outcome"]["security_success"] is None


def test_the_records_validate_so_they_can_actually_be_cited(tmp_path):
    """書けたのに再現に足りない記録は、書けなかったのと同じくらい役に立たない。"""
    rows = ER.from_paired_result(
        _result(), experiment_id="x", harness_id="h", candidate_parent="p",
        git_commit="abc", model="m", pool_version="v", random_seed=0,
        grader_version="g", security_policy_version="s", execution_profile="e")
    for row in rows:
        ER.validate(row)


# ---- 書けなくてもループは進む ---------------------------------------------------------------------

def test_an_unwritable_store_does_not_discard_an_evaluation_that_already_happened(tmp_path):
    """証拠の欠落であって、評価をやり直す理由ではない。"""
    ctl = _controller(tmp_path, records_path=str(tmp_path / "nope" / "deep" / "r.jsonl"))
    out = _run(ctl)
    assert out["decision"]["state"], "記録の失敗が判定を消している"


def test_the_record_is_written_before_the_verdict_is_decided(tmp_path):
    """判定に依存して内容が変わる記録は、走行の記録ではなく判定の記録。"""
    import inspect

    from relay.selfimprove import controller as C
    src = inspect.getsource(C.EvolutionController.run_candidate)
    assert src.index("_write_records") < src.index("verdict = D.decide")

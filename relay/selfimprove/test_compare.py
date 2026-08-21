"""2つの枝の比較。この機能の危険は配管ではなく、答えを製造できてしまうこと。

計器は transport 仮説のために較正されており、ノイズ床は実測 130-180MB、
判定閾値は 300MB。差分が memory_max_items なら、Edge のメモリが動く機序が無いので
20分かけて構造的に INCONCLUSIVE になる。「もう一回走らせる」ボタンと
最良の1回だけを見せる表示が揃うと、それは p-hacking 装置になる。
"""
import json

import pytest

from relay.selfimprove import archive as A
from relay.selfimprove import branches as BR
from relay.selfimprove import compare as C
from relay.selfimprove import manifest as M


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_SELFIMPROVE_BRANCHES", str(tmp_path / "branches.json"))
    monkeypatch.setenv("MCP_SELFIMPROVE_COMPARE_QUEUE", str(tmp_path / "queue.jsonl"))
    monkeypatch.setenv("MCP_SELFIMPROVE_COMPARISONS", str(tmp_path / "comparisons.jsonl"))
    monkeypatch.setattr(BR, "_record", lambda *a, **k: None)
    arc = A.Archive(path=str(tmp_path / "entries.jsonl"))

    def _mk(label, genome):
        gid = arc.add(dict(genome), slice_ids=["e1"], pass_at_1=0.5, ci=(0.4, 0.6),
                      gate_verdict="KEEP")
        BR.create(label, gid, archive=arc)
        return gid

    _mk("slow", {"components": {"transport": "transport/v1"}})
    _mk("fast", {"components": {"transport": "transport/v2"}})
    _mk("mem", {"parameters": {"memory_max_items": 9}})
    return arc


def _clean(monkeypatch, tmp_path):
    """No live refusals: frozen intact, no lock, no tripwire, plenty of memory."""
    from relay.selfimprove import frozen as F
    from relay.selfimprove import scheduler as S
    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (True, []))
    monkeypatch.setattr(S, "lock_held", lambda *a, **k: None)
    monkeypatch.setattr(S, "halt_on_record", lambda *a, **k: {})
    monkeypatch.setattr("relay.relay_fleet.avail_phys_mb", lambda: 99999.0)


# ---- 計器が見える範囲を先に言う -------------------------------------------------------------------

def test_a_difference_the_instrument_cannot_see_is_named_before_the_run(env):
    """20分待たせてから『測れませんでした』は言い訳。始まる前に言う。"""
    a = BR.resolve("mem", archive=env)["manifest"]
    b = BR.resolve("slow", archive=env)["manifest"]
    visible, why = C.instrument_can_see(a, b)
    assert visible is False
    assert "memory_max_items" in why and "twenty minutes" in why


def test_a_transport_difference_is_visible(env):
    a = BR.resolve("fast", archive=env)["manifest"]
    b = BR.resolve("slow", archive=env)["manifest"]
    visible, why = C.instrument_can_see(a, b)
    assert visible is True and "transport" in why


def test_the_diff_is_read_as_the_flat_shape_it_actually_has(env):
    """最初の版は M.diff を入れ子として読み、何も見つけられず、
    あらゆる比較を『2つのハーネスは同一』と報告した --
    機能全体を止めながら、考え抜かれた検査のように見える拒否。"""
    a = BR.resolve("fast", archive=env)["manifest"]
    b = BR.resolve("slow", archive=env)["manifest"]
    assert "components.transport" in M.diff(a, b)
    assert C.instrument_can_see(a, b)[0] is True


def test_the_expectation_is_recorded_at_request_time(env, monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    row = C.enqueue("fast", "slow", archive=env)
    assert "20 minutes" in row["expectation"]
    assert "INCONCLUSIVE is the usual result" in row["expectation"]


# ---- 拒否 ---------------------------------------------------------------------------------------

def test_comparing_a_branch_with_itself_is_refused(env, monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    with pytest.raises(C.CompareError) as exc:
        C.enqueue("fast", "fast", archive=env)
    assert "with itself" in str(exc.value)


def test_two_names_for_one_harness_are_refused(env, monkeypatch, tmp_path):
    """genome_id が違っても同一プログラムになりうる。A/A 比較が2つの名前を着て通る。"""
    _clean(monkeypatch, tmp_path)
    default = M.base_manifest()["parameters"]["max_retries"]
    gid = env.add({"parameters": {"max_retries": default}}, slice_ids=["e1"],
                  pass_at_1=0.5, ci=(0.4, 0.6), gate_verdict="KEEP")
    BR.create("twin", gid, archive=env)
    plain = env.add({}, slice_ids=["e1"], pass_at_1=0.5, ci=(0.4, 0.6), gate_verdict="KEEP")
    BR.create("plain", plain, archive=env)
    with pytest.raises(C.CompareError) as exc:
        C.enqueue("twin", "plain", archive=env)
    assert "same harness" in str(exc.value)


def test_a_changed_frozen_set_refuses(env, monkeypatch, tmp_path):
    from relay.selfimprove import frozen as F
    from relay.selfimprove import scheduler as S
    _clean(monkeypatch, tmp_path)
    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (False, ["grader.py"]))
    reasons = C.refusals("fast", "slow", archive=env)
    assert any("frozen set is not intact" in r for r in reasons), reasons


def test_a_held_lock_refuses_rather_than_queues(env, monkeypatch, tmp_path):
    """待たせると、結果が別の午後に帰属されて届く。
    そして両者の計器は互いを測っている。"""
    from relay.selfimprove import scheduler as S
    _clean(monkeypatch, tmp_path)
    monkeypatch.setattr(S, "lock_held", lambda *a, **k: "12:00")
    reasons = C.refusals("fast", "slow", archive=env)
    assert any("held the lock" in r for r in reasons), reasons
    assert any("measuring the other" in r for r in reasons), reasons


def test_a_fired_tripwire_refuses(env, monkeypatch, tmp_path):
    from relay.selfimprove import scheduler as S
    _clean(monkeypatch, tmp_path)
    monkeypatch.setattr(S, "halt_on_record", lambda *a, **k: {"fired": ["sentinel_regressed"]})
    reasons = C.refusals("fast", "slow", archive=env)
    assert any("tripwire fired" in r for r in reasons), reasons


def test_a_swapping_machine_refuses(env, monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    reasons = C.refusals("fast", "slow", archive=env, free_mb=100.0)
    assert any("swap" in r for r in reasons), reasons


def test_no_token_refuses_because_the_arms_would_be_identical(env, monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    reasons = C.refusals("fast", "slow", archive=env, token_ok=False)
    assert any("same program" in r for r in reasons), reasons


def test_every_reason_is_reported_at_once(env, monkeypatch, tmp_path):
    """1つ直して20分後に次で落ちる、を避ける。"""
    from relay.selfimprove import scheduler as S
    _clean(monkeypatch, tmp_path)
    monkeypatch.setattr(S, "lock_held", lambda *a, **k: "12:00")
    reasons = C.refusals("fast", "slow", archive=env, free_mb=100.0, token_ok=False)
    assert len(reasons) >= 3, reasons


# ---- 判定 ---------------------------------------------------------------------------------------

def _order(gain, done_a=4, done_b=4):
    return {"memory_gain_mb": gain,
            "control": {"done": done_a}, "candidate": {"done": done_b}}


def test_a_sign_that_does_not_survive_the_swap_is_not_a_sign():
    """腕の位置が効果より大きかった実測(+449 then -666)を、
    交絡から対照へ変えるのがこの規則。"""
    got = C.decide(_order(400), _order(-500))
    assert got["verdict"] == C.VERDICT_NONE
    assert "sign flipped" in got["why"]


def test_both_orderings_clearing_the_threshold_is_a_verdict():
    got = C.decide(_order(400), _order(350))
    assert got["verdict"] == C.VERDICT_A


def test_the_other_direction_names_the_other_branch():
    got = C.decide(_order(-400), _order(-350))
    assert got["verdict"] == C.VERDICT_B


def test_a_consistent_but_small_difference_is_inconclusive():
    """ノイズ床の内側。『どちらかが悪い』という発見ではない。"""
    got = C.decide(_order(100), _order(120))
    assert got["verdict"] == C.VERDICT_NONE
    assert "not a finding" in got["why"]


def test_losing_completion_settles_it_whatever_the_memory_says():
    got = C.decide(_order(900, done_b=3), _order(900, done_b=3))
    assert got["verdict"] == C.VERDICT_A
    assert "finishes fewer goals" in got["why"]


# ---- 全試行が残ること ---------------------------------------------------------------------------

def test_every_attempt_on_a_pair_is_kept_not_the_best_one(env, monkeypatch, tmp_path):
    """最良の1回だけを『もう一度走らせる』ボタンの隣に出すと、
    運用者が欲しい答えを製造する装置になる。履歴が唯一の歯止めで、
    画面に出ていて初めて歯止めになる。"""
    _clean(monkeypatch, tmp_path)
    req = C.enqueue("fast", "slow", archive=env)
    C.record(req, _order(100), _order(120), C.decide(_order(100), _order(120)))
    C.record(req, _order(400), _order(350), C.decide(_order(400), _order(350)))
    got = C.attempts_for("fast", "slow")
    assert len(got) == 2
    assert [r["verdict"] for r in got] == [C.VERDICT_NONE, C.VERDICT_A]


def test_a_refusal_at_run_time_is_recorded_against_the_request(env, monkeypatch, tmp_path):
    """記録しないと、実行時に拒否された依頼が永遠に queued に見え、
    運用者が再投入する -- 同じ拒否が20分後に黙って繰り返される。"""
    _clean(monkeypatch, tmp_path)
    req = C.enqueue("fast", "slow", archive=env)
    assert len(C.pending()) == 1
    C.record_refusal(req, ["the frozen set is not intact"])
    assert C.pending() == []
    assert C.read_results()[-1]["verdict"] is None


# ---- 走行 ---------------------------------------------------------------------------------------

def test_run_uses_both_orders_and_both_branches(env, monkeypatch, tmp_path):
    """両腕が2つの枝であること、両順序で走ることを、注入した評価器で確かめる。"""
    _clean(monkeypatch, tmp_path)
    from relay.selfimprove import scheduler as S
    monkeypatch.setattr(S, "take_lock", lambda *a, **k: True)
    monkeypatch.setattr(S, "release_lock", lambda *a, **k: None)
    seen = []

    def build(manifest_a, manifest_b, candidate_first):
        def evaluate(candidate, experiment_id, base=None):
            seen.append({"control": manifest_a["components"]["transport"],
                         "candidate": manifest_b["components"]["transport"],
                         "candidate_first": candidate_first})
            return _order(400 if not candidate_first else 350)
        return evaluate

    req = C.enqueue("fast", "slow", archive=env)
    row = C.run(req, archive=env, evaluator_for=build)
    assert [s["candidate_first"] for s in seen] == [False, True]
    assert seen[0]["control"] == "transport/v2" and seen[0]["candidate"] == "transport/v1"
    assert row["verdict"] == C.VERDICT_A
    assert len(row["orders"]) == 2


def test_run_records_a_refusal_instead_of_running(env, monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    from relay.selfimprove import frozen as F
    from relay.selfimprove import scheduler as S
    monkeypatch.setattr(S, "take_lock", lambda *a, **k: True)
    monkeypatch.setattr(S, "release_lock", lambda *a, **k: None)
    req = C.enqueue("fast", "slow", archive=env)
    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (False, ["grader.py"]))
    called = []
    row = C.run(req, archive=env, evaluator_for=lambda *a: called.append(1))
    assert called == [], "拒否されたのに腕が走った"
    assert row["verdict"] is None and row["refused"]


def test_the_lock_is_released_even_if_an_arm_raises(env, monkeypatch, tmp_path):
    """ロックが残ると、以後どの比較もキャンペーンも走れない。"""
    _clean(monkeypatch, tmp_path)
    from relay.selfimprove import scheduler as S
    released = []
    monkeypatch.setattr(S, "take_lock", lambda *a, **k: True)
    monkeypatch.setattr(S, "release_lock", lambda *a, **k: released.append(1))

    def boom(*a, **k):
        raise RuntimeError("arm died")

    req = C.enqueue("fast", "slow", archive=env)
    with pytest.raises(RuntimeError):
        C.run(req, archive=env, evaluator_for=boom)
    assert released == [1]


def test_the_refusals_run_again_inside_the_lock(env, monkeypatch, tmp_path):
    """enqueue 時の検査は、20分前の状態に同意した検査でしかない。"""
    import inspect
    src = inspect.getsource(C.run)
    assert src.index("take_lock") < src.index("refusals("), "ロックの外で再検査している"


def test_running_stamps_both_branches_so_staleness_is_visible(env, monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    from relay.selfimprove import scheduler as S
    monkeypatch.setattr(S, "take_lock", lambda *a, **k: True)
    monkeypatch.setattr(S, "release_lock", lambda *a, **k: None)
    req = C.enqueue("fast", "slow", archive=env)
    C.run(req, archive=env, evaluator_for=lambda *a: (lambda *b, **k: _order(10)))
    refs = BR.read()
    assert refs["fast"]["last_run_at"] and refs["slow"]["last_run_at"]


def test_the_comparison_never_writes_the_active_manifest():
    """走らせても運用者の帰り道は消えない、が全体の安全論拠。"""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(C).lstrip())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    code = ast.unparse(tree)
    assert "write_active" not in code and "ACTIVE_PATH" not in code


# ---- 基底は ref ではなく比較のオペランド ---------------------------------------------------------

def test_a_branch_can_be_compared_against_what_shipped(env, monkeypatch, tmp_path):
    """運用者が最初に訊く質問。枝2本を要求する設計ではこれが表現できない --
    archive は候補を持ち、基底は候補ではないので、
    2つ目のラベルを向ける行が最初から存在しない。"""
    _clean(monkeypatch, tmp_path)
    row = C.enqueue("fast", "base", archive=env)
    assert row["b"]["label"] == "base"
    assert row["b"]["genome_id"] is None
    assert row["instrument_can_see"] is True


def test_base_as_an_operand_does_not_create_a_ref(env, monkeypatch, tmp_path):
    """ref にすると基底に第二の定義ができ、コードから乖離しうる。
    オペランドとして受けるのは、その不変条件を壊さない。"""
    _clean(monkeypatch, tmp_path)
    C.enqueue("fast", "base", archive=env)
    assert "base" not in BR.read()
    with pytest.raises(BR.BranchError):
        BR.create("base", "whatever", archive=env)


def test_the_base_operand_is_constructed_not_read(env):
    """読む先が無いから古くなりようがない、が基底の性質。"""
    got = C.resolve_operand("base", archive=env)
    assert got["manifest"] == M.base_manifest()
    assert got["harness_id"] == M.harness_id(M.base_manifest())


def test_a_branch_identical_to_base_is_still_refused(env, monkeypatch, tmp_path):
    """オペランドを増やしても、同一プログラム同士の比較は通してはいけない。"""
    _clean(monkeypatch, tmp_path)
    gid = env.add({}, slice_ids=["e1"], pass_at_1=0.5, ci=(0.4, 0.6), gate_verdict="KEEP")
    BR.create("plain", gid, archive=env)
    with pytest.raises(C.CompareError) as exc:
        C.enqueue("plain", "base", archive=env)
    assert "same harness" in str(exc.value)


def test_running_against_base_stamps_only_the_branch(env, monkeypatch, tmp_path):
    """基底には鮮度が無い。刻む ref も無い。"""
    _clean(monkeypatch, tmp_path)
    from relay.selfimprove import scheduler as S
    monkeypatch.setattr(S, "take_lock", lambda *a, **k: True)
    monkeypatch.setattr(S, "release_lock", lambda *a, **k: None)
    req = C.enqueue("fast", "base", archive=env)
    C.run(req, archive=env, evaluator_for=lambda *a: (lambda *b, **k: _order(10)))
    assert BR.read()["fast"]["last_run_at"]
    assert "base" not in BR.read()

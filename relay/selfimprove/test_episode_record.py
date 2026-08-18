"""エピソード記録。§19 のスキーマ、§20 の再現性、§21 の「言ってよいこと」。

中心は2つ:

  * **完全に見えて再現できない記録は、記録が無いより悪い。** 再走行を誘い、出た差が
    「変更のせい」に帰属され、実際に動いていた未記録の4項目は誰も見ない。
  * **「X から Y へ改善した」は、最適化に使ったデータと汎化推定に使ったデータが
    区別できるときにしか言えない。** 繰り返し覗いた集合は2度目の時点で最適化
    フィードバックに変わっており、そう呼ぶのをやめた時点ではない。
"""
import json

import pytest

from relay.selfimprove import episode_record as E


def _full(**over):
    base = dict(
        episode_id="e1", experiment_id="x1", task_class="spreadsheet",
        harness_id="h1", execution_profile="p", git_commit="abc123",
        candidate_parent="h0", model="m1", pool="sealed", pool_version="v3",
        random_seed=0, grader_version="g2", security_policy_version="s5",
        start_state_hash="aaa", end_state_hash="bbb", turn_count=4,
        outcome={"functional_success": True, "security_success": True,
                 "infra_failure": False},
        raw_trace_path="runs/e1.jsonl", ts=100.0)
    base.update(over)
    return E.build(**base)


# ---- §20: 再現できない記録は保存しない ---------------------------------------------------------

def test_a_complete_record_validates():
    assert E.validate(_full())["episode_id"] == "e1"
    assert E.missing_for_reproduction(_full()) == []


def test_each_missing_reproducibility_field_is_named():
    """何が足りないかを言わない拒否は、次に何をすればいいか分からない拒否。"""
    for field in E.REPRODUCIBILITY_FIELDS:
        rec = _full()
        rec[field] = None if field == "random_seed" else ""
        assert field in E.missing_for_reproduction(rec), field
        with pytest.raises(E.RecordError) as exc:
            E.validate(rec)
        assert field in str(exc.value)


def test_a_seed_of_zero_is_a_seed():
    """`if not value` だと 0 が未設定になる -- 時刻 0.0 を未設定と読むのと同じ誤り。"""
    assert E.missing_for_reproduction(_full(random_seed=0)) == []
    assert "random_seed" in E.missing_for_reproduction(_full(random_seed=None))


def test_absent_is_stored_as_absent_not_as_a_plausible_default():
    """既定で埋めると、記録は通って再走行が合わない。
    『誰も記録しなかった』と『空だった』は読み分けられねばならない。"""
    rec = E.build(episode_id="e2")
    assert rec["git_commit"] == "" and rec["random_seed"] is None
    assert rec["component_versions"] == {}
    with pytest.raises(E.RecordError):
        E.validate(rec)


def test_the_field_list_matches_what_the_brief_asks_to_reconstruct():
    for field in ("git_commit", "harness_id", "candidate_parent", "model",
                  "random_seed", "grader_version", "security_policy_version"):
        assert field in E.REPRODUCIBILITY_FIELDS, field


# ---- §19: raw と compact を分ける ---------------------------------------------------------------

def test_the_summary_keeps_counts_and_leaves_the_contents_in_the_raw_trace():
    """全ツール呼び出しを載せた要約は、ヘッダ付きのトランスクリプト。
    そして実際に読まれるのは要約のほう。"""
    rec = _full(tool_calls=[{"name": "read_file"}] * 12,
                security_events=[{"kind": "blocked"}] * 2)
    got = E.compact(rec)
    assert got["tool_calls_count"] == 12 and got["security_events_count"] == 2
    assert "tool_calls" not in got and "security_events" not in got
    assert got["raw_trace_path"] == "runs/e1.jsonl", "生トレースへの参照が落ちている"


def test_the_summary_still_carries_everything_a_comparison_needs():
    got = E.compact(_full())
    for field in E.REPRODUCIBILITY_FIELDS:
        assert field in got, field
    assert got["outcome"]["functional_success"] is True


# ---- §20: append-only、ただし検証してから ---------------------------------------------------------

def test_appending_an_unreproducible_record_is_refused(tmp_path):
    """通してしまう保管庫は、誰も引用できない保管庫になる。"""
    path = tmp_path / "records.jsonl"
    with pytest.raises(E.RecordError):
        E.append(str(path), E.build(episode_id="e9"))
    assert not path.exists()


def test_records_append_one_line_each(tmp_path):
    path = tmp_path / "records.jsonl"
    E.append(str(path), _full(episode_id="a"))
    E.append(str(path), _full(episode_id="b"))
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert [json.loads(l)["episode_id"] for l in lines] == ["a", "b"]


# ---- §21: 「改善した」と言ってよいか ---------------------------------------------------------------

def test_evolution_pool_numbers_cannot_be_called_an_improvement():
    """最適化に使った当のデータから出た数は、適合の推定であって汎化の推定ではない。"""
    got = E.may_claim_improvement([_full(pool="evolution")])
    assert got["may_claim"] is False
    assert "optimiser's own feedback" in got["reason"]


def test_a_held_out_pool_read_once_may_carry_the_claim():
    got = E.may_claim_improvement([_full(pool="sealed")], pool_reads={"sealed": 1})
    assert got["may_claim"] is True


def test_a_held_out_pool_read_twice_has_stopped_being_held_out():
    """変わるのは2度目の時点であって、そう呼ぶのをやめた時点ではない。"""
    got = E.may_claim_improvement([_full(pool="sealed")], pool_reads={"sealed": 2})
    assert got["may_claim"] is False
    assert "second look" in got["reason"]


def test_mixing_both_kinds_is_allowed_but_must_be_stated():
    got = E.may_claim_improvement(
        [_full(pool="evolution"), _full(episode_id="e2", pool="sealed")],
        pool_reads={"sealed": 1})
    assert got["may_claim"] is True
    # 部分文字列だけを見ていたので、最適化用と汎化用を**逆に**説明する文でも通っていた。
    # 語順ではなく対応関係を検査する -- どちらのプールがどちらの役割として名指されるか。
    reason = got["reason"]
    assert reason.index("evolution") < reason.index("used to tune"), (
        "最適化に使ったプールが『tune に使った』側に名指されていない: %r" % reason)
    assert reason.index("sealed") < reason.index("estimates generalisation"), (
        "held-out プールが汎化推定の側に名指されていない: %r" % reason)


def test_no_records_is_not_permission():
    assert E.may_claim_improvement([])["may_claim"] is False


def test_nothing_here_returns_a_number():
    """差が本物かは別の問い（有意性ゲート）。ここが答えるのは
    『その文を言ってよいか』だけ。"""
    got = E.may_claim_improvement([_full(pool="sealed")], pool_reads={"sealed": 1})
    assert set(got) == {"may_claim", "reason", "pools"}
    for word in ("p_value", "delta", "pp", "score"):
        assert word not in json.dumps(got).lower()


def test_an_unnamed_pool_is_not_quietly_treated_as_held_out():
    got = E.may_claim_improvement([_full(pool="")])
    assert got["may_claim"] is False


# ---- 「記録しなかった」と「無かった」（レビュー指摘） -------------------------------------------

def test_unsupplied_telemetry_is_named_rather_than_shown_as_empty():
    """`[]` にすると『誰も記録しなかった』と『1件も起きなかった』が同じ行になる。
    そして要約は両方を 0 と報告する -- 片方だけが安心してよい事実。"""
    rec = E.build(episode_id="e1")
    assert "tool_calls" in rec["not_recorded"]
    assert "security_events" in rec["not_recorded"]

    supplied = E.build(episode_id="e2", tool_calls=[], security_events=[])
    assert "tool_calls" not in supplied["not_recorded"]


def test_the_summary_reports_unrecorded_telemetry_as_unknown_not_as_zero():
    absent = E.compact(_full())                       # telemetry 未指定
    assert absent["security_events_count"] is None
    present = E.compact(_full(security_events=[]))
    assert present["security_events_count"] == 0


def test_a_defaulted_timestamp_is_flagged_as_the_build_time():
    """後日の解析パスで作った行が、走行時の記録を名乗れてはいけない。"""
    assert "ts" in E.build(episode_id="e1")["not_recorded"]
    assert "ts" not in E.build(episode_id="e1", ts=5.0)["not_recorded"]


def test_the_summary_keeps_what_two_harnesses_differ_by():
    """`component_versions` は比較の対象そのもの、`verification` は走行が証明したもの。
    どちらもトランスクリプトの嵩ではなく、要約が存在する理由の側。"""
    got = E.compact(_full(component_versions={"memory": "memory/v2"},
                          verification={"checks_passed": 3}))
    assert got["component_versions"] == {"memory": "memory/v2"}
    assert got["verification"] == {"checks_passed": 3}

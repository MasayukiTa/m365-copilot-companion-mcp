"""判定器ラッパ(judge_wrap)の単体試験。bridge_judge を DI で差し替える。

judge_result.json の実物(w67)の形を素材に、ラッパが数値を作らず読み取るだけで
あること、transport 失敗を GO でない扱いに倒さず例外にすることを確認する。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.weekly_scan.judge_wrap import (  # noqa: E402
    JudgeTransportError,
    parse_judge_result,
    run_judge,
)


# w67 の judge_result.json を模した最小の構造(実物の値を使用)。
_W67_RESULT = {
    "order": {"order_ok": False, "registered_at": "2026-11-02T23:36:47.342067+09:00"},
    "verdict": {
        "verdict": "HOLD",
        "verdict_ja": "保留",
        "reasons": ["D_FAIL_NO_LOCK", "D_FAIL_EXPLORATORY"],
        "evidence": {
            "K": 18829,
            "ci_std": [0.335499271521014, 0.4733908347236796],
            "delta_min_std": 0.2,
            "df": 18827,
            "estimate_std": 0.4044450531223468,
        },
    },
}


def test_parse_w67_reads_fields_verbatim():
    out = parse_judge_result(json.dumps(_W67_RESULT))
    assert out.verdict == "HOLD"
    assert out.is_go is False
    assert out.order_ok is False
    assert out.reasons == ["D_FAIL_NO_LOCK", "D_FAIL_EXPLORATORY"]
    assert out.k == 18829
    assert out.ci_std == [0.335499271521014, 0.4733908347236796]
    assert out.delta_min_std == 0.2
    assert out.estimate_std == 0.4044450531223468


def test_interval_above_delta_min_true_for_w67():
    # w67 は区間 [0.3355, 0.4734] が 0 を含まず下限が Δmin 0.2 超。
    out = parse_judge_result(json.dumps(_W67_RESULT))
    assert out.interval_above_delta_min is True


def test_interval_includes_zero_is_false():
    data = json.loads(json.dumps(_W67_RESULT))
    data["verdict"]["evidence"]["ci_std"] = [-0.09, 0.17]
    out = parse_judge_result(json.dumps(data))
    assert out.interval_above_delta_min is False


def test_go_verdict_detected():
    data = json.loads(json.dumps(_W67_RESULT))
    data["verdict"]["verdict"] = "GO"
    data["order"]["order_ok"] = True
    out = parse_judge_result(json.dumps(data))
    assert out.is_go is True
    assert out.order_ok is True


def test_flat_verdict_string_form():
    out = parse_judge_result(json.dumps({"verdict": "NO", "reasons": ["X"]}))
    assert out.verdict == "NO"
    assert out.reasons == ["X"]


def test_missing_verdict_raises():
    with pytest.raises(JudgeTransportError):
        parse_judge_result(json.dumps({"order": {"order_ok": True}}))


def test_non_json_raises():
    with pytest.raises(JudgeTransportError):
        parse_judge_result("not json at all")


def test_empty_raises():
    with pytest.raises(JudgeTransportError):
        parse_judge_result("")


def test_run_judge_passes_request_unchanged():
    captured = {}

    def fake_judge(request_json):
        captured["req"] = request_json
        return json.dumps(_W67_RESULT)

    req = json.dumps({"id": "mcl_shrink_sonota", "delta_min": {"value": 0.2}})
    out = run_judge(req, judge_fn=fake_judge)
    # ラッパは request をそのまま判定器へ渡す(書き換えない)。
    assert captured["req"] == req
    assert out.verdict == "HOLD"


def test_run_judge_transport_failure_raises_not_go():
    def failing_judge(request_json):
        raise RuntimeError("bridge could not open a fresh conversation for judging")

    with pytest.raises(JudgeTransportError):
        run_judge("{}", judge_fn=failing_judge)


def test_parse_accepts_dict_directly():
    out = parse_judge_result(_W67_RESULT)
    assert out.verdict == "HOLD"

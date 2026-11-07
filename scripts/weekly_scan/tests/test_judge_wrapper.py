# -*- coding: utf-8 -*-
"""judge_wrapper の単体試験。判定器本体は呼ばず、ダミーの呼び出し関数を差す。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve()
_SCRIPTS = _HERE.parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from weekly_scan.judge_wrapper import (  # noqa: E402
    JudgeVerdict,
    parse_judge_result,
    run_judge,
)


# 実物 judge_result.json の形（トップとネスト verdict の双方に同名フィールド）を模す。
_SAMPLE = {
    "claim_id": "mcl_p1",
    "verdict": {
        "id": "mcl_p1",
        "verdict": "HOLD",
        "claim_supported": False,
        "finding": "ORDER_NOT_ESTABLISHED",
        "reasons": ["registered_at after data_fetched_at"],
        "notes": ["LOT_INDEPENDENCE_NOT_CHECKED"],
        "rules_version": "v1_8c2",
        "hashes": {"prereg": "abc"},
        "evidence": {
            "estimate_std": 0.42,
            "ci_std": [0.10, 0.74],
            "delta_min_std": 0.20,
            "K": 12,
            "df": 10,
        },
    },
    "order": {"order_ok": False, "registered_at": "2026-10-01T00:00:00+09:00"},
}


class ParseTests(unittest.TestCase):
    def test_parses_nested_verdict(self):
        v = parse_judge_result(_SAMPLE)
        self.assertEqual(v.claim_id, "mcl_p1")
        self.assertEqual(v.verdict, "HOLD")
        self.assertTrue(v.is_hold)
        self.assertFalse(v.is_go)
        self.assertEqual(v.finding, "ORDER_NOT_ESTABLISHED")
        self.assertEqual(v.order_ok, False)
        self.assertEqual(v.K, 12)
        self.assertEqual(v.ci_std, [0.10, 0.74])
        self.assertEqual(v.delta_min, 0.20)
        self.assertEqual(v.rules_version, "v1_8c2")

    def test_interval_inside_delta_min(self):
        # ci が ±delta_min の外 → False
        v = parse_judge_result(_SAMPLE)
        self.assertFalse(v.interval_inside_delta_min)
        # 内側に収まる例
        inside = dict(_SAMPLE)
        inside = {**_SAMPLE, "verdict": {**_SAMPLE["verdict"],
                  "evidence": {**_SAMPLE["verdict"]["evidence"],
                               "ci_std": [-0.05, 0.05], "delta_min_std": 0.20}}}
        v2 = parse_judge_result(inside)
        self.assertTrue(v2.interval_inside_delta_min)

    def test_interval_unknown_when_missing(self):
        nodelta = {**_SAMPLE, "verdict": {**_SAMPLE["verdict"],
                   "evidence": {"K": 3}}}
        v = parse_judge_result(nodelta)
        self.assertIsNone(v.interval_inside_delta_min)

    def test_go_verdict_detected(self):
        go = {**_SAMPLE, "verdict": {**_SAMPLE["verdict"], "verdict": "GO"}}
        v = parse_judge_result(go)
        self.assertTrue(v.is_go)

    def test_missing_claim_id_raises(self):
        bad = {"verdict": {"verdict": "GO"}}
        with self.assertRaises(ValueError):
            parse_judge_result(bad)

    def test_parse_json_string(self):
        import json
        v = parse_judge_result(json.dumps(_SAMPLE))
        self.assertEqual(v.claim_id, "mcl_p1")


class RunJudgeTests(unittest.TestCase):
    def test_run_judge_passes_config_id_and_normalises(self):
        seen = {}

        def fake_judge(config_id, engine_version=""):
            seen["config_id"] = config_id
            seen["engine_version"] = engine_version
            return _SAMPLE

        v = run_judge("mcl_p1", fake_judge, engine_version="v1_8c2")
        self.assertEqual(seen["config_id"], "mcl_p1")
        self.assertEqual(seen["engine_version"], "v1_8c2")
        self.assertIsInstance(v, JudgeVerdict)
        self.assertEqual(v.verdict, "HOLD")

    def test_run_judge_accepts_json_string_return(self):
        import json

        def fake_judge(config_id, engine_version=""):
            return json.dumps(_SAMPLE)

        v = run_judge("mcl_p1", fake_judge)
        self.assertEqual(v.claim_id, "mcl_p1")

    def test_run_judge_rejects_empty_config_id(self):
        with self.assertRaises(ValueError):
            run_judge("", lambda **k: _SAMPLE)

    def test_run_judge_does_not_invent_verdict(self):
        # ラッパは判定器の返値をそのまま写すだけ（verdict を捧造しない）。
        def fake_judge(config_id, engine_version=""):
            return {"claim_id": "x", "verdict": {"verdict": "NA", "finding": "UNIDENTIFIED"}}
        v = run_judge("x", fake_judge)
        self.assertEqual(v.verdict, "NA")
        self.assertFalse(v.is_go)

    def test_run_judge_propagates_callable_error(self):
        # judge_callable が投げた例外は握りつぶさず呼び出し元へ伝播する。
        class JudgeError(Exception):
            pass

        def boom(config_id, engine_version=""):
            raise JudgeError("engine fault")

        with self.assertRaises(JudgeError):
            run_judge("mcl_p1", boom)


class OrderOkTests(unittest.TestCase):
    def test_order_ok_none_when_order_block_missing(self):
        # order ブロックが無ければ order_ok は None（偽の値を捏造しない）。
        no_order = {"claim_id": "y", "verdict": {"verdict": "GO"}}
        v = parse_judge_result(no_order)
        self.assertIsNone(v.order_ok)

    def test_order_ok_true_from_top_level_order(self):
        # トップ階層 order の order_ok を素通しで写す。
        d = {"claim_id": "z", "verdict": "GO", "order": {"order_ok": True}}
        v = parse_judge_result(d)
        self.assertTrue(v.order_ok)
        self.assertTrue(v.is_go)


if __name__ == "__main__":
    unittest.main()

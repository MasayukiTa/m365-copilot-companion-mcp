# -*- coding: utf-8 -*-
"""Tests for scripts.weekly_scan.judge_wrapper.

Hermetic: builds judge_result.json dicts in-memory / under tmp_path. No network, no
judge engine, no DB. The fixture mirrors the real judge_result.json layout (verdict
object with evidence + diagnostics.capacity, and a top-level order object) seen in the
operator's result folders, with the w67 numbers used as a realistic example.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.weekly_scan import judge_wrapper as J


def _w67_like() -> dict:
    """A judge_result.json shaped like the real w67 one: HOLD, interval excludes 0
    and is above delta_min, order_ok False (post-hoc)."""
    return {
        "claim_id": "mcl_shrink_sonota",
        "order": {
            "order_ok": False,
            "registered_at": "2026-11-02T23:36:47.342067+09:00",
            "registered_at_trusted": True,
            "data_fetched_at": "2026-11-02T23:34:52.883557+09:00",
            "data_file_mtime": "2026-11-02T23:34:02.067427+09:00",
            "lock_recorded": True,
            "order_reason": "trusted registration is NOT before the data file mtime",
        },
        "verdict": {
            "id": "mcl_shrink_sonota",
            "verdict": "HOLD",
            "verdict_ja": "\u4fdd\u7559",
            "reasons": ["D_FAIL_NO_LOCK", "D_FAIL_EXPLORATORY"],
            "rules_version": "1.8c",
            "evidence": {
                "K": 18829,
                "df": 18827,
                "loo_n": 18829,
                "estimate_std": 0.4044450531223468,
                "ci_std": [0.335499271521014, 0.4733908347236796],
                "delta_min_std": 0.2,
            },
            "diagnostics": {
                "capacity": {
                    "K": 18829,
                    "k_min": 8,
                    "k_min_shortfall": 0,
                    "k_required_p80": 886,
                    "k_required_p90": 904,
                    "additional_lots_p80": 0,
                    "additional_lots_p90": 0,
                    "computable": True,
                },
            },
        },
    }


def _no_effect_like() -> dict:
    """A result whose interval sits inside +/- delta_min (class-A 'no effect')."""
    d = _w67_like()
    d["verdict"]["evidence"]["ci_std"] = [-0.05, 0.08]
    d["verdict"]["evidence"]["delta_min_std"] = 0.2
    return d


def test_parse_reads_verdict_and_evidence():
    r = J.parse_judge_result(_w67_like())
    assert r.id == "mcl_shrink_sonota"
    assert r.verdict == "HOLD"
    assert r.verdict_ja == "\u4fdd\u7559"
    assert r.reasons == ("D_FAIL_NO_LOCK", "D_FAIL_EXPLORATORY")
    assert r.rules_version == "1.8c"
    assert r.K == 18829
    assert r.df == 18827
    assert r.loo_n == 18829
    assert r.ci_std == (0.335499271521014, 0.4733908347236796)
    assert r.delta_min_std == 0.2


def test_order_is_read_verbatim_not_recomputed():
    r = J.parse_judge_result(_w67_like())
    assert r.order is not None
    assert r.order.order_ok is False
    assert r.order.registered_at_trusted is True
    assert r.order.data_file_mtime == "2026-11-02T23:34:02.067427+09:00"
    # The wrapper must not invent an order verdict when the object is absent.
    d = _w67_like()
    del d["order"]
    assert J.parse_judge_result(d).order is None


def test_capacity_is_read_from_diagnostics():
    r = J.parse_judge_result(_w67_like())
    assert r.capacity is not None
    assert r.capacity.k_required_p80 == 886
    assert r.capacity.k_required_p90 == 904
    assert r.capacity.additional_lots_p80 == 0
    assert J.additional_lots_needed(r, "p80") == 0
    assert J.additional_lots_needed(r, "p90") == 0


def test_interval_flags():
    r = J.parse_judge_result(_w67_like())
    # w67: interval excludes 0, and is NOT inside +/- delta_min.
    assert r.interval_excludes_zero is True
    assert r.interval_inside_threshold is False
    # no-effect fixture: interval inside threshold.
    n = J.parse_judge_result(_no_effect_like())
    assert n.interval_inside_threshold is True
    assert n.interval_excludes_zero is False


def test_missing_evidence_yields_none_not_zero():
    d = _w67_like()
    del d["verdict"]["evidence"]
    r = J.parse_judge_result(d)
    assert r.K is None
    assert r.ci_std is None
    assert r.interval_inside_threshold is None


def test_flat_fallback_shape():
    flat = {
        "id": "x",
        "verdict": "HOLD",
    }
    # A flat shape where verdict is a bare string must not crash; it falls back to
    # treating the top level as the verdict object.
    r = J.parse_judge_result(flat)
    assert r.verdict == "HOLD"
    assert r.id == "x"


def test_read_from_folder(tmp_path: Path):
    folder = tmp_path / "w67"
    folder.mkdir()
    (folder / "judge_result.json").write_text(
        json.dumps(_w67_like(), ensure_ascii=False), encoding="utf-8"
    )
    r = J.read_result_folder(folder)
    assert r.verdict == "HOLD"
    assert r.K == 18829


def test_missing_file_is_loud(tmp_path: Path):
    with pytest.raises(J.JudgeReadError):
        J.read_judge_result(tmp_path / "nope.json")
    with pytest.raises(J.JudgeReadError):
        J.read_result_folder(tmp_path)


def test_corrupt_json_is_loud(tmp_path: Path):
    p = tmp_path / "judge_result.json"
    p.write_text("{bad", encoding="utf-8")
    with pytest.raises(J.JudgeReadError):
        J.read_judge_result(p)

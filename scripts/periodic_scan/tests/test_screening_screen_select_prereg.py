"""単体試験: スクリーニング本体・対象選定・事前登録。合成データ・一時ファイルのみ。"""

import json
import os

from scripts.periodic_scan.screening.screen import (
    screen_result_item,
    STRONG_THRESHOLD,
)
from scripts.periodic_scan.screening.select_targets import select_targets
from scripts.periodic_scan.screening.preregister import (
    build_prereg_record,
    preregister_strong,
)


def _rows_strong():
    # 3つの層別キー値すべてで大きく一貫した効果、交絡なし → strong になりやすい。
    rows = []
    for product in ("A", "B", "C"):
        for i in range(6):
            rows.append({"lot": "t%s%d" % (product, i), "product": product,
                         "line": "L1", "date": "2026-08-01",
                         "exposure": "treated", "value": 2.0})
            rows.append({"lot": "c%s%d" % (product, i), "product": product,
                         "line": "L1", "date": "2026-08-01",
                         "exposure": "control", "value": 1.0})
    return rows


def test_screen_emits_candidate_schema():
    cand = screen_result_item(
        {"result_item": "指標値", "rows": _rows_strong(), "min_lots_per_group": 5},
        today="2026-11-04",
    )
    assert cand["candidate_id"] == "screen-指標値-20261104"
    assert cand["result_item"] == "指標値"
    assert cand["pooled_delta"] > 0
    assert 0.0 <= cand["strength"] <= 1.0
    assert isinstance(cand["strong"], bool)
    assert "observed" in cand and "missing" in cand
    assert cand["observed"]["usable_strata"] == 3


def test_strong_flag_tracks_threshold():
    cand = screen_result_item(
        {"result_item": "v", "rows": _rows_strong(), "min_lots_per_group": 5,
         "delta_min": 1.0},
        today="2026-11-04",
    )
    assert cand["strong"] == (cand["strength"] >= STRONG_THRESHOLD)


def test_select_targets_new_and_grew_only():
    all_items = [
        {"result_item": "新規", "record_count": 3},
        {"result_item": "増えた", "record_count": 10},
        {"result_item": "不変", "record_count": 5},
    ]
    prior = {"増えた": 7, "不変": 5}
    out = select_targets(all_items, prior)
    names = {o["result_item"]: o for o in out}
    assert set(names) == {"新規", "増えた"}  # 不変は取り逃しではなく今回対象外
    assert names["新規"]["reason"] == "new"
    assert names["増えた"]["reason"] == "grew"
    assert names["増えた"]["prior_count"] == 7


def test_build_prereg_record_uses_registered_clock():
    cand = {"candidate_id": "screen-v-20261104", "result_item": "v",
            "pooled_delta": 0.5}
    rec = build_prereg_record(cand, now_iso="2026-11-04T00:00:00Z")
    assert rec["order_clock"] == {"source": "registered", "state": "initial"}
    assert rec["preregistered_at"] == "2026-11-04T00:00:00Z"
    assert "pooled_delta=0.5" in rec["claim"]


def test_preregister_strong_writes_only_strong_and_dedupes(tmp_path):
    path = os.path.join(str(tmp_path), "prereg.jsonl")
    cands = [
        {"candidate_id": "a", "result_item": "A", "pooled_delta": 1.0, "strong": True},
        {"candidate_id": "b", "result_item": "B", "pooled_delta": 0.1, "strong": False},
    ]
    w1 = preregister_strong(cands, path, now_iso="2026-11-04T00:00:00Z")
    assert [r["candidate_id"] for r in w1] == ["a"]
    # 二度目は重複しない
    w2 = preregister_strong(cands, path, now_iso="2026-11-04T00:00:00Z")
    assert w2 == []
    lines = [l for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["candidate_id"] == "a"

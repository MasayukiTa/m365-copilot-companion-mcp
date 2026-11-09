"""単体試験: 交絡検出 (confound) と品名層別推定 (stratify)。合成データのみ。"""

from scripts.periodic_scan.screening.confound import (
    detect_confounding,
    IMBALANCE_THRESHOLD,
)
from scripts.periodic_scan.screening.stratify import estimate_stratified


def _row(lot, product, line, date, exposure, value):
    return {
        "lot": lot,
        "product": product,
        "line": line,
        "date": date,
        "exposure": exposure,
        "value": value,
    }


def test_no_confounding_when_groups_balanced_across_axes():
    # 2品名・2ライン・同一時期で、各セルに treated/control が均等。
    rows = []
    for product in ("A", "B"):
        for line in ("L1", "L2"):
            rows.append(_row("x", product, line, "2026-08-01", "treated", 1.0))
            rows.append(_row("y", product, line, "2026-08-01", "control", 1.0))
    rep = detect_confounding({"result_item": "v", "rows": rows})
    assert rep["imbalance"]["product"] == 0.0
    assert rep["imbalance"]["line"] == 0.0
    assert rep["confounded"] is False
    assert rep["confounders"] == []


def test_full_separation_by_line_is_flagged():
    # L1 は全部 treated、L2 は全部 control → ライン軸が完全分離（imbalance=1）。
    rows = []
    for i in range(6):
        rows.append(_row(str(i), "A", "L1", "2026-08-01", "treated", 1.0))
        rows.append(_row(str(i), "A", "L2", "2026-08-01", "control", 1.0))
    rep = detect_confounding({"result_item": "v", "rows": rows})
    assert rep["imbalance"]["line"] == 1.0
    assert "line" in rep["confounders"]
    assert rep["confounded"] is True
    assert rep["imbalance"]["line"] >= IMBALANCE_THRESHOLD


def test_period_confounding_detected():
    # 8月は treated 偏重、9月は control 偏重。
    rows = []
    for i in range(8):
        rows.append(_row(str(i), "A", "L1", "2026-08-15", "treated", 1.0))
    for i in range(8):
        rows.append(_row(str(i), "A", "L1", "2026-09-15", "control", 1.0))
    rep = detect_confounding({"result_item": "v", "rows": rows})
    assert rep["imbalance"]["period"] == 1.0
    assert "period" in rep["confounders"]


def test_stratified_pooled_delta_is_weighted_by_product():
    # 品名Aは delta=+1.0 (n各5), 品名Bは delta=+0.2 (n各5)。重みは等しいので pooled≈0.6。
    rows = []
    for i in range(5):
        rows.append(_row(str(i), "A", "L1", "2026-08-01", "treated", 2.0))
        rows.append(_row(str(i), "A", "L1", "2026-08-01", "control", 1.0))
        rows.append(_row(str(i), "B", "L1", "2026-08-01", "treated", 1.2))
        rows.append(_row(str(i), "B", "L1", "2026-08-01", "control", 1.0))
    est = estimate_stratified({"result_item": "v", "rows": rows, "min_lots_per_group": 5})
    assert est["usable_strata"] == 2
    assert est["n_total"] == 20
    assert abs(est["pooled_delta"] - 0.6) < 1e-6
    # 各層の delta が正しい
    by_product = {s["product"]: s for s in est["strata"]}
    assert abs(by_product["A"]["delta"] - 1.0) < 1e-6
    assert abs(by_product["B"]["delta"] - 0.2) < 1e-6


def test_stratify_marks_usable_only_when_both_groups_meet_min():
    # control が各群 min 未満 → usable=False。
    rows = []
    for i in range(5):
        rows.append(_row(str(i), "A", "L1", "2026-08-01", "treated", 2.0))
    for i in range(2):
        rows.append(_row(str(i), "A", "L1", "2026-08-01", "control", 1.0))
    est = estimate_stratified({"result_item": "v", "rows": rows, "min_lots_per_group": 5})
    assert est["usable_strata"] == 0
    assert est["strata"][0]["usable"] is False


def test_empty_rows_give_zero_and_not_confounded():
    rep = detect_confounding({"result_item": "v", "rows": []})
    assert rep["confounded"] is False
    est = estimate_stratified({"result_item": "v", "rows": []})
    assert est["pooled_delta"] == 0.0
    assert est["n_total"] == 0

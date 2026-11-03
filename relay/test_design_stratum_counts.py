# -*- coding: utf-8 -*-
"""Tests for relay/design_stratum_counts.py.

These pin three things the adapter must guarantee:
  1. column DETECTION -- stratifier columns are found by token, exposure/response/unit excluded;
  2. the RESPONSE COLUMN IS NEVER OPENED -- proven by poisoning the response cells with a value
     that would crash any reader that touched them, and by refusing it even if asked;
  3. the DB-join FALLBACK is called when the CSV carries no stratifier column, and its injected
     resolver output becomes the stratum counts.

Hermetic: only writes/reads a temp CSV; the DB resolver is an in-test stub.
"""
from __future__ import annotations

import csv
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from relay import design_stratum_counts as dsc  # noqa: E402


def _write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def _claim(exposure_col, unit_col, response_col):
    return {
        "exposure": {"column": exposure_col, "level": "between_lot",
                     "contrast": {"type": "levels", "treatment": "A", "reference": "B"}},
        "hierarchy": {"unit_column": unit_col},
        "response": {"column": response_col},
    }


# ---------------------------------------------------------------- column detection

def test_finds_product_and_line_excludes_exposure_response_unit():
    cols = ["ロット", "設備区分", "塗工布品名", "Line No", "不良量"]
    # exposure=設備区分 (matches '設備' token but is the exposure -> excluded),
    # response=不良量, unit=ロット
    found = dsc.find_stratifier_columns(
        cols, exclude=["設備区分", "不良量", "ロット"])
    assert "塗工布品名" in found
    assert "Line No" in found
    assert "設備区分" not in found  # exposure excluded even though it matches a token


def test_is_stratifier_name_token_substring():
    assert dsc.is_stratifier_name("塗工布品名")
    assert dsc.is_stratifier_name("Line No")
    assert not dsc.is_stratifier_name("不良量")
    assert not dsc.is_stratifier_name("")


def test_token_list_matches_confound_check_if_present():
    # The adapter and the screen must agree on what a design axis is. If tools.confound_check
    # is importable, the token sets must be identical.
    try:
        from tools import confound_check as cc
    except Exception:
        return  # tools package not on path in this env; nothing to pin
    assert set(dsc.STRATIFIER_TOKENS) == set(cc.STRATIFIER_TOKENS)


# ---------------------------------------------------------------- response never opened

def test_response_column_is_never_read(tmp_path):
    csv_path = str(tmp_path / "with_strat.csv")
    # Response cells are poisoned: a reader that parses them as numbers would blow up. The
    # adapter must never touch them, so the poison is harmless.
    _write_csv(
        csv_path,
        ["ロット", "設備区分", "塗工布品名", "不良量"],
        [
            ["L1", "A", "P1", "NOT_A_NUMBER_boom"],
            ["L2", "A", "P1", "NOT_A_NUMBER_boom"],
            ["L3", "B", "P1", "NOT_A_NUMBER_boom"],
            ["L4", "B", "P2", "NOT_A_NUMBER_boom"],
        ],
    )
    claim = _claim("設備区分", "ロット", "不良量")
    out = dsc.build_stratum_counts(claim, csv_path)
    assert out["response_column_read"] is False
    assert out["response_column"] == "不良量"
    assert out["source"] == "csv"
    # 塗工布品名 is a stratifier; P1 has A:{L1,L2}=2 lots, B:{L3}=1 lot
    counts = out["counts"]["塗工布品名"]
    assert counts["A"]["P1"] == 2
    assert counts["B"]["P1"] == 1
    assert counts["B"]["P2"] == 1


def test_swapping_response_column_does_not_change_the_design_counts(tmp_path):
    # Proof the verdict does not read the result: two CSVs identical except the response column
    # is replaced wholesale must yield identical stratum counts.
    header = ["ロット", "設備区分", "塗工布品名", "不良量"]
    rows_a = [["L1", "A", "P1", "1"], ["L2", "A", "P1", "2"], ["L3", "B", "P1", "3"]]
    rows_b = [["L1", "A", "P1", "999"], ["L2", "A", "P1", "-7"], ["L3", "B", "P1", "0"]]
    pa = str(tmp_path / "a.csv")
    pb = str(tmp_path / "b.csv")
    _write_csv(pa, header, rows_a)
    _write_csv(pb, header, rows_b)
    claim = _claim("設備区分", "ロット", "不良量")
    out_a = dsc.build_stratum_counts(claim, pa)
    out_b = dsc.build_stratum_counts(claim, pb)
    assert out_a["counts"] == out_b["counts"]


def test_read_selected_columns_refuses_forbidden():
    import pytest
    # Even if a caller mistakenly puts the response column in the want-list, the reader refuses.
    try:
        dsc.read_selected_columns("nonexistent.csv", ["不良量"], forbidden=["不良量"])
    except ValueError as e:
        assert "forbidden" in str(e) or "response" in str(e)
        return
    assert False, "expected ValueError for forbidden column"


# ---------------------------------------------------------------- distinct-lot counting

def test_lot_counted_once_per_stratum_despite_repeated_rows(tmp_path):
    csv_path = str(tmp_path / "dupe.csv")
    _write_csv(
        csv_path,
        ["ロット", "設備区分", "塗工布品名", "不良量"],
        [
            ["L1", "A", "P1", "1"],
            ["L1", "A", "P1", "1"],  # same lot repeated -> still one lot
            ["L2", "A", "P1", "1"],
        ],
    )
    claim = _claim("設備区分", "ロット", "不良量")
    out = dsc.build_stratum_counts(claim, csv_path)
    assert out["counts"]["塗工布品名"]["A"]["P1"] == 2


# ---------------------------------------------------------------- DB-join fallback

def test_db_join_called_when_no_stratifier_in_csv(tmp_path):
    csv_path = str(tmp_path / "no_strat.csv")
    # Only lot + exposure + response: NO stratifier column present.
    _write_csv(
        csv_path,
        ["ロット", "設備区分", "不良量"],
        [["L1", "A", "1"], ["L2", "A", "2"], ["L3", "B", "3"]],
    )
    claim = _claim("設備区分", "ロット", "不良量")

    calls = {"n": 0, "units": None}

    def resolver(units):
        calls["n"] += 1
        calls["units"] = list(units)
        # Attach a product name per lot from the 'DB'.
        return {"L1": {"塗工布品名": "P1"},
                "L2": {"塗工布品名": "P1"},
                "L3": {"塗工布品名": "P1"}}

    out = dsc.build_stratum_counts(claim, csv_path, db_resolver=resolver)
    assert out["db_join_called"] is True
    assert calls["n"] == 1
    assert out["source"] == "db_join"
    assert out["response_column_read"] is False
    assert "塗工布品名" in out["counts"]
    assert out["counts"]["塗工布品名"]["A"]["P1"] == 2
    assert out["counts"]["塗工布品名"]["B"]["P1"] == 1
    # The resolver was keyed by the unit values, not the response.
    assert calls["units"] == ["L1", "L2", "L3"]


def test_db_join_default_stub_returns_nothing(tmp_path):
    csv_path = str(tmp_path / "no_strat2.csv")
    _write_csv(
        csv_path,
        ["ロット", "設備区分", "不良量"],
        [["L1", "A", "1"], ["L2", "B", "2"]],
    )
    claim = _claim("設備区分", "ロット", "不良量")
    out = dsc.build_stratum_counts(claim, csv_path)  # no resolver -> stub
    assert out["db_join_called"] is True
    assert out["source"] == "none"
    assert out["counts"] == {}
    assert out["stratifier_columns"] == []


def test_join_product_from_db_stub_is_empty():
    assert dsc.join_product_from_db(["L1", "L2"]) == {}


# ---------------------------------------------------------------- shared-stratum summary

def test_shared_stratum_summary_counts_k_min():
    # A:P1=10,P2=3 ; B:P1=9,P2=20 -> shared {P1,P2}; meeting k_min(8) in BOTH: only P1
    gc = {"A": {"P1": 10, "P2": 3}, "B": {"P1": 9, "P2": 20}}
    s = dsc.shared_stratum_summary(gc, k_min=8)
    assert s["shared_strata"] == 2
    assert s["shared_strata_meeting_k_min"] == 1
    assert s["groups"] == ["A", "B"]


def test_shared_stratum_summary_no_shared():
    gc = {"A": {"P1": 10}, "B": {"P2": 10}}
    s = dsc.shared_stratum_summary(gc, k_min=8)
    assert s["shared_strata"] == 0
    assert s["shared_strata_meeting_k_min"] == 0

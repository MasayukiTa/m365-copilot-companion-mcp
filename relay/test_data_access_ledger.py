# -*- coding: utf-8 -*-
"""Contract tests for relay/data_access_ledger.py (the data-opening ledger).

These pin the properties the independence check downstream relies on: a recorded opening can be
read back with its fetch time and lot set; the fingerprint matches the stored lots; the append
is one line per opening and never rewrites earlier rows; and the file/dir are created on demand.
No Windows-only path assumptions -- everything goes through tmp_path so it runs on the Linux CI.
"""
from __future__ import annotations

import json
import os

try:
    from relay import data_access_ledger as dal
except Exception:  # pragma: no cover - allow flat import when run as a script
    import data_access_ledger as dal  # type: ignore


def _ledger(tmp_path):
    return str(tmp_path / "sub" / "data_access_ledger.jsonl")


def test_env_override_is_used_verbatim(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    monkeypatch.setenv(dal.LEDGER_PATH_ENV, p)
    assert dal.ledger_path() == p


def test_default_path_builds_from_home_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv(dal.LEDGER_PATH_ENV, raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    got = dal.ledger_path()
    # Ends with the fixed subparts and sits under the home we set; no hard-coded account name.
    assert got.endswith(os.path.join(*dal._LEDGER_SUBPARTS))
    assert got.startswith(str(tmp_path))


def test_init_creates_dir_and_empty_file_and_is_idempotent(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    monkeypatch.setenv(dal.LEDGER_PATH_ENV, p)
    dal.init_ledger()
    assert os.path.exists(p)
    assert os.path.getsize(p) == 0
    # Put a row in, then init again: must NOT truncate.
    dal.record_access("a1", ["L1"])
    size = os.path.getsize(p)
    assert size > 0
    dal.init_ledger()
    assert os.path.getsize(p) == size


def test_record_access_round_trips_time_and_lots(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    monkeypatch.setenv(dal.LEDGER_PATH_ENV, p)
    row = dal.record_access(
        "mcl_demo",
        ["B2", "A1", "A1", "C3"],
        data_location="/tmp/x.csv",
        data_ext=".csv",
        unit_column="lot",
        exposure_column="cond",
        response_column="rate",
        fetched_at="2026-11-03T07:00:00+09:00",
        input_columns=["lot", "cond", "rate"],
    )
    # Lots de-duplicated and sorted; n_lots and fingerprint derived from that set.
    assert row["lots"] == ["A1", "B2", "C3"]
    assert row["n_lots"] == 3
    assert row["lots_sha256"] == dal.lots_sha256(["A1", "B2", "C3"])
    assert row["fetched_at"] == "2026-11-03T07:00:00+09:00"
    back = dal.read_ledger()
    assert len(back) == 1
    assert back[0]["analysis_id"] == "mcl_demo"
    assert back[0]["lots"] == ["A1", "B2", "C3"]
    assert back[0]["fetched_at"] == "2026-11-03T07:00:00+09:00"


def test_each_record_is_one_line_appended_in_order(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    monkeypatch.setenv(dal.LEDGER_PATH_ENV, p)
    dal.record_access("a1", ["L1"])
    dal.record_access("a2", ["L2", "L3"])
    dal.record_access("a3", ["L4"])
    with open(p, "r", encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    assert len(lines) == 3
    ids = [json.loads(ln)["analysis_id"] for ln in lines]
    assert ids == ["a1", "a2", "a3"]


def test_fingerprint_matches_stored_lots_field(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    monkeypatch.setenv(dal.LEDGER_PATH_ENV, p)
    row = dal.record_access("a", ["Z9", "Y8", "Z9"])
    assert row["lots_sha256"] == dal.lots_sha256(row["lots"])


def test_fetched_at_defaults_to_an_iso_time_with_offset(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    monkeypatch.setenv(dal.LEDGER_PATH_ENV, p)
    row = dal.record_access("a", ["L1"])
    ft = row["fetched_at"]
    # ISO-8601 'YYYY-MM-DDTHH:MM:SS' plus a +HH:MM / -HH:MM offset: a row with no time cannot
    # be joined, so the default must carry one.
    assert "T" in ft
    assert ("+" in ft) or (ft.count("-") >= 3)


def test_read_ledger_absent_file_is_empty(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    monkeypatch.setenv(dal.LEDGER_PATH_ENV, p)
    assert dal.read_ledger() == []


def test_read_ledger_skips_a_garbled_last_line(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    monkeypatch.setenv(dal.LEDGER_PATH_ENV, p)
    dal.record_access("good1", ["L1"])
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"analysis_id": "truncated", "lots": ["L2"')  # no newline, broken JSON
    rows = dal.read_ledger()
    assert len(rows) == 1
    assert rows[0]["analysis_id"] == "good1"


def test_iter_openings_flattens_ok_rows_only(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    monkeypatch.setenv(dal.LEDGER_PATH_ENV, p)
    dal.record_access("a", ["L1", "L2"], fetched_at="2026-11-01T00:00:00+09:00")
    dal.record_access("b", [], status="no_unit_col")
    got = sorted(dal.iter_openings())
    assert got == [
        ("a", "2026-11-01T00:00:00+09:00", "L1"),
        ("a", "2026-11-01T00:00:00+09:00", "L2"),
    ]


def test_build_row_is_side_effect_free(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    monkeypatch.setenv(dal.LEDGER_PATH_ENV, p)
    row = dal.build_row("a", lots=["L1"])
    assert not os.path.exists(p)  # building a row must not create the file
    assert row["analysis_id"] == "a"
    assert set(dal.ROW_FIELDS) == set(row.keys())

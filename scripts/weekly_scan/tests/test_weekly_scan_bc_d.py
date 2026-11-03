"""Unit tests for the weekly-scan B/C-confirmation and D-re-acquisition drivers.

These tests inject fake lot counters, data fetchers and judges, so they touch
no DB and never run the real judge. They assert the driver's orchestration:
that the registered lock is reused (ordering never forged), that B/C rows below
the required lot count are not judged but stay scannable, that reaching the
count triggers a re-judge, that D rows are re-acquired, and that the ledger
round-trips without losing unknown fields.

No Windows-specific paths or assertions: everything uses tmp_path / pathlib.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.weekly_scan import bc_confirm, common, d_reacquire


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeCounter:
    def __init__(self, table):
        # table: {analysis_id: (k_now, k_required)}
        self.table = table

    def __call__(self, analysis_id, *, dir_rel):
        k_now, k_req = self.table.get(analysis_id, (0, 0))
        return common.LotCount(analysis_id=analysis_id, k_now=k_now, k_required=k_req)


class FakeFetcher:
    def __init__(self, table):
        # table: {analysis_id: path_or_None}
        self.table = table
        self.calls = []

    def __call__(self, analysis_id, *, dir_rel):
        self.calls.append(analysis_id)
        return self.table.get(analysis_id)


class FakeJudge:
    def __init__(self, table):
        # table: {analysis_id: JudgeOutcome}
        self.table = table
        self.seen_locks = {}

    def __call__(self, analysis_id, *, input_path, registered_lock_sha256, dir_rel):
        self.seen_locks[analysis_id] = registered_lock_sha256
        return self.table[analysis_id]


def _write_registered(docs_root: Path, dir_rel: str, *, k_req, lock):
    d = docs_root / dir_rel
    d.mkdir(parents=True, exist_ok=True)
    obj = {
        "verdict": {
            "verdict": "HOLD",
            "reasons": ["D_FAIL_NO_LOCK"],
            "hashes": {"lock_sha256": lock},
            "diagnostics": {"capacity": {"k_required_p80": k_req}},
        }
    }
    (d / "judge_result.json").write_text(json.dumps(obj), encoding="utf-8")


# --------------------------------------------------------------------------
# common.py
# --------------------------------------------------------------------------


def test_ledger_roundtrip_preserves_unknown_fields(tmp_path):
    rows = [{"analysis_id": "x", "class": "B", "dir": "a/b", "custom": 1}]
    p = tmp_path / "ledger.json"
    common.save_ledger(p, rows)
    back = common.load_ledger(p)
    assert back[0]["custom"] == 1
    assert back[0]["analysis_id"] == "x"


def test_update_row_appends_scan_notes_without_overwrite():
    row = {"analysis_id": "x", "class": "C", "last_scanned": "2026-11-04"}
    common.update_row(row, last_scanned="2026-11-11", next_trigger="n1", scan_note="first")
    common.update_row(row, last_scanned="2026-11-18", next_trigger="n2", scan_note="second")
    assert row["last_scanned"] == "2026-11-18"
    assert row["next_trigger"] == "n2"
    assert [n["note"] for n in row["scan_notes"]] == ["first", "second"]


def test_registered_readers_handle_nested_and_missing(tmp_path):
    _write_registered(tmp_path, "d1", k_req=112, lock="abc123")
    result = common.read_registered_result(tmp_path / "d1" / "judge_result.json")
    assert common.registered_k_required(result) == 112
    assert common.registered_lock_sha256(result) == "abc123"
    # Missing file -> None, never fabricated.
    assert common.read_registered_result(tmp_path / "nope" / "judge_result.json") is None


def test_outcome_from_result_parses_string_reasons():
    res = {"verdict": "HOLD", "reasons": "D_FAIL_NO_LOCK;D_FAIL_EXPLORATORY"}
    out = common.outcome_from_result(res)
    assert out.verdict == "HOLD"
    assert out.reasons == ("D_FAIL_NO_LOCK", "D_FAIL_EXPLORATORY")


def test_find_rows_selects_only_requested_classes():
    rows = [
        {"analysis_id": "a", "class": "A"},
        {"analysis_id": "b", "class": "B"},
        {"analysis_id": "c", "class": "C"},
        {"analysis_id": "d", "class": "D"},
    ]
    got = [r["analysis_id"] for r in common.find_rows(rows, ("B", "C"))]
    assert got == ["b", "c"]
    # Class A is never selected for B/C or D handling.
    assert "a" not in got


# --------------------------------------------------------------------------
# bc_confirm.py (processing a)
# --------------------------------------------------------------------------


def test_bc_below_requirement_is_not_judged_but_stays_scannable(tmp_path):
    _write_registered(tmp_path, "c1", k_req=112, lock="L1")
    row = {"analysis_id": "c1", "class": "C", "dir": "c1", "verdict": "HOLD"}
    counter = FakeCounter({"c1": (10, 112)})
    fetcher = FakeFetcher({})
    judge = FakeJudge({})
    res = bc_confirm.process_bc_row(
        row, docs_root=tmp_path, lot_counter=counter,
        data_fetcher=fetcher, judge_fn=judge, today="2026-11-11",
    )
    assert res.reached is False
    assert res.judged is False
    assert fetcher.calls == []  # no fetch when not reached
    assert row["last_scanned"] == "2026-11-11"
    assert "102" in row["next_trigger"]  # 112 - 10 shortfall shown


def test_bc_reached_triggers_rejudge_with_registered_lock(tmp_path):
    _write_registered(tmp_path, "c2", k_req=100, lock="REGLOCK")
    row = {"analysis_id": "c2", "class": "B", "dir": "c2", "verdict": "HOLD"}
    counter = FakeCounter({"c2": (120, 100)})
    fetcher = FakeFetcher({"c2": "/tmp/input_c2.json"})
    outcome = common.JudgeOutcome(verdict="GO", reasons=("A_OK", "B_OK"),
                                  order_ok=True, lock_sha256="REGLOCK")
    judge = FakeJudge({"c2": outcome})
    res = bc_confirm.process_bc_row(
        row, docs_root=tmp_path, lot_counter=counter,
        data_fetcher=fetcher, judge_fn=judge, today="2026-11-11",
    )
    assert res.reached is True
    assert res.judged is True
    assert res.verdict == "GO"
    # The REGISTERED lock was handed to the judge; ordering never forged.
    assert judge.seen_locks["c2"] == "REGLOCK"


def test_bc_reached_but_empty_fetch_records_acquisition_issue(tmp_path):
    _write_registered(tmp_path, "c3", k_req=100, lock="L")
    row = {"analysis_id": "c3", "class": "C", "dir": "c3", "verdict": "HOLD"}
    counter = FakeCounter({"c3": (150, 100)})
    fetcher = FakeFetcher({"c3": None})
    judge = FakeJudge({})
    res = bc_confirm.process_bc_row(
        row, docs_root=tmp_path, lot_counter=counter,
        data_fetcher=fetcher, judge_fn=judge, today="2026-11-11",
    )
    assert res.reached is True
    assert res.judged is False
    assert "再取得" in row["next_trigger"]


def test_bc_missing_registered_lock_still_judges_order_capped(tmp_path):
    # No lock registered -> driver passes None; judge caps order on its own.
    _write_registered(tmp_path, "c4", k_req=50, lock=None)
    row = {"analysis_id": "c4", "class": "B", "dir": "c4", "verdict": "HOLD"}
    counter = FakeCounter({"c4": (60, 50)})
    fetcher = FakeFetcher({"c4": "/tmp/in.json"})
    outcome = common.JudgeOutcome(verdict="HOLD", reasons=("D_FAIL_NO_LOCK",),
                                  order_ok=False, lock_sha256=None)
    judge = FakeJudge({"c4": outcome})
    res = bc_confirm.process_bc_row(
        row, docs_root=tmp_path, lot_counter=counter,
        data_fetcher=fetcher, judge_fn=judge, today="2026-11-11",
    )
    assert judge.seen_locks["c4"] is None
    assert res.verdict == "HOLD"


def test_run_bc_processes_only_b_and_c(tmp_path):
    for aid in ("b1", "cc", "aa", "dd"):
        _write_registered(tmp_path, aid, k_req=10, lock="L")
    rows = [
        {"analysis_id": "b1", "class": "B", "dir": "b1"},
        {"analysis_id": "cc", "class": "C", "dir": "cc"},
        {"analysis_id": "aa", "class": "A", "dir": "aa"},
        {"analysis_id": "dd", "class": "D", "dir": "dd"},
    ]
    counter = FakeCounter({"b1": (1, 10), "cc": (1, 10)})
    res = bc_confirm.run_bc(
        rows, docs_root=tmp_path, lot_counter=counter,
        data_fetcher=FakeFetcher({}), judge_fn=FakeJudge({}), today="2026-11-11",
    )
    assert {r.analysis_id for r in res} == {"b1", "cc"}
    # Class A row must be untouched (no last_scanned set here).
    assert "last_scanned" not in rows[2]


# --------------------------------------------------------------------------
# d_reacquire.py (processing b)
# --------------------------------------------------------------------------


def test_d_reacquire_empty_keeps_class_d(tmp_path):
    _write_registered(tmp_path, "d1", k_req=None, lock="L")
    row = {"analysis_id": "d1", "class": "D", "dir": "d1",
           "verdict": "ERROR", "reasons": "INPUT_NO_ROWS"}
    fetcher = FakeFetcher({"d1": None})
    res = d_reacquire.process_d_row(
        row, docs_root=tmp_path, data_fetcher=fetcher,
        judge_fn=FakeJudge({}), today="2026-11-11",
    )
    assert res.reacquired is False
    assert res.judged is False
    assert row["last_scanned"] == "2026-11-11"


def test_d_reacquire_success_rejudges_with_registered_lock(tmp_path):
    _write_registered(tmp_path, "d2", k_req=None, lock="DLOCK")
    row = {"analysis_id": "d2", "class": "D", "dir": "d2",
           "verdict": "ERROR", "reasons": "INPUT_NO_ROWS"}
    fetcher = FakeFetcher({"d2": "/tmp/d2.json"})
    outcome = common.JudgeOutcome(verdict="HOLD", reasons=("D_FAIL_NO_LOCK",),
                                  order_ok=False, lock_sha256="DLOCK")
    judge = FakeJudge({"d2": outcome})
    res = d_reacquire.process_d_row(
        row, docs_root=tmp_path, data_fetcher=fetcher,
        judge_fn=judge, today="2026-11-11",
    )
    assert res.reacquired is True
    assert res.judged is True
    assert judge.seen_locks["d2"] == "DLOCK"


def test_run_d_processes_only_d(tmp_path):
    for aid in ("d1", "b1"):
        _write_registered(tmp_path, aid, k_req=None, lock="L")
    rows = [
        {"analysis_id": "d1", "class": "D", "dir": "d1", "reasons": "INPUT_NO_ROWS"},
        {"analysis_id": "b1", "class": "B", "dir": "b1"},
    ]
    res = d_reacquire.run_d(
        rows, docs_root=tmp_path, data_fetcher=FakeFetcher({"d1": None}),
        judge_fn=FakeJudge({}), today="2026-11-11",
    )
    assert {r.analysis_id for r in res} == {"d1"}
    assert "last_scanned" not in rows[1]

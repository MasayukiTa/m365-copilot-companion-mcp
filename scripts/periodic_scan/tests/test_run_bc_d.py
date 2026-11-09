# -*- coding: utf-8 -*-
"""Unit tests for the periodic-scan run_bc_d driver (processing (a)+(b) over the
ledger). The judge, DB and fetcher are injected as in-memory fakes, so these
tests are hermetic. All fixtures are synthetic.

Invariants under test:
  * every input row is returned in order; only B/C and D rows are updated;
  * a row's last_scanned / next_trigger / verdict come from its processor's
    outcome, keyed by (analysis_id, dir) so duplicate ids do not collide;
  * the registered trust clock is reused verbatim (checked via the judge the
    runner drove);
  * load_ledger / save_ledger round-trip the JSON array shape.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SCRIPTS = _HERE.parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from periodic_scan import run_bc_d  # noqa: E402


class FakeLotCounter:
    def __init__(self, n):
        self.n = n

    def count_new_lots(self, request):
        return self.n


class FakeFetcher:
    def __init__(self, path="/tmp/confirm.csv"):
        self.path = path

    def fetch(self, request):
        return self.path


class FakeJudge:
    def __init__(self, verdict="HOLD", reasons=None):
        self._verdict = verdict
        self._reasons = reasons or []
        self.seen = []

    def run(self, request):
        self.seen.append(request)
        return {"verdict": {"verdict": self._verdict, "reasons": self._reasons}}


def _request(registered_at="2026-10-01T00:00:00+00:00"):
    return {
        "claim_id": "A0001",
        "data_location": "/orig/data.csv",
        "registration": {"registered_at": registered_at,
                         "claim": {"delta_min": {"value": 0.3}}},
    }


def _loader(request):
    return lambda row: request


def test_run_over_ledger_updates_only_bcd_rows_and_passes_through_others():
    rows = [
        {"analysis_id": "a", "dir": "1", "class": "A", "next_trigger": "keep"},
        {"analysis_id": "c", "dir": "2", "class": "C", "kshort": 2, "add80": 0,
         "next_trigger": "old"},
        {"analysis_id": "d", "dir": "3", "class": "D", "next_trigger": "old"},
    ]
    judge = FakeJudge("HOLD", ["D_FAIL_NO_LOCK"])
    updated, outcomes = run_bc_d.run_over_ledger(
        rows, request_loader=_loader(_request()), lot_counter=FakeLotCounter(2),
        data_fetcher=FakeFetcher(), judge=judge, scanned_on="2026-11-04",
    )
    assert len(updated) == 3
    assert updated[0]["next_trigger"] == "keep"
    assert "last_scanned" not in updated[0]
    assert updated[1]["last_scanned"] == "2026-11-04"
    assert updated[1]["verdict"] == "HOLD"
    assert updated[2]["last_scanned"] == "2026-11-04"
    assert {o.key for o in outcomes} == {("c", "2"), ("d", "3")}


def test_run_over_ledger_reuses_registered_clock():
    rows = [{"analysis_id": "c", "dir": "2", "class": "B", "kshort": 0, "add80": 0}]
    judge = FakeJudge("GO", ["B_OK"])
    run_bc_d.run_over_ledger(
        rows, request_loader=_loader(_request("2026-09-01T00:00:00+00:00")),
        lot_counter=FakeLotCounter(0), data_fetcher=FakeFetcher("/new/c.csv"),
        judge=judge, scanned_on="2026-11-04",
    )
    seen = judge.seen[0]
    assert seen["registration"]["registered_at"] == "2026-09-01T00:00:00+00:00"
    assert seen["data_location"] == "/new/c.csv"


def test_run_over_ledger_keys_by_analysis_id_and_dir():
    rows = [
        {"analysis_id": "dup", "dir": "X", "class": "C", "kshort": 1, "add80": 0},
        {"analysis_id": "dup", "dir": "Y", "class": "D"},
    ]
    updated, outcomes = run_bc_d.run_over_ledger(
        rows, request_loader=_loader(_request()), lot_counter=FakeLotCounter(5),
        data_fetcher=FakeFetcher(), judge=FakeJudge("HOLD"), scanned_on="2026-11-04",
    )
    assert updated[0]["dir"] == "X" and updated[0]["last_scanned"] == "2026-11-04"
    assert updated[1]["dir"] == "Y" and updated[1]["last_scanned"] == "2026-11-04"
    assert {o.key for o in outcomes} == {("dup", "X"), ("dup", "Y")}


def test_build_run_binds_ports():
    rows = [{"analysis_id": "c", "dir": "2", "class": "C", "kshort": 1, "add80": 0}]
    run = run_bc_d.build_run(
        request_loader=_loader(_request()), lot_counter=FakeLotCounter(1),
        data_fetcher=FakeFetcher(), judge=FakeJudge("HOLD"),
    )
    updated, outcomes = run(rows, "2026-11-04")
    assert len(outcomes) == 1
    assert updated[0]["last_scanned"] == "2026-11-04"


def test_format_run_log_lists_each_outcome():
    rows = [{"analysis_id": "c", "dir": "2", "class": "C", "kshort": 1, "add80": 0}]
    _, outcomes = run_bc_d.run_over_ledger(
        rows, request_loader=_loader(_request()), lot_counter=FakeLotCounter(1),
        data_fetcher=FakeFetcher(), judge=FakeJudge("HOLD"), scanned_on="2026-11-04",
    )
    log = run_bc_d.format_run_log(outcomes, "2026-11-04")
    assert "2026-11-04" in log
    assert "c" in log


def test_format_run_log_empty():
    log = run_bc_d.format_run_log([], "2026-11-04")
    assert "no B/C/D rows" in log


def test_load_and_save_ledger_round_trip(tmp_path):
    p = tmp_path / "ledger.json"
    original = [{"analysis_id": "a", "dir": "1", "class": "C", "kshort": 2}]
    p.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    rows = run_bc_d.load_ledger(str(p))
    assert rows == original
    rows[0]["last_scanned"] = "2026-11-04"
    run_bc_d.save_ledger(str(p), rows)
    reloaded = json.loads(p.read_text(encoding="utf-8"))
    assert reloaded[0]["last_scanned"] == "2026-11-04"


def test_load_ledger_accepts_rows_wrapper(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"rows": [{"analysis_id": "a", "dir": "1", "class": "A"}]}),
                 encoding="utf-8")
    rows = run_bc_d.load_ledger(str(p))
    assert rows == [{"analysis_id": "a", "dir": "1", "class": "A"}]

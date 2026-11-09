# -*- coding: utf-8 -*-
"""Unit tests for periodic-scan processing (a) B/C confirmation and
(b) D re-acquisition. The judge, the DB and the fetcher are injected as
in-memory fakes, so these tests are hermetic (no DB, no service, no Windows-only
calls). All fixtures are synthetic.

Invariants under test:
  * the registered trust clock is REUSED verbatim and NEVER minted/forged;
  * a B/C row is confirmed only when new lots have reached the required number;
  * D re-acquisition repairs the INPUT and re-judges on the recorded clock;
  * the ledger row is keyed by (analysis_id, dir), so duplicate ids do not collide;
  * no processor mutates delta_min / criteria.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SCRIPTS = _HERE.parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from periodic_scan import bc_confirm, common, d_reacquire  # noqa: E402


class FakeLotCounter:
    def __init__(self, n):
        self.n = n
        self.seen = []

    def count_new_lots(self, request):
        self.seen.append(request)
        return self.n


class FakeFetcher:
    def __init__(self, path="/tmp/confirm.csv"):
        self.path = path
        self.seen = []

    def fetch(self, request):
        self.seen.append(request)
        return self.path


class FakeJudge:
    def __init__(self, verdict="HOLD", reasons=None):
        self._verdict = verdict
        self._reasons = reasons or []
        self.seen = []

    def run(self, request):
        self.seen.append(request)
        return {"verdict": {"verdict": self._verdict, "reasons": self._reasons}}


def _request(registered_at="2026-10-01T00:00:00+00:00", dmin=0.3):
    return {
        "claim_id": "A0001",
        "data_location": "/orig/data.csv",
        "registration": {
            "registered_at": registered_at,
            "claim": {"delta_min": {"value": dmin, "scale": "standardized"}},
        },
    }


def _loader(request):
    return lambda row: request


# --- common: requirement arithmetic ----------------------------------------


def test_lots_still_required_takes_max_of_kshort_and_add80():
    assert common.lots_still_required({"kshort": 2, "add80": 5}) == 5
    assert common.lots_still_required({"kshort": 8, "add80": 0}) == 8
    assert common.lots_still_required({"kshort": 0, "add80": 0}) == 0


def test_lots_still_required_none_when_both_unknown():
    assert common.lots_still_required({"kshort": None, "add80": None}) is None
    assert common.lots_still_required({}) is None


def test_is_ready_requires_meeting_a_positive_need():
    assert common.is_ready_for_confirmation({"kshort": 2, "add80": 2}, 2) is True
    assert common.is_ready_for_confirmation({"kshort": 2, "add80": 2}, 1) is False
    assert common.is_ready_for_confirmation({}, 9999) is False


# --- common: the trust clock is reused, never forged -----------------------


def test_registered_at_read_from_both_places():
    assert common.registered_at_of(_request("2026-10-01T00:00:00+00:00")) == (
        "2026-10-01T00:00:00+00:00"
    )
    assert common.registered_at_of({"order": {"registered_at": "X"}}) == "X"
    assert common.registered_at_of({}) is None


def test_with_registered_clock_reuses_verbatim_and_only_moves_data():
    req = _request("2026-10-01T00:00:00+00:00")
    conf = common.with_registered_clock(req, "/new/confirm.csv")
    assert conf["data_location"] == "/new/confirm.csv"
    assert conf["registration"]["registered_at"] == "2026-10-01T00:00:00+00:00"
    assert conf["reused_registered_at"] == "2026-10-01T00:00:00+00:00"
    assert conf["registration"]["claim"]["delta_min"]["value"] == 0.3


def test_with_registered_clock_refuses_to_mint_a_clock():
    with pytest.raises(ValueError):
        common.with_registered_clock({"data_location": "/x"}, "/new.csv")


def test_ledger_key_distinguishes_duplicate_ids_across_dirs():
    a = {"analysis_id": "A0679", "dir": "AAA/679"}
    b = {"analysis_id": "A0679", "dir": "ERR/A0679"}
    assert common.ledger_key(a) != common.ledger_key(b)


# --- bc_confirm: not ready --------------------------------------------------


def test_bc_not_ready_when_new_lots_below_need():
    row = {"analysis_id": "A0001", "dir": "d", "class": "C", "kshort": 8, "add80": 8}
    judge = FakeJudge("GO")
    out = bc_confirm.confirm_one(
        row, request_loader=_loader(_request()), lot_counter=FakeLotCounter(3),
        data_fetcher=FakeFetcher(), judge=judge, scanned_on="2026-11-04",
    )
    assert out.action == "bc_not_ready"
    assert out.verdict is None
    assert judge.seen == []          # judge NOT run when not ready
    assert "5 to go" in out.next_trigger


def test_bc_confirms_and_reuses_clock_when_ready():
    row = {"analysis_id": "A0001", "dir": "d", "class": "B", "kshort": 0, "add80": 0}
    judge = FakeJudge("GO", ["A_OK", "D_OK", "C_OK", "B_OK"])
    fetcher = FakeFetcher("/new/confirm.csv")
    out = bc_confirm.confirm_one(
        row, request_loader=_loader(_request("2026-09-01T00:00:00+00:00")),
        lot_counter=FakeLotCounter(0), data_fetcher=fetcher, judge=judge,
        scanned_on="2026-11-04",
    )
    assert out.action == "bc_confirmed_go"
    assert out.verdict == "GO"
    seen = judge.seen[0]
    assert seen["registration"]["registered_at"] == "2026-09-01T00:00:00+00:00"
    assert seen["data_location"] == "/new/confirm.csv"


def test_bc_confirmed_hold_keeps_rescan_trigger():
    row = {"analysis_id": "A0001", "dir": "d", "class": "C", "kshort": 2, "add80": 0}
    judge = FakeJudge("HOLD", ["D_FAIL_NO_LOCK"])
    out = bc_confirm.confirm_one(
        row, request_loader=_loader(_request()), lot_counter=FakeLotCounter(2),
        data_fetcher=FakeFetcher(), judge=judge, scanned_on="2026-11-04",
    )
    assert out.action == "bc_confirmed_hold"
    assert out.verdict == "HOLD"
    assert "re-confirm" in out.next_trigger


def test_bc_no_request_is_not_fabricated():
    row = {"analysis_id": "x", "dir": "d", "class": "B", "kshort": 0, "add80": 0}
    judge = FakeJudge("GO")
    out = bc_confirm.confirm_one(
        row, request_loader=lambda r: None, lot_counter=FakeLotCounter(99),
        data_fetcher=FakeFetcher(), judge=judge, scanned_on="2026-11-04",
    )
    assert out.action == "bc_no_request"
    assert judge.seen == []


def test_bc_rows_skips_non_bc():
    rows = [
        {"analysis_id": "a", "dir": "1", "class": "A"},
        {"analysis_id": "b", "dir": "2", "class": "D"},
        {"analysis_id": "c", "dir": "3", "class": "C", "kshort": 1, "add80": 0},
    ]
    outs = bc_confirm.confirm_rows(
        rows, request_loader=_loader(_request()), lot_counter=FakeLotCounter(0),
        data_fetcher=FakeFetcher(), judge=FakeJudge("HOLD"), scanned_on="2026-11-04",
    )
    assert len(outs) == 1
    assert outs[0].key == ("c", "3")


# --- d_reacquire ------------------------------------------------------------


def test_d_reacquire_repairs_input_and_rejudges_on_recorded_clock():
    row = {"analysis_id": "A0100", "dir": "d", "class": "D"}
    judge = FakeJudge("HOLD", ["D_FAIL_NO_LOCK"])
    fetcher = FakeFetcher("/repaired/data.csv")
    out = d_reacquire.reacquire_one(
        row, request_loader=_loader(_request("2026-08-01T00:00:00+00:00")),
        data_fetcher=fetcher, judge=judge, scanned_on="2026-11-04",
    )
    assert out.action == "d_redone_rejudged"
    seen = judge.seen[0]
    assert seen["data_location"] == "/repaired/data.csv"
    assert seen["registration"]["registered_at"] == "2026-08-01T00:00:00+00:00"


def test_d_reacquire_without_clock_does_not_invent_one():
    row = {"analysis_id": "A0100", "dir": "d", "class": "D"}
    judge = FakeJudge("ERROR", ["INPUT_NO_ROWS"])
    req = {"claim_id": "A0100", "data_location": "/orig.csv"}  # no registered_at
    out = d_reacquire.reacquire_one(
        row, request_loader=lambda r: req, data_fetcher=FakeFetcher("/re.csv"),
        judge=judge, scanned_on="2026-11-04",
    )
    assert out.action == "d_redone_error"
    seen = judge.seen[0]
    assert "registration" not in seen or not seen.get("registration")
    assert seen["data_location"] == "/re.csv"


def test_d_reacquire_go_sets_follow_trigger():
    row = {"analysis_id": "A0100", "dir": "d", "class": "D"}
    judge = FakeJudge("GO", ["A_OK", "D_OK", "C_OK", "B_OK"])
    out = d_reacquire.reacquire_one(
        row, request_loader=_loader(_request()), data_fetcher=FakeFetcher(),
        judge=judge, scanned_on="2026-11-04",
    )
    assert out.action == "d_redone_go"
    assert out.verdict == "GO"


def test_d_rows_skips_non_d():
    rows = [
        {"analysis_id": "a", "dir": "1", "class": "B"},
        {"analysis_id": "b", "dir": "2", "class": "D"},
    ]
    outs = d_reacquire.reacquire_rows(
        rows, request_loader=_loader(_request()), data_fetcher=FakeFetcher(),
        judge=FakeJudge("ERROR"), scanned_on="2026-11-04",
    )
    assert len(outs) == 1
    assert outs[0].key == ("b", "2")


# --- apply_outcome ----------------------------------------------------------


def test_apply_outcome_updates_only_scan_fields_and_verdict():
    row = {"analysis_id": "a", "dir": "d", "class": "C", "dmin": 0.3,
           "last_scanned": "2026-10-01", "next_trigger": "old"}
    out = common.ScanOutcome(
        key=("a", "d"), action="bc_confirmed_hold", scanned_on="2026-11-04",
        next_trigger="new trigger", verdict="HOLD", reasons=["D_FAIL_NO_LOCK"],
    )
    updated = common.apply_outcome(row, out)
    assert updated["last_scanned"] == "2026-11-04"
    assert updated["next_trigger"] == "new trigger"
    assert updated["verdict"] == "HOLD"
    assert updated["reasons"] == "D_FAIL_NO_LOCK"
    assert updated["dmin"] == 0.3
    assert "2026-11-04 bc_confirmed_hold" in updated["scan_log"]

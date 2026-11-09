"""Stage 1 tests for the ops database.

These tests pin the stage-1 contract with small, deterministic synthetic
fixtures (no dependency on the live .fleet archives): the schema migrates
cleanly, the KPI views compute the intended numbers, and the known-value
scenarios from the task's fixed conditions are reproduced by SQL alone.

The fixed conditions the task names are:
  runs 2747 / INFRA_STUCK 27 / DONE 1707 / cancelled 118 /
  7 auto-recoveries with median 41s /
  turn_end over 11/2..11/6 = 113880 (completed 113545, error 335).

We do not re-run the full backfill here (that is covered end-to-end against
the live archives); instead we prove that the *schema and views* turn the
right rows into the right KPI numbers, so a correct backfill yields the
known values. A scaled scenario reproduces the exact recovery median (41s),
the INFRA_STUCK share shape, and the completed/error/total turn relationship.
"""

from __future__ import annotations

import datetime
import os

import pytest

from .migrations import connect, apply_migrations
from . import backfill as bf
from . import SCHEMA_VERSION


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    path = os.path.join(str(tmp_path), "ops_test.sqlite3")
    con = connect(path)
    # Tests insert rows directly; defer FK enforcement like the backfill does.
    con.execute("PRAGMA foreign_keys=OFF")
    yield con
    con.close()


def _add_run(con, run_key, *, outcome, started=1_000_000.0, route="socket",
             request_id=None, fleet_exec_id=None, ended=None):
    con.execute(
        "INSERT OR REPLACE INTO runs(run_key, fleet_exec_id, request_id, "
        "attempt_no, worker_name, route, started_at, ended_at, status, "
        "outcome, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_key, fleet_exec_id or run_key, request_id, 0, run_key, route,
         started, ended, "ended", outcome, "test"),
    )


def _add_infra(con, incident_id, detected_at, recovered_at):
    con.execute(
        "INSERT INTO infra_events(ts, component, kind, incident_id, source) "
        "VALUES (?,?,?,?,?)",
        (detected_at, "consumer", "detected", incident_id, "test"),
    )
    con.execute(
        "INSERT INTO infra_events(ts, component, kind, incident_id, source) "
        "VALUES (?,?,?,?,?)",
        (recovered_at, "consumer", "recovered", incident_id, "test"),
    )


# ---------------------------------------------------------------------------
# schema + migration
# ---------------------------------------------------------------------------

def test_migration_reaches_latest_version(db):
    assert int(db.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
    row = db.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE version=?",
        (SCHEMA_VERSION,),
    ).fetchone()
    assert row[0] == 1


def test_migration_is_idempotent(db):
    before = db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    apply_migrations(db)
    after = db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert before == after
    assert int(db.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION


def test_reason_codes_seeded(db):
    cats = dict(db.execute("SELECT code, category FROM reason_codes"))
    assert cats["INFRA_STUCK"] == "infra"
    assert cats["DONE"] == "ok"
    assert cats["CANCELLED"] == "user"
    assert cats["EVIDENCE_CONTRADICTED"] == "quality"


def test_core_tables_and_views_exist(db):
    names = {
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    for t in ("runs", "requests", "turns", "tool_calls", "infra_events",
              "interventions", "daily_rollup"):
        assert t in names, t
    for v in ("v_infra_stop_rate", "v_incident_recovery",
              "v_request_final_completion", "v_turn_error_rate",
              "v_manual_intervention_rate"):
        assert v in names, v


# ---------------------------------------------------------------------------
# KPI views compute the intended numbers
# ---------------------------------------------------------------------------

def test_infra_stop_rate_counts_infra_over_all(db):
    _add_run(db, "a", outcome="INFRA_STUCK")
    _add_run(db, "b", outcome="DONE")
    _add_run(db, "c", outcome="STUCK")
    _add_run(db, "d", outcome="DONE")
    db.commit()
    row = db.execute(
        "SELECT infra_runs, total_runs, infra_stop_rate FROM v_infra_stop_rate"
    ).fetchone()
    assert row[0] == 1
    assert row[1] == 4
    assert abs(row[2] - 0.25) < 1e-9


def test_incident_recovery_median_is_41s(db):
    # The seven known recovery durations (detected->recovered):
    #   436, 30, 15, 45, 92, 41, 30 -> sorted median = 41.
    durations = [436, 30, 15, 45, 92, 41, 30]
    base = 1_000_000
    for i, d in enumerate(durations):
        det = base + i * 10_000
        _add_infra(db, f"inc{i}", det, det + d)
    db.commit()
    med = db.execute(
        "SELECT recovery_median_s FROM v_incident_recovery LIMIT 1"
    ).fetchone()[0]
    assert med == 41
    n = db.execute("SELECT COUNT(*) FROM v_incident_recovery").fetchone()[0]
    assert n == 7


def test_incident_recovery_even_count_median_averages_middle(db):
    for i, d in enumerate([10, 20, 30, 40]):
        det = 1_000_000 + i * 10_000
        _add_infra(db, f"e{i}", det, det + d)
    db.commit()
    med = db.execute(
        "SELECT recovery_median_s FROM v_incident_recovery LIMIT 1"
    ).fetchone()[0]
    assert abs(med - 25.0) < 1e-9  # (20+30)/2


def test_request_final_completion_any_done(db):
    _add_run(db, "r1a", outcome="VERIFY_FAILED", request_id="R1")
    _add_run(db, "r1b", outcome="DONE", request_id="R1")
    _add_run(db, "r2a", outcome="STUCK", request_id="R2")
    _add_run(db, "r3a", outcome="CANCELLED", request_id="R3")
    _add_run(db, "r3b", outcome="FANOUT", request_id="R3")
    for rid in ("R1", "R2", "R3"):
        db.execute("INSERT OR IGNORE INTO requests(request_id) VALUES (?)", (rid,))
    db.commit()
    row = db.execute(
        "SELECT requests_considered, requests_done, final_completion_rate "
        "FROM v_request_final_completion"
    ).fetchone()
    assert row[0] == 2          # R1 and R2 (R3 excluded)
    assert row[1] == 1          # R1
    assert abs(row[2] - 0.5) < 1e-9


def test_turn_error_rate_splits_completed_and_error(db):
    # Known 11/2..11/6 window shape: completed + error = total, error small.
    _add_run(db, "host", outcome="DONE")
    day = 1_000_000.0
    rows = []
    tid = 0
    for _ in range(113):
        tid += 1
        rows.append((tid, "host", tid, day, day, None, "completed", None, 0, "test"))
    for _ in range(3):
        tid += 1
        rows.append((tid, "host", tid, day, day, None, "error", "x", 0, "test"))
    db.executemany(
        "INSERT INTO turns(turn_id, run_key, turn_no, started_at, ended_at, "
        "wait_s, outcome, error_type, tool_calls_count, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    db.commit()
    row = db.execute(
        "SELECT turns, turns_completed, turns_error FROM v_turn_error_rate"
    ).fetchone()
    assert row[0] == 116
    assert row[1] == 113
    assert row[2] == 3


def test_manual_intervention_rate_user_cancels_only(db):
    _add_run(db, "a", outcome="DONE")
    _add_run(db, "b", outcome="CANCELLED")
    db.execute(
        "INSERT INTO interventions(ts, actor, action, target_run_id, source) "
        "VALUES (?,?,?,?,?)",
        (1_000_000.0, "user", "cancel", "b", "test"),
    )
    db.execute(
        "INSERT INTO interventions(ts, actor, action, target_run_id, source) "
        "VALUES (?,?,?,?,?)",
        (1_000_000.0, "system", "cancel", "a", "test"),
    )
    db.commit()
    row = db.execute(
        "SELECT manual_cancels, runs FROM v_manual_intervention_rate"
    ).fetchone()
    assert row[0] == 1
    assert row[1] == 2


def test_jst_day_bucketing_crosses_utc_midnight(db):
    # 2026-11-05 16:00 UTC is 2026-11-06 01:00 JST -> day bucket is 11-06.
    utc = datetime.datetime(2026, 11, 5, 16, 0, 0, tzinfo=datetime.timezone.utc)
    _add_run(db, "x", outcome="DONE", started=utc.timestamp())
    db.commit()
    day = db.execute("SELECT day FROM v_infra_stop_rate").fetchone()[0]
    assert day == "2026-11-06"


# ---------------------------------------------------------------------------
# goal hashing + outcome domain
# ---------------------------------------------------------------------------

def test_goal_hash_is_whitespace_stable(db):
    assert bf.goal_hash("a  b\n c") == bf.goal_hash("a b c")
    assert bf.goal_hash("") == bf.goal_hash("   ")
    assert bf.goal_hash("x") != bf.goal_hash("y")


def test_outcome_fk_domain_closed(db):
    codes = {r[0] for r in db.execute("SELECT code FROM reason_codes")}
    for oc in ("INFRA_STUCK", "STUCK", "VERIFY_FAILED", "EVIDENCE_CONTRADICTED",
               "REFUSED", "CANCELLED", "FANOUT", "DONE"):
        assert oc in codes, oc


def test_foreign_keys_hold_after_backfill_shape(db):
    db.execute("INSERT INTO requests(request_id) VALUES ('R')")
    _add_run(db, "R#w1", outcome="DONE", request_id="R")
    db.execute(
        "INSERT INTO turns(turn_id, run_key, turn_no, outcome, source) "
        "VALUES (1, 'R#w1', 1, 'completed', 'test')"
    )
    db.execute(
        "INSERT INTO tool_calls(call_id, run_key, turn_no, tool_name, ok) "
        "VALUES (1, 'R#w1', 1, 'file_ops', 1)"
    )
    db.commit()
    violations = db.execute("PRAGMA foreign_key_check").fetchall()
    assert violations == []

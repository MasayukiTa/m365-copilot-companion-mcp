"""DDL for the ops database (stage 1).

The schema is versioned with ``PRAGMA user_version`` and a
``schema_migrations`` table (see migrations.py). All timestamps are UNIX
seconds (UTC) stored as REAL; JST display is provided by views, not by
storing a second copy.

The statements here are idempotent (``IF NOT EXISTS``) so that opening an
already-initialised database is cheap, but creation of a *new* database and
upgrading an *existing* one both go through migrations.py, which is the one
place that bumps ``user_version``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Base tables (schema version 1)
# ---------------------------------------------------------------------------

TABLES_SQL = r"""
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT,
    applied_at REAL
);

CREATE TABLE IF NOT EXISTS reason_codes (
    code        TEXT PRIMARY KEY,
    category    TEXT,
    description TEXT
);

CREATE TABLE IF NOT EXISTS requests (
    request_id    TEXT PRIMARY KEY,
    goal_hash     TEXT,
    goal_summary  TEXT,
    submitter     TEXT,
    submitted_at  REAL,
    attempts      INTEGER,
    final_outcome TEXT,
    final_run_id  TEXT,
    completed_at  REAL
);

-- A run is one worker in one fleet execution: PK = run_started#name.
-- fleet_exec_id is the shared rXXXX_aN identifier (many runs share it).
CREATE TABLE IF NOT EXISTS runs (
    run_key         TEXT PRIMARY KEY,
    fleet_exec_id   TEXT,
    request_id      TEXT REFERENCES requests(request_id),
    attempt_no      INTEGER,
    worker_name     TEXT,
    campaign_id     TEXT,
    route           TEXT,
    transport       TEXT,
    started_at      REAL,
    ended_at        REAL,
    status          TEXT,
    outcome         TEXT REFERENCES reason_codes(code),
    reason_text     TEXT,
    verified        INTEGER,
    verify_attempts INTEGER,
    refuter_verdict TEXT,
    cancelled_by    TEXT,
    turns_count     INTEGER,
    conv_guid       TEXT,
    source          TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    turn_id          INTEGER PRIMARY KEY,
    run_key          TEXT REFERENCES runs(run_key),
    turn_no          INTEGER,
    started_at       REAL,
    ended_at         REAL,
    wait_s           REAL,
    outcome          TEXT,
    error_type       TEXT,
    tool_calls_count INTEGER,
    source           TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id      INTEGER PRIMARY KEY,
    run_key         TEXT REFERENCES runs(run_key),
    turn_no         INTEGER,
    role            TEXT,
    ts              REAL,
    chars           INTEGER,
    sha256          TEXT,
    body            TEXT,
    body_expires_at REAL
);

CREATE TABLE IF NOT EXISTS tool_calls (
    call_id    INTEGER PRIMARY KEY,
    run_key    TEXT,
    turn_no    INTEGER,
    tool_name  TEXT,
    ok         INTEGER,
    error_type TEXT,
    duration_s REAL,
    ts         REAL,
    args_hash  TEXT
);

CREATE TABLE IF NOT EXISTS infra_events (
    event_id    INTEGER PRIMARY KEY,
    ts          REAL,
    component   TEXT,
    kind        TEXT,
    incident_id TEXT,
    detail      TEXT,
    source      TEXT
);

CREATE TABLE IF NOT EXISTS heartbeats (
    ts         REAL,
    component  TEXT,
    up         INTEGER,
    latency_ms REAL,
    PRIMARY KEY (ts, component)
);

CREATE TABLE IF NOT EXISTS interventions (
    intervention_id INTEGER PRIMARY KEY,
    ts              REAL,
    actor           TEXT,
    action          TEXT,
    target_run_id   TEXT,
    reason          TEXT,
    source          TEXT
);

CREATE TABLE IF NOT EXISTS daily_rollup (
    day             TEXT,
    route           TEXT,
    runs            INTEGER,
    runs_done       INTEGER,
    runs_infra      INTEGER,
    runs_transient  INTEGER,
    runs_quality    INTEGER,
    runs_refusal    INTEGER,
    runs_cancelled  INTEGER,
    requests        INTEGER,
    requests_done   INTEGER,
    turns           INTEGER,
    turns_error     INTEGER,
    wait_p50_s      REAL,
    wait_p95_s      REAL,
    incidents       INTEGER,
    mttr_median_s   REAL,
    PRIMARY KEY (day, route)
);
"""

# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

INDEXES_SQL = r"""
CREATE INDEX IF NOT EXISTS idx_runs_started      ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_request      ON runs(request_id);
CREATE INDEX IF NOT EXISTS idx_runs_exec         ON runs(fleet_exec_id);
CREATE INDEX IF NOT EXISTS idx_runs_outcome_time ON runs(outcome, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_route_time   ON runs(route, started_at);
CREATE INDEX IF NOT EXISTS idx_turns_run_no      ON turns(run_key, turn_no);
CREATE INDEX IF NOT EXISTS idx_turns_started     ON turns(started_at);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name   ON tool_calls(tool_name, ts);
CREATE INDEX IF NOT EXISTS idx_infra_ts          ON infra_events(ts);
CREATE INDEX IF NOT EXISTS idx_infra_incident    ON infra_events(incident_id);
CREATE INDEX IF NOT EXISTS idx_messages_run_no   ON messages(run_key, turn_no);
"""

# ---------------------------------------------------------------------------
# Full text search (FTS5). Contentless-external tables keyed by rowid.
# ---------------------------------------------------------------------------

FTS_SQL = r"""
CREATE VIRTUAL TABLE IF NOT EXISTS requests_fts USING fts5(
    goal_summary,
    content=''
);

CREATE VIRTUAL TABLE IF NOT EXISTS runs_fts USING fts5(
    reason_text,
    content=''
);
"""

# Seed rows for reason_codes. CANCELLED maps to the user category by default;
# backfill overrides the row category to 'system' per run when the cancel was
# automated (handled in runs.outcome + the KPI views, not by mutating this
# dimension table).
REASON_CODES = [
    ("INFRA_STUCK", "infra", "connection or ownership never established; run stopped"),
    ("STUCK", "transient", "run stalled and did not complete"),
    ("VERIFY_FAILED", "quality", "verification did not pass"),
    ("EVIDENCE_CONTRADICTED", "quality", "refuter contradicted the claimed evidence"),
    ("REFUSED", "refusal", "the request was refused"),
    ("CANCELLED", "user", "cancelled (actor=user; system when automated)"),
    ("FANOUT", "system", "split parent; work handed to children"),
    ("DONE", "ok", "completed"),
]

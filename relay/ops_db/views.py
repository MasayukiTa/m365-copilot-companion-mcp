"""KPI views for the ops database (stage 1).

Each view is plain SQL over the base tables so that a KPI can be produced
with a single query. Timestamps are UNIX seconds (UTC); the JST day is
derived as ``date(ts + 9*3600, 'unixepoch')`` -- we add nine hours and then
take the civil date, which is the Japan calendar day without storing a
second timestamp.

Reason categories (from reason_codes):
    infra     -> infrastructure stop (INFRA_STUCK)
    transient -> STUCK
    quality   -> VERIFY_FAILED, EVIDENCE_CONTRADICTED
    refusal   -> REFUSED
    user      -> CANCELLED by a person
    system    -> CANCELLED by automation, FANOUT
    ok        -> DONE
"""

from __future__ import annotations

# JST offset in seconds, applied only for display/day-bucketing.
_JST = "(ts + 32400)"

VIEWS_SQL = r"""
-- Runs with a JST civil day attached (display helper).
CREATE VIEW IF NOT EXISTS v_runs_jst AS
SELECT
    r.*,
    date(r.started_at + 32400, 'unixepoch')     AS day_jst,
    datetime(r.started_at + 32400, 'unixepoch')  AS started_jst,
    datetime(r.ended_at   + 32400, 'unixepoch')  AS ended_jst
FROM runs r;

-- Infrastructure stop rate, per JST day: INFRA_STUCK runs / all runs.
CREATE VIEW IF NOT EXISTS v_infra_stop_rate AS
SELECT
    date(started_at + 32400, 'unixepoch') AS day,
    SUM(CASE WHEN outcome = 'INFRA_STUCK' THEN 1 ELSE 0 END) AS infra_runs,
    COUNT(*)                                                AS total_runs,
    CAST(SUM(CASE WHEN outcome = 'INFRA_STUCK' THEN 1 ELSE 0 END) AS REAL)
        / NULLIF(COUNT(*), 0)                              AS infra_stop_rate
FROM runs
WHERE started_at IS NOT NULL
GROUP BY day;

-- Incident recovery: detected->recovered seconds per incident_id, plus the
-- overall median across incidents (as a scalar repeated on every row).
CREATE VIEW IF NOT EXISTS v_incident_recovery AS
WITH paired AS (
    SELECT
        d.incident_id,
        MIN(d.ts) AS detected_at,
        MIN(rec.ts) AS recovered_at
    FROM infra_events d
    JOIN infra_events rec
      ON rec.incident_id = d.incident_id
     AND rec.kind = 'recovered'
    WHERE d.kind = 'detected'
    GROUP BY d.incident_id
),
secs AS (
    SELECT incident_id, detected_at, recovered_at,
           (recovered_at - detected_at) AS recovery_s
    FROM paired
    WHERE recovered_at IS NOT NULL
)
SELECT
    s.incident_id,
    s.detected_at,
    s.recovered_at,
    s.recovery_s,
    (SELECT AVG(recovery_s) FROM (
        SELECT recovery_s FROM secs ORDER BY recovery_s
        LIMIT 2 - (SELECT COUNT(*) FROM secs) % 2
        OFFSET (SELECT (COUNT(*) - 1) / 2 FROM secs)
    )) AS recovery_median_s
FROM secs s
ORDER BY s.detected_at;

-- Per-request final completion. A request counts as completed if, across all
-- its attempts, at least one run reached DONE. Requests whose runs are only
-- cancelled, or only FANOUT (split parents), are excluded from the
-- denominator.
CREATE VIEW IF NOT EXISTS v_request_final_completion AS
WITH per_request AS (
    SELECT
        req.request_id,
        MAX(CASE WHEN run.outcome = 'DONE' THEN 1 ELSE 0 END)   AS any_done,
        SUM(CASE WHEN run.outcome NOT IN ('CANCELLED','FANOUT')
                 THEN 1 ELSE 0 END)                             AS substantive_runs
    FROM requests req
    LEFT JOIN runs run ON run.request_id = req.request_id
    GROUP BY req.request_id
)
SELECT
    COUNT(*)                                     AS requests_considered,
    SUM(any_done)                                AS requests_done,
    CAST(SUM(any_done) AS REAL) / NULLIF(COUNT(*), 0) AS final_completion_rate
FROM per_request
WHERE substantive_runs > 0;

-- Turn error rate per JST day. error turns / all turns.
CREATE VIEW IF NOT EXISTS v_turn_error_rate AS
SELECT
    date(started_at + 32400, 'unixepoch') AS day,
    COUNT(*)                                                     AS turns,
    SUM(CASE WHEN outcome = 'error' THEN 1 ELSE 0 END)           AS turns_error,
    SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END)       AS turns_completed,
    CAST(SUM(CASE WHEN outcome = 'error' THEN 1 ELSE 0 END) AS REAL)
        / NULLIF(COUNT(*), 0)                                   AS turn_error_rate
FROM turns
WHERE started_at IS NOT NULL
GROUP BY day;

-- Manual intervention rate: user cancels / all runs, per JST day. 'user'
-- cancels come from interventions(actor='user', action='cancel'); runs are
-- the day's run total.
CREATE VIEW IF NOT EXISTS v_manual_intervention_rate AS
WITH run_day AS (
    SELECT date(started_at + 32400, 'unixepoch') AS day, COUNT(*) AS runs
    FROM runs WHERE started_at IS NOT NULL GROUP BY day
),
interv_day AS (
    SELECT date(ts + 32400, 'unixepoch') AS day, COUNT(*) AS manual_cancels
    FROM interventions
    WHERE actor = 'user' AND action = 'cancel'
    GROUP BY day
)
SELECT
    rd.day,
    COALESCE(id.manual_cancels, 0)                              AS manual_cancels,
    rd.runs                                                     AS runs,
    CAST(COALESCE(id.manual_cancels, 0) AS REAL)
        / NULLIF(rd.runs, 0)                                    AS manual_intervention_rate
FROM run_day rd
LEFT JOIN interv_day id ON id.day = rd.day;

-- Availability from heartbeats, per JST day and component. up fraction.
CREATE VIEW IF NOT EXISTS v_availability AS
SELECT
    date(ts + 32400, 'unixepoch') AS day,
    component,
    COUNT(*)                                            AS samples,
    SUM(CASE WHEN up = 1 THEN 1 ELSE 0 END)             AS up_samples,
    CAST(SUM(CASE WHEN up = 1 THEN 1 ELSE 0 END) AS REAL)
        / NULLIF(COUNT(*), 0)                           AS availability
FROM heartbeats
GROUP BY day, component;
"""

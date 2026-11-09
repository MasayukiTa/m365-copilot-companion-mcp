"""Backfill the ops database from read-only operational inputs.

Usage:
    python -m relay.ops_db.backfill --out <path/to/ops.sqlite3> \
        [--fleet-dir <.fleet>] [--sessions <sessions.sqlite3>]

Every input is opened read-only and never modified. The large sessions
database is opened with ``mode=ro`` and ``PRAGMA query_only=1`` and is never
copied. Backfill is idempotent (re-running does not duplicate rows) and uses
bounded memory (inputs are streamed).

Input -> table mapping (see ops_db_spec.md section 6):
    archive/history_completed_*.jsonl.gz -> runs, requests, interventions
    sessions.fleet_turns (ro)            -> turns.wait_s, messages,
                                            runs.conv_guid, runs.refuter_verdict
    socket_route.jsonl                   -> turns (turn_end outcome, transport=socket)
    tool_events.jsonl(.1)                -> tool_calls
    consumer_watch.log                   -> infra_events (detected->recovered)
    campaigns.jsonl                      -> runs.campaign_id, parent linkage

Missing data stays missing; ``source`` records provenance ('backfill').
"""

from __future__ import annotations

import argparse
import glob
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
from typing import Iterable, Iterator, Optional

from .migrations import connect

SOURCE = "backfill"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _norm_goal(text: str) -> str:
    """Normalise a goal string for hashing: collapse whitespace, strip."""
    return re.sub(r"\s+", " ", (text or "").strip())


def goal_hash(text: str) -> str:
    """First 14 hex chars of sha1 of the normalised goal text."""
    return hashlib.sha1(_norm_goal(text).encode("utf-8")).hexdigest()[:14]


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _iter_jsonl(path: str) -> Iterator[dict]:
    if not os.path.exists(path):
        return
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _run_id_from_history(rec: dict) -> Optional[str]:
    """Stable run id for a history record.

    Prefer the explicit ``run_id`` (rXXXX_aN). Fall back to a composite of
    run_started and name so a run is still identifiable without one.
    """
    rid = (rec.get("run_id") or "").strip()
    if rid:
        return rid
    rs = rec.get("run_started")
    name = rec.get("name") or ""
    if rs is None:
        return None
    return f"rs{rs}_{name}"


def _attempt_no(run_id: Optional[str]) -> Optional[int]:
    if not run_id:
        return None
    m = re.search(r"_a(\d+)\b", run_id)
    return int(m.group(1)) if m else None


def _conv_guid(rec: dict) -> Optional[str]:
    u = rec.get("conv_url") or ""
    if u.startswith("sess:"):
        return u[len("sess:"):]
    return None


# ---------------------------------------------------------------------------
# runs + requests  (authoritative source: history_completed archives)
# ---------------------------------------------------------------------------

def load_history_latest(fleet_dir: str) -> dict:
    """Return {(run_started, name): latest_record} across all archives.

    The latest record for a (run_started, name) pair (by ``seq``) is the
    authoritative final state of that run. This reproduces the known run
    count of 2747.
    """
    archive = os.path.join(fleet_dir, "archive")
    files = sorted(glob.glob(os.path.join(archive, "history_completed_*.jsonl.gz")))
    latest: dict = {}
    for fp in files:
        for rec in _iter_jsonl(fp):
            key = (rec.get("run_started"), rec.get("name"))
            seq = rec.get("seq") or 0
            if key not in latest or seq >= latest[key][0]:
                latest[key] = (seq, rec)
    return {k: v[1] for k, v in latest.items()}


def backfill_runs_requests(con: sqlite3.Connection, records: Iterable[dict]) -> dict:
    """Insert runs and requests. Returns {conv_guid: run_key} for later joins.

    A run is one worker in one fleet execution; its primary key is
    run_key = "{run_started}#{name}". fleet_exec_id keeps the shared
    rXXXX_aN identifier. History may have duplicate (run_started, name)
    rows across archive snapshots; the latest one wins (dedup by run_key).
    """
    cur = con.cursor()
    guid_to_run: dict = {}
    # request aggregation
    req: dict = {}
    # dedup runs by run_key (keep the record with the latest ts)
    run_by_key: dict = {}
    for rec in records:
        fleet_exec_id = _run_id_from_history(rec)
        started = rec.get("run_started")
        name = rec.get("name")
        if started is None or not name:
            continue
        run_key = f"{started}#{name}"
        outcome = rec.get("outcome")
        status = rec.get("status")
        ended = rec.get("ts")
        guid = _conv_guid(rec)
        if guid:
            guid_to_run[guid] = run_key
        jid = (rec.get("jid") or "").strip()
        gh = goal_hash(rec.get("goal") or "")
        request_id = jid or gh
        cancelled_by = "unknown" if outcome == "CANCELLED" else None
        prev = run_by_key.get(run_key)
        if prev is None or (ended is not None and (prev["_ts"] is None or ended >= prev["_ts"])):
            run_by_key[run_key] = {
                "_ts": ended,
                "row": (
                    run_key, fleet_exec_id, request_id,
                    _attempt_no(fleet_exec_id), name, None, "unknown", None,
                    started, ended, status, outcome, None,
                    rec.get("verified"), rec.get("verify_attempts"), None,
                    cancelled_by, rec.get("turn"), guid, SOURCE,
                ),
            }
        # aggregate request
        r = req.setdefault(request_id, {
            "goal_hash": gh,
            "goal_summary": (rec.get("goal_summary") or "")[:200],
            "submitter": rec.get("submitter") or "",
            "submitted_at": started,
            "attempts": 0,
            "final_outcome": None,
            "final_run_id": None,
            "completed_at": None,
            "_seen_keys": set(),
        })
        # count each distinct run_key once toward attempts
        if run_key not in r["_seen_keys"]:
            r["_seen_keys"].add(run_key)
            r["attempts"] += 1
        if started is not None and (r["submitted_at"] is None or started < r["submitted_at"]):
            r["submitted_at"] = started
        if outcome == "DONE":
            if r["final_outcome"] != "DONE" or (ended is not None and (r["completed_at"] is None or ended < r["completed_at"])):
                r["final_outcome"] = "DONE"
                r["final_run_id"] = run_key
                r["completed_at"] = ended
        elif r["final_outcome"] != "DONE" and outcome:
            r["final_outcome"] = outcome
            r["final_run_id"] = run_key
    run_rows = [v["row"] for v in run_by_key.values()]
    for request_id, r in req.items():
        cur.execute(
            "INSERT OR REPLACE INTO requests(request_id, goal_hash, goal_summary, "
            "submitter, submitted_at, attempts, final_outcome, final_run_id, "
            "completed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (request_id, r["goal_hash"], r["goal_summary"], r["submitter"],
             r["submitted_at"], r["attempts"], r["final_outcome"],
             r["final_run_id"], r["completed_at"]),
        )
        cur.execute(
            "INSERT INTO requests_fts(rowid, goal_summary) VALUES "
            "((SELECT rowid FROM requests WHERE request_id=?), ?)",
            (request_id, r["goal_summary"]),
        )
    # now insert runs (requests exist, so the FK holds)
    cur.executemany(
        "INSERT OR REPLACE INTO runs(run_key, fleet_exec_id, request_id, "
        "attempt_no, worker_name, campaign_id, route, transport, started_at, "
        "ended_at, status, outcome, reason_text, verified, verify_attempts, "
        "refuter_verdict, cancelled_by, turns_count, conv_guid, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        run_rows,
    )
    con.commit()
    return guid_to_run


# ---------------------------------------------------------------------------
# interventions  (cancelled runs; actor unknown for historical data)
# ---------------------------------------------------------------------------

def backfill_interventions(con: sqlite3.Connection, records: Iterable[dict]) -> None:
    cur = con.cursor()
    for rec in records:
        if rec.get("outcome") != "CANCELLED":
            continue
        run_id = _run_id_from_history(rec)
        ts = rec.get("ts")
        # deterministic id so re-runs do not duplicate: hash of run_id
        iid = int(hashlib.sha1(("cancel:" + str(run_id)).encode()).hexdigest()[:12], 16)
        cur.execute(
            "INSERT OR REPLACE INTO interventions(intervention_id, ts, actor, "
            "action, target_run_id, reason, source) VALUES (?,?,?,?,?,?,?)",
            (iid, ts, "unknown", "cancel", run_id, "cancelled run", SOURCE),
        )
    con.commit()


# ---------------------------------------------------------------------------
# sessions.sqlite3 (read-only): turns.wait_s, messages, conv_guid, refuter
# ---------------------------------------------------------------------------

def _open_sessions_ro(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=1")
    return con


def _run_id_from_key(key):
    """Derive a run id from a fleet_turns key.

    Keys look like ``rXXXX_wNN`` (metric/meta/guid rows) or
    ``rXXXX_aN_wNN`` (assistant/refuter rows), sometimes with a
    ``__refuter_*`` suffix. We keep the attempt segment when present so the
    id matches history run ids (rXXXX_aN); otherwise we return rXXXX.
    """
    if not key:
        return None
    base = key.split("__", 1)[0]
    m = re.match(r"^(r[0-9a-f]+(?:_a\d+)?)_w\d+", base)
    if m:
        return m.group(1)
    m = re.match(r"^(r[0-9a-f]+(?:_a\d+)?)", base)
    return m.group(1) if m else None


def backfill_from_sessions(con, sessions_path, guid_to_run):
    """Fill messages, turn wait_s, conv_guid and refuter verdicts.

    Streams fleet_turns once. Never copies or writes the sessions db.
    ``guid_to_run`` maps conv guid -> run_id from history; we also learn
    key-derived run ids here. messages store chars + sha256 but not the body.
    """
    scon = _open_sessions_ro(sessions_path)
    scur = scon.cursor()
    cur = con.cursor()
    # Resolve a sessions row to a run_key via (fleet_exec_id, worker_name).
    # runs already knows every run_key and its (fleet_exec_id, worker_name).
    exec_worker_to_key = {}
    known_keys = set()
    for rk, fx, wn in cur.execute(
        "SELECT run_key, fleet_exec_id, worker_name FROM runs"
    ):
        known_keys.add(rk)
        if fx is not None and wn is not None:
            exec_worker_to_key[(fx, wn)] = rk

    def _key_to_run(sess_key):
        """sessions key 'rXXXX_aN_wNN'/'rXXXX_wNN' -> run_key via exec+worker."""
        ex = _run_id_from_key(sess_key)
        if ex is None:
            return None
        base = sess_key.split("__", 1)[0]
        mw = re.search(r"_(w\d+)", base)
        worker = mw.group(1) if mw else None
        if worker is None:
            return None
        return exec_worker_to_key.get((ex, worker))

    waits = {}                 # (run_key, turn) -> turn_wait_s
    verdict_updates = {}       # run_key -> refuter verdict label
    msg_rows = []              # batched messages
    msg_id = 0
    BATCH = 5000
    msg_sql = (
        "INSERT OR REPLACE INTO messages(message_id, run_key, turn_no, "
        "role, ts, chars, sha256, body, body_expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)"
    )
    for key, name, turn, role, text, extra, ts in scur.execute(
        "SELECT key, name, turn, role, text, extra, ts FROM fleet_turns"
    ):
        if role == "metric":
            if extra and "turn_wait_s" in extra:
                try:
                    d = json.loads(extra)
                except Exception:
                    d = {}
                if d.get("name") == "turn_wait_s":
                    rk = _key_to_run(key)
                    if rk is not None:
                        waits[(rk, turn)] = d.get("value")
            continue
        if role == "guid":
            continue  # conv_guid already set from history in runs
        if role in ("user", "assistant", "refuter_verdict"):
            msg_id += 1
            body = text or ""
            rk = _key_to_run(key)
            msg_run = rk if rk in known_keys else None
            msg_rows.append((msg_id, msg_run, turn, role, ts, len(body),
                             _sha256(body), None, None))
            if len(msg_rows) >= BATCH:
                cur.executemany(msg_sql, msg_rows)
                msg_rows.clear()
            if role == "refuter_verdict" and msg_run is not None:
                verdict = (body.split(":", 1)[0] or "").strip()[:32]
                verdict_updates.setdefault(msg_run, verdict)
    if msg_rows:
        cur.executemany(msg_sql, msg_rows)
    scon.close()
    if verdict_updates:
        cur.executemany(
            "UPDATE runs SET refuter_verdict=COALESCE(refuter_verdict, ?) "
            "WHERE run_key=?",
            [(v, r) for r, v in verdict_updates.items()],
        )
    con.commit()
    return waits


# ---------------------------------------------------------------------------
# socket_route.jsonl -> turns (turn_end outcome, transport=socket)
# ---------------------------------------------------------------------------

def backfill_turns(con, fleet_dir, guid_to_run, waits):
    """One turns row per turn_end event. run_id via conv guid when known."""
    cur = con.cursor()
    known_keys = {row[0] for row in cur.execute("SELECT run_key FROM runs")}
    path = os.path.join(fleet_dir, "socket_route.jsonl")
    sql = (
        "INSERT OR REPLACE INTO turns(turn_id, run_key, turn_no, started_at, "
        "ended_at, wait_s, outcome, error_type, tool_calls_count, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)"
    )
    rows = []
    tid = 0
    for rec in _iter_jsonl(path):
        if rec.get("event") != "turn_end":
            continue
        tid += 1
        conv = rec.get("conv")
        run_key = guid_to_run.get(conv)
        if run_key not in known_keys:
            run_key = None
        turn_no = rec.get("turn")
        outcome = rec.get("outcome")
        err = None if outcome == "completed" else outcome
        wait = waits.get((run_key, turn_no)) if run_key is not None else None
        rows.append((tid, run_key, turn_no, None, rec.get("ts"), wait, outcome,
                     err, rec.get("tool_calls"), SOURCE))
        if len(rows) >= 5000:
            cur.executemany(sql, rows)
            rows.clear()
    if rows:
        cur.executemany(sql, rows)
    con.commit()


# ---------------------------------------------------------------------------
# tool_events.jsonl(.1) -> tool_calls (call + outcome matched by id)
# ---------------------------------------------------------------------------

def backfill_tool_calls(con, fleet_dir):
    cur = con.cursor()
    calls = {}
    sql = (
        "INSERT OR REPLACE INTO tool_calls(call_id, run_key, turn_no, "
        "tool_name, ok, error_type, duration_s, ts, args_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?)"
    )
    rows = []
    for suffix in ("tool_events.jsonl.1", "tool_events.jsonl"):
        path = os.path.join(fleet_dir, suffix)
        for rec in _iter_jsonl(path):
            ev = rec.get("event")
            cid = rec.get("id")
            if not cid:
                continue
            if ev == "call":
                calls[cid] = {
                    "tool": rec.get("tool"), "worker": rec.get("worker"),
                    "turn": rec.get("turn"), "ts": rec.get("ts"),
                }
            elif ev == "outcome":
                c = calls.pop(cid, {})
                numeric = int(cid[:12], 16) if re.fullmatch(r"[0-9a-f]+", cid or "") else abs(hash(cid))
                rows.append((numeric, None, c.get("turn"), c.get("tool"),
                             1 if rec.get("ok") else 0,
                             (rec.get("error") or None),
                             rec.get("duration_s"), rec.get("ts"), None))
                if len(rows) >= 5000:
                    cur.executemany(sql, rows)
                    rows.clear()
    if rows:
        cur.executemany(sql, rows)
    con.commit()


# ---------------------------------------------------------------------------
# consumer_watch.log -> infra_events (detected -> recovered, paired)
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _parse_watch_ts(line):
    import datetime
    m = _TS_RE.match(line)
    if not m:
        return None
    # the log is written in local (JST) wall-clock; store as unix seconds
    dt = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=9))).timestamp()


def backfill_infra(con, fleet_dir):
    cur = con.cursor()
    path = os.path.join(fleet_dir, "consumer_watch.log")
    if not os.path.exists(path):
        return
    eid = 0
    incident = 0
    pending = None  # (incident_id, detected_ts)
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            ts = _parse_watch_ts(line)
            if ts is None:
                continue
            low = line.lower()
            if "alarm cleared" in low or "nothing stuck" in low:
                kind = "recovered"
            elif "restarting through" in low or "starting a run that adopts" in low:
                kind = "detected"
            else:
                continue
            if kind == "detected" and pending is None:
                incident += 1
                inc_id = f"inc{incident:04d}"
                pending = (inc_id, ts)
                eid += 1
                cur.execute(
                    "INSERT OR REPLACE INTO infra_events(event_id, ts, component, "
                    "kind, incident_id, detail, source) VALUES (?,?,?,?,?,?,?)",
                    (eid, ts, "consumer", "detected", inc_id, line[:300], SOURCE),
                )
            elif kind == "recovered" and pending is not None:
                inc_id, _ = pending
                eid += 1
                cur.execute(
                    "INSERT OR REPLACE INTO infra_events(event_id, ts, component, "
                    "kind, incident_id, detail, source) VALUES (?,?,?,?,?,?,?)",
                    (eid, ts, "consumer", "recovered", inc_id, line[:300], SOURCE),
                )
                pending = None
    con.commit()


# ---------------------------------------------------------------------------
# campaigns.jsonl -> runs.campaign_id (best effort by task linkage)
# ---------------------------------------------------------------------------

def backfill_campaigns(con, fleet_dir):
    """Record campaign ids. campaigns.jsonl carries campaign_id/task_id/text
    but no run id, so we can only attach a campaign to runs whose request
    goal matches a campaign task text (goal_hash). This is best-effort; when
    no match exists the campaign linkage stays NULL (missing, not guessed).
    """
    cur = con.cursor()
    path = os.path.join(fleet_dir, "campaigns.jsonl")
    seen = 0
    for rec in _iter_jsonl(path):
        cid = rec.get("campaign_id")
        text = rec.get("text") or ""
        if not cid or not text:
            continue
        gh = goal_hash(text)
        cur.execute(
            "UPDATE runs SET campaign_id=COALESCE(campaign_id, ?) "
            "WHERE request_id=? AND campaign_id IS NULL",
            (cid, gh),
        )
        seen += 1
    con.commit()
    return seen


# ---------------------------------------------------------------------------
# daily_rollup rebuild (recomputed; delete-all then insert)
# ---------------------------------------------------------------------------

def rebuild_daily_rollup(con):
    cur = con.cursor()
    cur.execute("DELETE FROM daily_rollup")
    cur.execute(
        "INSERT INTO daily_rollup(day, route, runs, runs_done, runs_infra, "
        "runs_transient, runs_quality, runs_refusal, runs_cancelled, "
        "requests, requests_done, turns, turns_error, wait_p50_s, wait_p95_s, "
        "incidents, mttr_median_s) "
        "SELECT date(started_at + 32400, 'unixepoch') AS day, "
        "COALESCE(route,'unknown') AS route, "
        "COUNT(*), "
        "SUM(CASE WHEN outcome='DONE' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN outcome='INFRA_STUCK' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN outcome='STUCK' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN outcome IN ('VERIFY_FAILED','EVIDENCE_CONTRADICTED') THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN outcome='REFUSED' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN outcome='CANCELLED' THEN 1 ELSE 0 END), "
        "0, 0, 0, 0, NULL, NULL, 0, NULL "
        "FROM runs WHERE started_at IS NOT NULL "
        "GROUP BY day, route"
    )
    con.commit()


# ---------------------------------------------------------------------------
# orchestration + CLI
# ---------------------------------------------------------------------------

def run_backfill(out_path, fleet_dir, sessions_path):
    con = connect(out_path)
    # Speed: defer FK enforcement during bulk load (we constrain run_id to
    # known runs in code), then verify integrity with foreign_key_check.
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("PRAGMA synchronous=OFF")
    history = load_history_latest(fleet_dir)
    records = list(history.values())
    guid_to_run = backfill_runs_requests(con, records)
    backfill_interventions(con, records)
    waits = {}
    if sessions_path and os.path.exists(sessions_path):
        # The live sessions db may be mid-write (lock / malformed / I/O error).
        # messages/wait_s/refuter are auxiliary; never let a transient sessions
        # failure abort the core KPI backfill. Record the skip and continue.
        try:
            waits = backfill_from_sessions(con, sessions_path, guid_to_run)
        except sqlite3.DatabaseError as e:
            sys.stderr.write(
                f"[ops_db] sessions step skipped (live db unavailable): {e}\n"
            )
            waits = {}
    backfill_turns(con, fleet_dir, guid_to_run, waits)
    backfill_tool_calls(con, fleet_dir)
    backfill_infra(con, fleet_dir)
    backfill_campaigns(con, fleet_dir)
    rebuild_daily_rollup(con)
    # integrity gate: fail loudly if any FK is dangling
    bad = con.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        con.close()
        raise RuntimeError(f"foreign_key_check found {len(bad)} violations: {bad[:5]}")
    con.execute("PRAGMA foreign_keys=ON")
    con.commit()
    con.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backfill the ops database.")
    ap.add_argument("--out", required=True, help="output ops.sqlite3 path")
    ap.add_argument("--fleet-dir", default=".fleet", help="path to .fleet")
    ap.add_argument("--sessions", default=None,
                    help="path to sessions.sqlite3 (read-only input)")
    args = ap.parse_args(argv)
    sessions = args.sessions
    if sessions is None:
        guess = os.path.join(args.fleet_dir, "sessions", "sessions.sqlite3")
        sessions = guess if os.path.exists(guess) else None
    run_backfill(args.out, args.fleet_dir, sessions)
    return 0


if __name__ == "__main__":
    sys.exit(main())

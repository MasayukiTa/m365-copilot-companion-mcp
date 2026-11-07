"""Reproduce the stage-1 known values from the live .fleet archives.

Run this to regenerate the known-value table in the stage-1 report:

    python -m relay.ops_db.verify_known_values \
        --fleet-dir <.fleet> --out <scratch ops.sqlite3> \
        [--turn-cutoff-ts <unix seconds>]

The sessions step is intentionally skipped (sessions is auxiliary and the
live db is often mid-write); the core KPIs do not depend on it. The turn_end
window 11/2..11/6 is bucketed in JST (UTC+9), matching the rollup.

The turn_end source (socket_route.jsonl) is a live, append-only file, so the
window total keeps growing after the known values were measured. To reproduce
the FROZEN known values (113880 / 113545 / 335), pass --turn-cutoff-ts with
the measurement cutoff (2026-11-06 15:14:11 JST = 1793945651.674643): only
turn_end rows at or before that instant are counted.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import statistics

from .backfill import run_backfill

# Measurement cutoff that freezes the turn_end window to the known values.
FROZEN_TURN_CUTOFF_TS = 1793945651.674643  # 2026-11-06 15:14:11 JST


def _jst_ts(day: int) -> float:
    tz = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime(2026, 11, day, 0, 0, 0, tzinfo=tz).timestamp()


def compute(out_path: str, fleet_dir: str, turn_cutoff_ts: float | None = None) -> dict:
    for ext in ("", "-wal", "-shm"):
        p = out_path + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    run_backfill(out_path, fleet_dir, None)  # sessions skipped (auxiliary)
    con = sqlite3.connect(out_path)
    try:
        runs = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        oc = dict(con.execute("SELECT outcome, COUNT(*) FROM runs GROUP BY outcome"))
        det = [r[0] for r in con.execute(
            "SELECT ts FROM infra_events WHERE kind='detected' ORDER BY ts")]
        rec = [r[0] for r in con.execute(
            "SELECT ts FROM infra_events WHERE kind='recovered' ORDER BY ts")]
        durs = sorted(b - a for a, b in zip(det, rec))
        lo, hi = _jst_ts(2), _jst_ts(6) + 86400
        if turn_cutoff_ts is not None:
            hi = min(hi, turn_cutoff_ts)
            # ended_at is the turn_end ts; freeze to rows at/below the cutoff.
            tw = con.execute(
                "SELECT COUNT(*), SUM(outcome='completed'), SUM(outcome='error') "
                "FROM turns WHERE ended_at>=? AND ended_at<=?", (lo, turn_cutoff_ts)
            ).fetchone()
        else:
            tw = con.execute(
                "SELECT COUNT(*), SUM(outcome='completed'), SUM(outcome='error') "
                "FROM turns WHERE ended_at>=? AND ended_at<?", (lo, hi)).fetchone()
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        con.close()
    return {
        "runs": runs,
        "INFRA_STUCK": oc.get("INFRA_STUCK"),
        "DONE": oc.get("DONE"),
        "CANCELLED": oc.get("CANCELLED"),
        "recoveries": len(durs),
        "recovery_median_s": int(statistics.median(durs)) if durs else None,
        "turn_total": tw[0], "turn_completed": tw[1], "turn_error": tw[2],
        "fk_violations": len(fk),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fleet-dir", default=".fleet")
    ap.add_argument("--out", required=True)
    ap.add_argument("--turn-cutoff-ts", type=float, default=None,
                    help="freeze turn_end to rows at/below this unix ts; "
                         "use the FROZEN_TURN_CUTOFF_TS to match known values")
    ap.add_argument("--frozen", action="store_true",
                    help="shortcut for --turn-cutoff-ts FROZEN_TURN_CUTOFF_TS")
    args = ap.parse_args(argv)
    cutoff = args.turn_cutoff_ts
    if args.frozen:
        cutoff = FROZEN_TURN_CUTOFF_TS
    result = compute(args.out, args.fleet_dir, cutoff)
    known = {"runs": 2747, "INFRA_STUCK": 27, "DONE": 1707, "CANCELLED": 118,
             "recoveries": 7, "recovery_median_s": 41}
    print(f"{'metric':<20} {'got':>10} {'known':>10}  ok")
    for k, exp in known.items():
        got = result[k]
        print(f"{k:<20} {str(got):>10} {str(exp):>10}  {'OK' if got == exp else 'DIFF'}")
    tag = "FROZEN" if cutoff is not None else "LIVE"
    print(f"turn_end 11/2..11/6 JST [{tag}]: total={result['turn_total']} "
          f"completed={result['turn_completed']} error={result['turn_error']} "
          f"(known 113880/113545/335)")
    print(f"foreign_key_check violations: {result['fk_violations']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

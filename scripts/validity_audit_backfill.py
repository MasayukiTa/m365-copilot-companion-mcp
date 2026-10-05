# -*- coding: utf-8 -*-
"""Backfill the validity audit ledger from conversations that already happened.

    python scripts/validity_audit_backfill.py --since "2026-10-05 21:00" --until "2026-10-05 22:15"
    python scripts/validity_audit_backfill.py ... --dry-run
    python scripts/validity_audit_backfill.py ... --store-dir <dir holding a COPY of sessions.sqlite3>

For every worker conversation in fleet_turns inside the window that touched a validity_* tool (or
names one in its goal) it appends the role-tagged rows to `validity_audit` (see
bridge/validity_audit.py), joining the tool ledger (.fleet/tool_events.jsonl) and the run history
(.fleet/history.json, for the final outcome and whether a review ran). Validity tool calls that no
worker conversation owns are bound to their own claim id, or 'UNATTRIBUTED'. Reviewer text from
before the reviewer was recorded does not exist; it is written as an explicit
`not_recorded(before 2026-10-06)` row.

IDEMPOTENT: run it twice and the second run inserts nothing. Times are local time (JST on the
owner's machine). --dry-run reads and reports and writes no ledger rows (the additive schema is
created if the database predates it). Exit code 1 when a worker failed to sync.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _ts(text):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return float(text)


def _history(path):
    out = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        sys.stderr.write("[backfill] cannot read %s: %s\n" % (path, exc))
        return out
    for e in data if isinstance(data, list) else []:
        if isinstance(e, dict) and e.get("run_id") and e.get("name"):
            out["%s_%s" % (e["run_id"], e["name"])] = e
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--store-dir", default="", help="directory of sessions.sqlite3 (default: the real one)")
    ap.add_argument("--ledger", default=os.path.join(REPO, ".fleet", "tool_events.jsonl"))
    ap.add_argument("--history", default=os.path.join(REPO, ".fleet", "history.json"))
    ns = ap.parse_args(argv)
    if ns.store_dir:
        os.environ["MCP_SESSION_STORE_DIR"] = ns.store_dir
    from bridge import session_store as S
    from bridge import validity_audit as V

    since, until = _ts(ns.since), _ts(ns.until)
    conn = S._db(import_files=False)
    try:
        keys = [r[0] for r in conn.execute(
            "SELECT DISTINCT key FROM fleet_turns WHERE role = 'meta' AND ts BETWEEN ? AND ? "
            "AND key NOT LIKE '%\\_\\_refuter\\_%' ESCAPE '\\' ORDER BY key", (since, until))]
    finally:
        conn.close()
    hist = _history(ns.history)
    failed, audited, ins, dup = 0, 0, 0, 0
    for key in keys:
        h = hist.get(key) or {}
        phases = [p.get("event") for p in (h.get("phase_events") or []) if isinstance(p, dict)]
        outcome = ({"outcome": h.get("outcome"), "status": h.get("status"),
                    "reason": h.get("reason") or ""} if h else None)
        try:
            st = V.sync_worker(key, ledger_path=ns.ledger, jid=h.get("jid") or "",
                               outcome=outcome, reviewed=("refuting" in phases),
                               dry_run=ns.dry_run)
        except Exception as exc:
            failed += 1
            print("FAILED  %s  %s: %s" % (key, type(exc).__name__, exc))
            continue
        if st.get("audited"):
            audited += 1
            ins += st.get("inserted", 0)
            dup += st.get("already_there", 0)
            print("%-28s claims=%s rows=%d inserted=%d already=%d%s" % (
                key, ",".join(st["claims"]), st["rows"], st.get("inserted", 0),
                st.get("already_there", 0), "  (dry-run)" if ns.dry_run else ""))
        else:
            print("%-28s skipped: %s" % (key, st.get("reason")))
    un = V.sync_unattributed_calls(ns.ledger, since, until, dry_run=ns.dry_run)
    print("unattributed validity tool calls: rows=%d inserted=%d already=%d" % (
        un["rows"], un["inserted"], un["already_there"]))
    print("workers audited=%d inserted=%d already_there=%d failed=%d" % (audited, ins, dup, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

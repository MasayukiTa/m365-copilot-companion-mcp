# -*- coding: utf-8 -*-
"""Print the reviewer's recorded conversation for one worker as ONE line of JSON (read-only).

    python scripts/ledger_dump.py --key r6ac3919c_a0_w0 [--store-dir <state>/sessions]

Used by the fleet cockpit (the review tab of a worker card and a history row) to show, from the
sqlite ledger, the full text the reviewer was sent and every reply, with lens, time and verdict.
The C# UI cannot open sqlite itself, so it runs this windowless and parses the line.

Output: {"key": ..., "turns": [{"role","lens","ts","sha16","route","seq","text","truncated"}...],
         "recorded": true|false}
`recorded` is false when the ledger holds nothing for this worker (a run from before the reviewer
was recorded) -- the UI says so instead of showing an empty list.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def dump(key):
    from bridge import session_store as S
    conn = S._db(import_files=False)
    try:
        prefix = key + "__refuter_"
        rows = conn.execute(
            "SELECT id, key, role, turn, text, extra, ts FROM fleet_turns "
            "WHERE substr(key, 1, ?) = ? AND role LIKE 'refuter\\_%' ESCAPE '\\' ORDER BY ts, id",
            (len(prefix), prefix)).fetchall()
    finally:
        conn.close()
    turns = []
    for r in rows:
        try:
            ex = json.loads(r["extra"] or "{}")
        except ValueError:
            ex = {}
        turns.append({"role": r["role"], "lens": ex.get("lens") or r["key"][len(prefix):],
                      "ts": r["ts"], "sha16": ex.get("sha16", ""), "route": ex.get("route", ""),
                      "seq": r["turn"], "text": r["text"],
                      "truncated": bool(ex.get("truncated")),
                      "orig_chars": ex.get("orig_chars", len(r["text"]))})
    return {"key": key, "turns": turns, "recorded": bool(turns)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--store-dir", default="")
    ns = ap.parse_args(argv)
    if ns.store_dir:
        os.environ["MCP_SESSION_STORE_DIR"] = ns.store_dir
    try:
        out = dump(ns.key)
    except Exception as exc:
        out = {"key": ns.key, "turns": [], "recorded": False,
               "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

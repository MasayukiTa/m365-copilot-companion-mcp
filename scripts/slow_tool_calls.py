# -*- coding: utf-8 -*-
"""List the slow tools/call requests from the response-delivery telemetry log.

WHAT THIS ANSWERS. When a client reports that a call "hangs" or is "cut after N seconds", the
first question is WHICH tool and HOW the call ended -- did the server take too long, or did the
client give up and disconnect first? The response telemetry
(tools/response_telemetry.py -> .setup/logs/response_telemetry.jsonl) now records, per /mcp
request, the JSON-RPC method, the tools/call tool name, the JSON-RPC id, and a split of the
wall-clock time into intake/network wait (`wait_ms`) and the tool body itself (`tool_ms`), plus
`end_reason` (complete / client_gone / error / stream) and, for a client that vanished,
`recv_to_disconnect_ms`. This script reads that log and prints every tools/call at or above a
threshold (default 5000 ms), slowest first, so the slow tool and the cut calls are both visible.

Read-only: nothing is written. Only the telemetry log is read.

  python scripts/slow_tool_calls.py [--path PATH] [--threshold-ms 5000] [--top 50]
                                    [--since-ts ISO] [--include-rotated] [--json]

With no --path, the live log and (with --include-rotated) its .1 .. .4 rotations beside it are
read. --since-ts keeps only lines whose `ts` is >= the given ISO-8601 prefix (string compare,
which works because the timestamps are fixed-width local ISO). Exit code is always 0; an empty
result is printed as such.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _default_log_path() -> str:
    """The live telemetry log, resolved the same way tools.response_telemetry does."""
    try:
        from tools import response_telemetry as rt
        return rt._log_path()
    except Exception:
        return os.path.join(REPO, ".setup", "logs", "response_telemetry.jsonl")


def _rotated_paths(path: str, keep: int = 4):
    """The live file followed by its .1 .. .keep rotations, those that exist."""
    out = [path]
    for i in range(1, keep + 1):
        p = "%s.%d" % (path, i)
        if os.path.exists(p):
            out.append(p)
    return out


def _iter_rows(paths):
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue
        except FileNotFoundError:
            continue
        except OSError:
            continue


def _is_tools_call(row: dict) -> bool:
    # A /mcp request whose JSON-RPC method is tools/call. rpc_method is written by the telemetry
    # for any request that carried a JSON-RPC body; rows from before this field existed have no
    # rpc_method and are not tools/call-identifiable, so they are skipped.
    return row.get("rpc_method") == "tools/call"


def _duration_ms(row: dict):
    v = row.get("duration_ms")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def collect(paths, threshold_ms: float, since_ts: str):
    """Return the tools/call rows at or above the threshold, slowest first."""
    hits = []
    for row in _iter_rows(paths):
        if not isinstance(row, dict) or not _is_tools_call(row):
            continue
        if since_ts and str(row.get("ts", "")) < since_ts:
            continue
        dur = _duration_ms(row)
        if dur is None or dur < threshold_ms:
            continue
        hits.append(row)
    hits.sort(key=lambda r: _duration_ms(r) or 0.0, reverse=True)
    return hits


def _fmt_row(row: dict) -> str:
    tool = row.get("tool") or "(no name)"
    dur = _duration_ms(row)
    dur_s = "%.1f" % dur if dur is not None else "?"
    parts = [
        "%8s ms" % dur_s,
        "%-28s" % str(tool)[:28],
        "end=%s" % (row.get("end_reason") or "?"),
    ]
    if row.get("wait_ms") is not None:
        parts.append("wait=%sms" % row.get("wait_ms"))
    if row.get("tool_ms") is not None:
        parts.append("tool=%sms" % row.get("tool_ms"))
    if row.get("recv_to_disconnect_ms") is not None:
        parts.append("cut_after=%sms" % row.get("recv_to_disconnect_ms"))
    if row.get("rpc_id") is not None:
        parts.append("id=%s" % row.get("rpc_id"))
    if row.get("ts"):
        parts.append("at=%s" % row.get("ts"))
    return "  ".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=None,
                    help="telemetry jsonl to read (default: the live response_telemetry.jsonl)")
    ap.add_argument("--threshold-ms", type=float, default=5000.0,
                    help="minimum duration_ms to list (default: 5000)")
    ap.add_argument("--top", type=int, default=50,
                    help="how many to print, slowest first (default: 50)")
    ap.add_argument("--since-ts", default="",
                    help="keep only rows whose ts is >= this ISO-8601 prefix (string compare)")
    ap.add_argument("--include-rotated", action="store_true",
                    help="also read the .1 .. .4 rotations beside the live file")
    ap.add_argument("--json", action="store_true",
                    help="print the matching rows as JSON instead of a table")
    args = ap.parse_args(argv)

    base = args.path or _default_log_path()
    paths = _rotated_paths(base) if args.include_rotated else [base]
    hits = collect(paths, args.threshold_ms, args.since_ts)
    shown = hits[: max(0, args.top)]

    if args.json:
        print(json.dumps(shown, ensure_ascii=False, indent=2))
        return 0

    print("slow tools/call >= %.0f ms (from %s)" % (args.threshold_ms, base))
    print("matched %d; showing %d" % (len(hits), len(shown)))
    if not shown:
        print("  (none)")
        return 0
    for row in shown:
        print(_fmt_row(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

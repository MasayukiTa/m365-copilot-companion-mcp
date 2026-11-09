#!/usr/bin/env python3
"""Read-only board communication telemetry (roadmap Phase B slice B3 / L4-pre gate).

WHY THIS EXISTS. The roadmap L4 gate asks, before any peer-to-peer communication is
turned on, to measure the board's communication *per completion*: how much talk each
completion costs, how much of it carries verified useful information, how much the
handed-off context grows, and whether reads/denies signal misinformation. This script
answers that from audit data that already exists on disk. It is SHADOW / READ-ONLY:
it opens two JSONL files, joins them, prints a report, and writes nothing. It changes
no behaviour and is not imported by the running server.

INPUTS (both under --fleet-dir, default .fleet):
  board.jsonl            one row per board entry. Coordinator 'result' rows mark a
                         completed subtask (ref = 'task <campaign_id>-<subtask_index>').
                         post / read / deny audit rows, when workers write to the board
                         (Phase B2), are counted here too; their absence is reported
                         honestly, not hidden.
  relay_findings_shadow.jsonl  one row per hand-off a child produced:
                         campaign_id, subtask_index, findings (verified useful items),
                         handoff_chars, result_chars.

METRICS (roadmap L4-pre):
  M1 communication per completion = board rows / completed subtasks.
  M2 useful ratio                 = hand-offs carrying >=1 finding / hand-offs.
  M3 context growth               = distribution of handoff_chars (and result_chars).
  M4 misinformation signal        = deny rows / (post+read+deny) audit rows.

VERDICT:
  INSUFFICIENT        - fewer than --min-completions completed subtasks, OR no hand-off
                        rows to judge usefulness against. (Honest: not a 'board is
                        worthless' finding, just not enough data yet.)
  NO_WORKER_TRAFFIC   - completions exist but the board has only coordinator rows, so
                        the per-completion communication cost of *peer* traffic cannot
                        be judged yet; M4 is undefined. Reported, not hidden.
  COMMUNICATION_PAYS  - useful ratio is at or above --useful-threshold.
  COMMUNICATION_THIN  - useful ratio is below the threshold (talk is not carrying its
                        weight; candidate to leave the board off / narrow it).

USAGE:
  python scripts/board_report.py [--fleet-dir .fleet] [--format text|json]
                                 [--min-completions 20] [--useful-threshold 0.5]

Nothing here is executed, written, or pruned.
"""
import argparse
import json
import os
import re
import statistics

_REF_RE = re.compile(r"task\s+(\S+?)-(\d+)\s*$")
_AUDIT_KINDS = ("post", "read", "deny")


def _read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    return rows


def _parse_ref(ref):
    m = _REF_RE.match(ref or "")
    if not m:
        return (None, None)
    try:
        return (m.group(1), int(m.group(2)))
    except (TypeError, ValueError):
        return (None, None)


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _findings_count(raw):
    if raw in (None, "", "[]", "{}"):
        return 0
    if isinstance(raw, (list, tuple)):
        return len(raw)
    if isinstance(raw, str):
        return raw.count("'kind'") or raw.count('"kind"') or (1 if raw.strip() else 0)
    return 1 if raw else 0


def build_report(fleet_dir, min_completions=20, useful_threshold=0.5):
    board = _read_jsonl(os.path.join(fleet_dir, "board.jsonl"))
    finds = _read_jsonl(os.path.join(fleet_dir, "relay_findings_shadow.jsonl"))

    board_rows = len(board)
    completed = set()
    kind_counts = {}
    audit_total = 0
    deny_total = 0
    for b in board:
        kind = b.get("kind") or ""
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        if kind == "result":
            cid, idx = _parse_ref(b.get("ref"))
            if cid is not None:
                completed.add((cid, idx))
        if kind in _AUDIT_KINDS:
            audit_total += 1
            if kind == "deny":
                deny_total += 1

    n_completed = len(completed)

    handoffs = 0
    useful_handoffs = 0
    handoff_chars = []
    result_chars = []
    for r in finds:
        handoffs += 1
        if _findings_count(r.get("findings")) >= 1:
            useful_handoffs += 1
        hc = _as_int(r.get("handoff_chars"))
        rc = _as_int(r.get("result_chars"))
        if hc is not None:
            handoff_chars.append(hc)
        if rc is not None:
            result_chars.append(rc)

    def _dist(vals):
        if not vals:
            return {"n": 0, "median": None, "p90": None, "max": None}
        s = sorted(vals)
        p90 = s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]
        return {
            "n": len(vals),
            "median": statistics.median(vals),
            "p90": p90,
            "max": max(vals),
        }

    comm_per_completion = (board_rows / n_completed) if n_completed else None
    useful_ratio = (useful_handoffs / handoffs) if handoffs else None
    misinfo_ratio = (deny_total / audit_total) if audit_total else None

    if n_completed < min_completions or handoffs == 0:
        verdict = "INSUFFICIENT"
    elif audit_total == 0:
        verdict = "NO_WORKER_TRAFFIC"
    elif useful_ratio is not None and useful_ratio >= useful_threshold:
        verdict = "COMMUNICATION_PAYS"
    else:
        verdict = "COMMUNICATION_THIN"

    return {
        "fleet_dir": fleet_dir,
        "board_rows": board_rows,
        "board_kinds": kind_counts,
        "completed_subtasks": n_completed,
        "handoffs": handoffs,
        "useful_handoffs": useful_handoffs,
        "audit_rows": audit_total,
        "deny_rows": deny_total,
        "M1_comm_per_completion": comm_per_completion,
        "M2_useful_ratio": useful_ratio,
        "M3_handoff_chars": _dist(handoff_chars),
        "M3_result_chars": _dist(result_chars),
        "M4_misinformation_ratio": misinfo_ratio,
        "min_completions": min_completions,
        "useful_threshold": useful_threshold,
        "verdict": verdict,
    }


def _fmt(v):
    return "n/a" if v is None else ("%.3f" % v if isinstance(v, float) else str(v))


def render_text(rep):
    lines = [
        "# Board communication telemetry (Phase B3, shadow / read-only)",
        "fleet-dir: %s" % rep["fleet_dir"],
        "board rows: %d  kinds: %s" % (rep["board_rows"], rep["board_kinds"]),
        "completed subtasks: %d" % rep["completed_subtasks"],
        "hand-offs: %d (useful: %d)" % (rep["handoffs"], rep["useful_handoffs"]),
        "audit rows (post/read/deny): %d (deny: %d)" % (rep["audit_rows"], rep["deny_rows"]),
        "",
        "- M1 communication per completion (board rows / completed): %s"
        % _fmt(rep["M1_comm_per_completion"]),
        "- M2 useful ratio (hand-offs carrying a finding): %s" % _fmt(rep["M2_useful_ratio"]),
        "- M3 context growth handoff_chars: median=%s p90=%s max=%s"
        % (
            _fmt(rep["M3_handoff_chars"]["median"]),
            _fmt(rep["M3_handoff_chars"]["p90"]),
            _fmt(rep["M3_handoff_chars"]["max"]),
        ),
        "- M3 context growth result_chars:  median=%s p90=%s max=%s"
        % (
            _fmt(rep["M3_result_chars"]["median"]),
            _fmt(rep["M3_result_chars"]["p90"]),
            _fmt(rep["M3_result_chars"]["max"]),
        ),
        "- M4 misinformation signal (deny / audit rows): %s"
        % _fmt(rep["M4_misinformation_ratio"]),
        "",
        "verdict: %s" % rep["verdict"],
    ]
    if rep["verdict"] == "NO_WORKER_TRAFFIC":
        lines.append(
            "  (board has only coordinator result rows; worker post/read/deny traffic"
            " has not been produced yet, so M4 is undefined. Not a 'board is worthless'"
            " finding.)"
        )
    elif rep["verdict"] == "INSUFFICIENT":
        lines.append(
            "  (fewer than %d completed subtasks, or no hand-off rows to judge usefulness.)"
            % rep["min_completions"]
        )
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Read-only board communication telemetry; shadow, nothing written."
    )
    ap.add_argument("--fleet-dir", default=None)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--min-completions", type=int, default=20)
    ap.add_argument("--useful-threshold", type=float, default=0.5)
    args = ap.parse_args(argv)

    fleet_dir = args.fleet_dir or os.path.join(os.getcwd(), ".fleet")
    rep = build_report(
        fleet_dir,
        min_completions=args.min_completions,
        useful_threshold=args.useful_threshold,
    )
    if args.format == "json":
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render_text(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

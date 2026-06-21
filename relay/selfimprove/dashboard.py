"""Read-only aggregator for the self-improvement controller dashboard (M365_HARDENING_AND_UX, Tier 2).

This is the *data backbone* of the "surpass" dashboard: it folds the several self-improvement ledgers
(the genome Archive, the burned-instance registry, the SWE grade-results stream, and the per-A/B
selfimprove_report_*.json files) into ONE structured ``dashboard_state`` dict that both a CLI and a
future WPF view can render. Think "status.json, but for the self-improvement controller".

Hard rule: this module is READ-ONLY. It opens ledgers to read, never to write or lock. A live
measurement may be appending to ``grade_results.jsonl`` while this runs -- we only ever read it.

Every section is defensive: a missing/empty/corrupt file degrades to an empty/zero section and NEVER
raises. Ordering is deterministic (by embedded ts when present, else file mtime; no randomness).

stdlib only; no network; JSON-safe output.
"""
from __future__ import annotations

import glob as _glob
import json
import os

# Resolve every default path relative to the repo root (this file lives at
# <root>/relay/selfimprove/dashboard.py, so the root is two directories up).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DEFAULT_ARCHIVE = os.path.join(_REPO_ROOT, "relay", "selfimprove", "archive", "entries.jsonl")
_DEFAULT_BURNED = os.path.join(_REPO_ROOT, "relay", "selfimprove", "burned.jsonl")
_DEFAULT_GRADE = os.path.join(_REPO_ROOT, ".fleet", "swe", "grade_results.jsonl")
_DEFAULT_REPORTS_GLOB = os.path.join(_REPO_ROOT, ".fleet", "swe", "selfimprove_report_*.json")


# --------------------------------------------------------------------------------------------------
# Small defensive readers
# --------------------------------------------------------------------------------------------------

def _read_jsonl(path) -> list:
    """Read a .jsonl file into a list of dicts. Missing/unreadable file -> []; bad lines skipped."""
    out: list = []
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except Exception:
        return []
    return out


def _read_json(path):
    """Read a single JSON object file. Missing/unreadable/non-dict -> None."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _mtime(path) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0


# --------------------------------------------------------------------------------------------------
# Section builders
# --------------------------------------------------------------------------------------------------

def _ab_history(reports_glob) -> list:
    """Build the A/B history (oldest->newest) from the selfimprove_report_*.json files.

    Sort key: an embedded numeric ``ts`` if present, else the file mtime. Each row pulls the gate's
    net_pp/p/verdict/keep so a renderer needs only this list, not the raw nested gate dict.
    """
    paths = []
    try:
        paths = sorted(_glob.glob(reports_glob)) if reports_glob else []
    except Exception:
        paths = []

    rows = []
    for p in paths:
        rep = _read_json(p)
        if rep is None:
            continue
        gate = rep.get("gate") if isinstance(rep.get("gate"), dict) else {}
        ts = rep.get("ts")
        sort_ts = ts if isinstance(ts, (int, float)) else _mtime(p)
        rows.append((sort_ts, {
            "ts": ts,
            "toggle": rep.get("toggle"),
            "n": rep.get("n"),
            "on_resolved": rep.get("on_resolved"),
            "off_resolved": rep.get("off_resolved"),
            "net_pp": _to_float(gate.get("net_pp")),
            "p": _to_float(gate.get("p")),
            "verdict": gate.get("verdict"),
            "keep": gate.get("keep"),
        }))

    rows.sort(key=lambda t: t[0])          # oldest -> newest, deterministic
    return [r for _, r in rows]


def _burned_ledger(burned_path) -> dict:
    """{'total', 'by_reason', 'recent'(last<=20)} from the burned.jsonl registry. Read-only."""
    recs = _read_jsonl(burned_path)
    by_reason: dict = {}
    for r in recs:
        reason = r.get("reason")
        if reason is None:
            reason = "unknown"
        by_reason[reason] = by_reason.get(reason, 0) + 1
    recent = [{"instance_id": r.get("instance_id"), "reason": r.get("reason")} for r in recs[-20:]]
    return {"total": len(recs), "by_reason": by_reason, "recent": recent}


def _archive_sections(archive_path):
    """Load the Archive (read-only) and return (pass1_trend, archive_section).

    Uses relay.selfimprove.archive.Archive so the on-disk format stays owned by one module. A missing
    file yields an empty Archive (Archive.__init__ already guards that), so this degrades to empties.
    """
    pass1_trend: list = []
    archive_section = {"count": 0, "genomes": [], "qd_cells": 0}
    try:
        from relay.selfimprove.archive import Archive
        arc = Archive(archive_path)
        entries = arc.all()
    except Exception:
        return pass1_trend, archive_section

    for e in entries:
        if not isinstance(e, dict):
            continue
        pass1_trend.append({
            "ts": e.get("ts"),
            "pass_at_1": _to_float(e.get("pass_at_1")),
            "ci": e.get("ci"),
        })

    genomes = []
    for e in entries[:50]:
        if not isinstance(e, dict):
            continue
        genomes.append({
            "id": e.get("id"),
            "parent_id": e.get("parent_id"),
            "pass_at_1": _to_float(e.get("pass_at_1")),
            "gate_verdict": e.get("gate_verdict"),
            "descriptors": e.get("descriptors"),
        })

    try:
        qd_cells = len(arc.qd_map())
    except Exception:
        qd_cells = 0

    archive_section = {"count": len(entries), "genomes": genomes, "qd_cells": qd_cells}
    return pass1_trend, archive_section


# --------------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------------

def dashboard_state(*, archive_path=None, burned_path=None, grade_results_path=None,
                    reports_glob=None) -> dict:
    """Aggregate the self-improvement ledgers into one JSON-safe ``dashboard_state`` dict.

    READ-ONLY: opens each ledger for reading only -- it never writes to or locks any of them (a live
    measurement may be appending to grade_results.jsonl). Every section is defensive: a missing or
    empty file produces an empty/zero section instead of raising.

    Args (all optional; None -> the repo-root default for that ledger):
      archive_path        relay/selfimprove/archive/entries.jsonl
      burned_path         relay/selfimprove/burned.jsonl
      grade_results_path  .fleet/swe/grade_results.jsonl
      reports_glob        .fleet/swe/selfimprove_report_*.json

    Top-level sections: summary, ab_history, pass1_trend, burned_ledger, archive.
    """
    archive_path = _DEFAULT_ARCHIVE if archive_path is None else archive_path
    burned_path = _DEFAULT_BURNED if burned_path is None else burned_path
    grade_results_path = _DEFAULT_GRADE if grade_results_path is None else grade_results_path
    reports_glob = _DEFAULT_REPORTS_GLOB if reports_glob is None else reports_glob

    ab_history = _ab_history(reports_glob)
    burned_ledger = _burned_ledger(burned_path)
    pass1_trend, archive_section = _archive_sections(archive_path)

    # grade_results is read so missing-ness is exercised and the section is available to consumers;
    # the headline numbers come from the structured trend/history, but we surface its size cheaply.
    grade_recs = _read_jsonl(grade_results_path)

    latest_pass = pass1_trend[-1]["pass_at_1"] if pass1_trend else None
    latest_ab = None
    if ab_history:
        last = ab_history[-1]
        latest_ab = {
            "net_pp": last.get("net_pp"),
            "p": last.get("p"),
            "verdict": last.get("verdict"),
            "keep": last.get("keep"),
        }

    summary = {
        "latest_pass_at_1": latest_pass,
        "latest_ab": latest_ab,
        "burned_total": burned_ledger["total"],
        "archive_count": archive_section["count"],
        "grade_results_count": len(grade_recs),
    }

    return {
        "summary": summary,
        "ab_history": ab_history,
        "pass1_trend": pass1_trend,
        "burned_ledger": burned_ledger,
        "archive": archive_section,
    }


def render_text(state) -> str:
    """Format a ``dashboard_state`` dict as a compact ASCII human scorecard (a few lines)."""
    if not isinstance(state, dict):
        return "SELF-IMPROVEMENT SCORECARD\n  no data yet"

    summary = state.get("summary") or {}
    ab_history = state.get("ab_history") or []

    lines = ["SELF-IMPROVEMENT SCORECARD", "=" * 26]

    lp = summary.get("latest_pass_at_1")
    lines.append("latest pass@1 : %s" % ("n/a" if lp is None else ("%.3f" % lp)))

    latest_ab = summary.get("latest_ab")
    if latest_ab:
        net = latest_ab.get("net_pp")
        p = latest_ab.get("p")
        net_s = "n/a" if net is None else ("%+.1fpp" % net)
        p_s = "n/a" if p is None else ("%.3f" % p)
        lines.append("latest A/B    : %s  net=%s  p=%s  keep=%s"
                     % (latest_ab.get("verdict") or "n/a", net_s, p_s, latest_ab.get("keep")))
    else:
        lines.append("latest A/B    : n/a")

    lines.append("burned total  : %d" % int(summary.get("burned_total") or 0))
    lines.append("archive count : %d" % int(summary.get("archive_count") or 0))

    if ab_history:
        lines.append("A/B history (last %d):" % min(3, len(ab_history)))
        for row in ab_history[-3:]:
            net = row.get("net_pp")
            p = row.get("p")
            net_s = "n/a" if net is None else ("%+.1fpp" % net)
            p_s = "n/a" if p is None else ("%.3f" % p)
            lines.append("  - %s n=%s net=%s p=%s verdict=%s keep=%s"
                         % (row.get("toggle") or "?", row.get("n"), net_s, p_s,
                            row.get("verdict") or "?", row.get("keep")))
    else:
        lines.append("A/B history   : none")

    return "\n".join(lines)


def main() -> int:
    try:
        state = dashboard_state()
    except Exception:
        # Belt-and-suspenders: dashboard_state should never raise, but the CLI must never traceback.
        print("SELF-IMPROVEMENT SCORECARD\n  no data yet")
        return 0
    print(render_text(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

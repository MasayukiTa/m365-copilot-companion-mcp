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
import time as _time

# Resolve every default path relative to the repo root (this file lives at
# <root>/relay/selfimprove/dashboard.py, so the root is two directories up).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DEFAULT_ARCHIVE = os.path.join(_REPO_ROOT, "relay", "selfimprove", "archive", "entries.jsonl")
_DEFAULT_BURNED = os.path.join(_REPO_ROOT, "relay", "selfimprove", "burned.jsonl")
_DEFAULT_GRADE = os.path.join(_REPO_ROOT, ".fleet", "swe", "grade_results.jsonl")
_DEFAULT_REPORTS_GLOB = os.path.join(_REPO_ROOT, ".fleet", "swe", "selfimprove_report_*.json")

# Where write_json() drops the stable JSON feed the WPF view (ui/SelfImproveDashboard.cs) tails --
# the self-improvement analogue of .fleet/status.json. Under the repo root so the cockpit's
# .fleet-relative pathing finds it next to status.json.
_DEFAULT_JSON_OUT = os.path.join(_REPO_ROOT, ".fleet", "selfimprove_dashboard.json")


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
            # WHICH DATA THE NUMBER CAME FROM. Without this the claimable check below is
            # permanently red -- and a light that can never go green carries no information
            # and teaches the reader to skip the line. A report that does not record its
            # pools still reads as "not recorded", which is a true statement about the
            # report rather than a verdict about the claim.
            "pools": rep.get("pools") or [],
            "pool_reads": rep.get("pool_reads") or {},
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
    archive_section = {"count": 0, "records": 0, "genomes": [], "qd_cells": 0}
    try:
        from relay.selfimprove.archive import Archive
        arc = Archive(archive_path)
        entries = arc.all()
    except Exception:
        return pass1_trend, archive_section

    # A GENOME ID IS A CONTENT HASH, so re-measuring one appends a second row for the same
    # genome -- archive.py says the collision is deliberate. Two rows are therefore not two
    # adopted genomes, and a trend drawn straight over them shows a CORRECTION as progress.
    #
    # That is not hypothetical here. The live archive holds one genome measured twice: 0.34,
    # then 0.50. The 0.34 was a grading-host artifact -- 19 instances silently produced no
    # test output under a concurrent grade -- and the re-grade in isolation replaced it. The
    # commit that landed the correction says in as many words that the dashboard would now
    # show "0.34 -> 0.50", and it did: a measurement error, drawn as a 16-point improvement.
    #
    # Nothing is dropped. The superseded row stays in the trend, flagged, so the record of
    # having measured it twice survives; what changes is that it stops being counted as a
    # separate genome and stops being drawn as a rise.
    latest_at = {}
    for i, e in enumerate(entries):
        if isinstance(e, dict):
            latest_at[e.get("id")] = i

    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        pass1_trend.append({
            "ts": e.get("ts"),
            "pass_at_1": _to_float(e.get("pass_at_1")),
            "ci": e.get("ci"),
            "superseded": latest_at.get(e.get("id")) != i,
            "note": e.get("note"),
        })

    measured = {}
    for e in entries:
        if isinstance(e, dict):
            measured[e.get("id")] = measured.get(e.get("id"), 0) + 1

    genomes = []
    for i, e in enumerate(entries[:50]):
        if not isinstance(e, dict):
            continue
        if latest_at.get(e.get("id")) != i:
            continue                      # an earlier measurement of a genome listed below
        genomes.append({
            "id": e.get("id"),
            "parent_id": e.get("parent_id"),
            "pass_at_1": _to_float(e.get("pass_at_1")),
            "gate_verdict": e.get("gate_verdict"),
            "descriptors": e.get("descriptors"),
            "measurements": measured.get(e.get("id"), 1),
        })

    try:
        qd_cells = len(arc.qd_map())
    except Exception:
        qd_cells = 0

    # `count` answers "how many genomes has this loop adopted", which is a count of distinct
    # genomes; `records` keeps the raw row count so the difference is visible rather than lost.
    archive_section = {"count": len(measured), "records": len(entries),
                       "genomes": genomes, "qd_cells": qd_cells}
    return pass1_trend, archive_section


# --------------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------------


def _branch_section():
    """Branches, what is running right now, and every comparison attempt.

    THE POINT OF THIS SECTION IS THE ONE LINE THAT SAYS WHAT IS RUNNING.

    "Running on a harness nobody remembers naming" has no other symptom: the fleet works, runs
    complete, and the numbers look like numbers. Resolving the live harness back to a label --
    and reporting `unnamed` rather than rounding it to `base` -- is the only detector that
    state has, which is why it is the first thing in here and not a footnote.

    Read-only and defensive like every other section: a missing branches file or an archive
    that will not open produces an empty section, never an exception. A dashboard that cannot
    render because one ledger is absent is a dashboard nobody trusts during an incident.
    """
    out = {"active": {"kind": "unknown", "label": None}, "branches": [], "comparisons": [],
           "pending": 0, "instrument": {"measures": [], "note": ""}}
    try:
        import os as _os

        from relay.selfimprove import archive as A
        from relay.selfimprove import branches as BR
        from relay.selfimprove import compare as C

        repo = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        arc = A.Archive(_os.path.join(repo, ".fleet", "selfimprove", "archive.jsonl"))
        out["active"] = BR.describe_active(archive=arc)
        for label, ref in sorted(BR.read().items()):
            row = {"label": label, "genome_id": ref.get("genome_id"),
                   "last_run_at": ref.get("last_run_at"), "note": ref.get("note") or "",
                   "resolves": True}
            try:
                BR.resolve(label, archive=arc)
            except Exception:
                row["resolves"] = False
            out["branches"].append(row)

        measures, note = C.instrument_measures()
        out["instrument"] = {"measures": list(measures), "note": note}
        out["pending"] = len(C.pending())

        # EVERY attempt, oldest first. Not the best one, and not the latest one: a pair whose
        # history shows only its most favourable run, beside a control that starts another, is
        # an instrument for producing whichever answer was wanted.
        gone = C.withdrawn_ids()
        for row in C.read_results():
            if row.get("withdraws"):
                continue
            verdict = row.get("verdict")
            if row.get("request_id") in gone and verdict is not None:
                verdict = "WITHDRAWN"
            out["comparisons"].append({
                "at": row.get("at"),
                "a": (row.get("a") or {}).get("label"),
                "b": (row.get("b") or {}).get("label"),
                "verdict": verdict,
                "original_verdict": row.get("verdict") if verdict == "WITHDRAWN" else None,
                "why": row.get("why") or "",
                "refused": bool(row.get("refused")),
            })
    except Exception:
        return out
    return out


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

    # Live usage = the general-user lens (no bench needed): completion rate / turns / trend from real
    # runs, plus the persona-leak quality rate. Defensive: a failure here must never break the
    # bench-side dashboard (the default dict carries the persona keys so summary mirroring is safe).
    try:
        from relay.selfimprove.usage import usage_section
        usage = usage_section()
    except Exception:
        usage = {"n_tasks": 0, "completion_rate": None, "status_mix": {}, "trend": [],
                 "persona_leak_rate": None, "quality_scored": 0, "persona_flagged": []}

    summary = {
        "latest_pass_at_1": latest_pass,
        "latest_ab": latest_ab,
        "burned_total": burned_ledger["total"],
        "archive_count": archive_section["count"],
        "grade_results_count": len(grade_recs),
        # mirror the general-user quality headline up to the summary for one-glance reading
        "persona_leak_rate": usage.get("persona_leak_rate"),
        # From the newest report that recorded them; absent stays absent.
        "pools": (latest_ab or {}).get("pools") or [],
        "pool_reads": (latest_ab or {}).get("pool_reads"),
    }

    return {
        "summary": summary,
        "usage": usage,
        "branches": _branch_section(),
        "ab_history": ab_history,
        "pass1_trend": pass1_trend,
        "burned_ledger": burned_ledger,
        "archive": archive_section,
    }


def write_json(path=None) -> str:
    """Write ``dashboard_state()`` as pretty (indent=2) JSON to ``path`` and return the path.

    This is the stable feed for the WPF self-improvement view (ui/SelfImproveDashboard.cs), the
    self-improvement analogue of status.json. ``path`` defaults to ``.fleet/selfimprove_dashboard.json``
    under the repo root. The parent directory is created if missing.

    READ-ONLY on every ledger (it only WRITES the single output file); never raises. On any error the
    output is best-effort -- a partially writable filesystem still gets a valid JSON skeleton.
    """
    out_path = _DEFAULT_JSON_OUT if path is None else path
    try:
        state = dashboard_state()
    except Exception:
        state = {"summary": {}, "ab_history": [], "pass1_trend": [],
                 "burned_ledger": {"total": 0, "by_reason": {}, "recent": []},
                 "archive": {"count": 0, "records": 0, "genomes": [], "qd_cells": 0}}
    try:
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Write atomically-ish: a temp file then replace, so a tailing reader never sees a half file.
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, out_path)
    except Exception:
        # Belt-and-suspenders: a write failure must not raise out of a read-only dashboard helper.
        try:
            with open(out_path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
    return out_path



def _render_branches(state) -> list:
    """The branch block, with the running-harness line first."""
    section = state.get("branches") or {}
    active = section.get("active") or {}
    lines = ["", "BRANCHES", "-" * 26]

    kind, label = active.get("kind"), active.get("label")
    if kind == "branch":
        lines.append("running now   : %s" % label)
    elif kind == "base":
        lines.append("running now   : base (as shipped)")
    elif kind == "unnamed":
        # SAID LOUDLY, because it has no other symptom. Everything works; nobody can say what
        # is running.
        lines.append("running now   : UNNAMED HARNESS -- no branch points at it (%s)"
                     % str(active.get("harness_id") or "")[:12])
        lines.append("                nobody named what this machine is running")
    else:
        lines.append("running now   : unknown")

    for row in section.get("branches") or []:
        last = row.get("last_run_at")
        stamp = _time.strftime("%Y-%m-%d", _time.localtime(last)) if last else "never"
        broken = "" if row.get("resolves") else "  [BROKEN REF]"
        lines.append("  %-18s %s  last run %s%s"
                     % (row.get("label"), str(row.get("genome_id") or "")[:12], stamp, broken))
    if not section.get("branches"):
        lines.append("  (none)")

    instrument = section.get("instrument") or {}
    if instrument.get("measures"):
        lines.append("instrument    : sees %s -- %s"
                     % (", ".join(instrument["measures"]), instrument.get("note", "")[:60]))

    comparisons = section.get("comparisons") or []
    if comparisons or section.get("pending"):
        lines.append("comparisons   : %d recorded, %d queued (ALL attempts shown)"
                     % (len(comparisons), section.get("pending") or 0))
    for row in comparisons[-8:]:
        stamp = (_time.strftime("%m-%d %H:%M", _time.localtime(row["at"]))
                 if row.get("at") else "     ")
        verdict = row.get("verdict")
        # INCONCLUSIVE is not a quiet win and must not read like one. A refusal is not a
        # result at all, and neither is a withdrawn verdict.
        mark = {"A": "->", "B": "<-", "INCONCLUSIVE": "==",
                "WITHDRAWN": "xx", None: "--"}.get(verdict, "??")
        shown = verdict or ("refused" if row.get("refused") else "no verdict")
        lines.append("  %s %s %s %s %-12s %s"
                     % (stamp, row.get("a"), mark, row.get("b"), shown, (row.get("why") or "")[:52]))
    return lines


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

    # SECTION 21, AT THE PLACE A HUMAN READS THE NUMBER. The A/B line above shows a net
    # percentage-point gain and says nothing about which pool produced it or whether that
    # pool has been looked at often enough to have become optimisation feedback. A gain read
    # off the data used to tune the harness estimates fit, not generalisation, and the line
    # above is where that gets forgotten.
    #
    # Fails to "no" when the state does not say. An unannotated gain is exactly the thing the
    # rule is about, so silence has to read as "cannot claim" rather than as permission.
    from relay.selfimprove import episode_record as _ER
    _pools = summary.get("pools") or []
    _reads = summary.get("pool_reads")
    if not _pools:
        # THREE STATES, NOT TWO. "The report does not say" is a fact about the report and is
        # actionable (make the report say); "NO" is a verdict about the claim. Collapsing
        # them made this line print NO on every dashboard ever rendered.
        lines.append("claimable     : not recorded -- the report does not say which pool the "
                     "gain came from, so it cannot be read as an improvement")
    else:
        _claim = _ER.may_claim_improvement(
            [{"pool": p, "pool_version": "reported", "episode_id": "reported"}
             for p in _pools], pool_reads=_reads)
        lines.append("claimable     : %s -- %s"
                     % ("yes" if _claim["may_claim"] else "NO", _claim["reason"]))

    lines.append("burned total  : %d" % int(summary.get("burned_total") or 0))
    lines.append("archive count : %d" % int(summary.get("archive_count") or 0))

    # general-user QUALITY headline: persona-leak rate (None -> n/a, else NN.N%). ASCII-only.
    plr = summary.get("persona_leak_rate")
    lines.append("persona leak  : %s" % ("n/a" if plr is None else ("%.1f%%" % (plr * 100))))

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

    lines.extend(_render_branches(state))
    return "\n".join(lines)


def main(argv=None) -> int:
    """CLI: bare -> the text scorecard; ``--json`` -> ``json.dumps(dashboard_state(), indent=2)``.

    Both modes are defensive: missing data degrades to an empty state and NEVER tracebacks.
    """
    import sys
    args = sys.argv[1:] if argv is None else argv
    want_json = "--json" in args
    want_write = "--write" in args

    # --write: (re)generate the .fleet/selfimprove_dashboard.json feed and exit. This is what
    # the WPF dashboard window shells out to on open / refresh so the tailed JSON is current
    # rather than a stale snapshot. Defensive: never tracebacks, prints the path it wrote.
    if want_write:
        try:
            out = write_json()
            print(out)
            return 0
        except Exception as e:
            print("write_json error: %s: %s" % (type(e).__name__, e))
            return 1

    try:
        state = dashboard_state()
    except Exception:
        # Belt-and-suspenders: dashboard_state should never raise, but the CLI must never traceback.
        if want_json:
            print(json.dumps({"summary": {}, "ab_history": [], "pass1_trend": [],
                              "burned_ledger": {"total": 0, "by_reason": {}, "recent": []},
                              "archive": {"count": 0, "records": 0, "genomes": [], "qd_cells": 0}}, indent=2))
        else:
            print("SELF-IMPROVEMENT SCORECARD\n  no data yet")
        return 0

    if want_json:
        print(json.dumps(state, indent=2, ensure_ascii=False))
    else:
        print(render_text(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""Read-only report: the RETURN a communication EDGE carries, so "is a board edge worth its cost"
can be answered edge by edge, and the edges that carry nothing can be named as shadow-only pruning
candidates.

WHY THIS EXISTS (Phase C communication ROI, roadmap C5-C7). A board lets one fan-out child's finding
reach its siblings, but a communication path only pays for itself if what travels on it is used. The
sibling-cost report (scripts/sibling_cost_report.py, B3) measured the COST of duplicated work a board
could remove; this report measures the OTHER side of the same ledger -- the verified-useful
information each communication edge actually carries -- and flags the edges that carry none, as
PRUNING CANDIDATES. It is shadow: it only measures and names candidates; it prunes nothing, changes
no behaviour, and writes nothing.

WHAT AN EDGE IS. In the P0 board (relay/agent_board.py) the only writer is the coordinator, which
posts one `result` row per completed child that POINTS at where that child's answer lives. Each such
row is one communication edge of its campaign: the coordinator offering a sibling's result to the
others. What that edge would actually CARRY forward is recorded, shadow-only, by relay/relay_findings
(the findings -- discovered paths, confirmed facts, unresolved gaps -- a second stage could be handed),
one row per child keyed by (campaign_id, subtask_index).

INPUTS (all read-only, under one --fleet-dir; default the live .fleet):
  board.jsonl                   coordinator `result` rows: campaign_id, kind, author_role, subject,
                                ref, ts, expires_ts. One result row = one communication edge.
  relay_findings_shadow.jsonl   shadow hand-off rows: campaign_id, subtask_index, kinds
                                (path/fact/gap counts), handoff_chars, result_chars. The useful
                                information an edge would carry.

JOIN. An edge (campaign_id, subtask index parsed from the result `subject`/`ref`) is matched to the
hand-off row of the same (campaign_id, subtask_index). An edge with no matching hand-off, or whose
hand-off carried no finding (handoff_chars == 0 / no kinds), is a WASTEFUL EDGE: a communication path
that cost a board row but would forward nothing. These are the shadow pruning candidates.

MEASURES
  R1  edges: coordinator result rows (communication edges) in qualifying campaigns.
  R2  carrying edges: edges whose matched hand-off carried >= 1 finding.
  R3  findings carried: total path+fact+gap findings across carrying edges (verified-useful payload).
  R4  communication ROI: findings carried per edge (R3 / R1). Higher = edges earn their cost.
  R5  wasteful edges: edges with no finding carried (pruning candidates, shadow only).

HONESTY. A campaign qualifies only when it has >= 2 edges AND >= 1 hand-off row to join against (so a
campaign with a board but no shadow findings recorded is not silently scored). If too few campaigns
qualify, the verdict is INSUFFICIENT and says so; a pruning decision is never made from an unmeasured
quantity. Nothing here is executed, pruned, or written.

  python scripts/comm_roi_report.py [--fleet-dir .fleet] [--top 50] [--format text|json]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: A campaign is scored only with at least this many communication edges and a hand-off to join.
MIN_EDGES = 2
#: At least this many qualifying campaigns before a verdict other than INSUFFICIENT is given.
MIN_CAMPAIGNS = 8
#: A board's edges earn their cost when at most this share of them carry nothing. Above it, the
#: shadow advice is to consider pruning the silent edges.
WASTEFUL_SHARE_LIMIT = 0.25

#: The board row kind that is a communication edge (the coordinator's per-child result pointer).
EDGE_KIND = "result"
EDGE_ROLE = "coordinator"
#: subtask index appears in the result row as "subtask N" (subject) or "...-N" (ref). Parse both.
_SUBJ_IDX = re.compile(r"subtask\s+(\d+)", re.IGNORECASE)
_REF_IDX = re.compile(r"-(\d+)\b")


def _read_jsonl(path):
    """[dict] from a .jsonl file, skipping unparseable lines. [] when the file is absent. Pure read."""
    rows = []
    try:
        with io.open(path, encoding="utf-8-sig", errors="replace") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    continue
    except OSError:
        return []
    return rows


def load_fleet(fleet_dir):
    """(board_rows, findings_rows) read-only from fleet_dir. Rotated copies (.1) are included so a
    recently rotated log is not silently dropped."""
    board = []
    findings = []
    for name in ("board.jsonl", "board.jsonl.1"):
        board += _read_jsonl(os.path.join(fleet_dir, name))
    for name in ("relay_findings_shadow.jsonl", "relay_findings_shadow.jsonl.1"):
        findings += _read_jsonl(os.path.join(fleet_dir, name))
    return board, findings


def _edge_index(row):
    """The subtask index a coordinator result row points at, or None when it cannot be parsed."""
    m = _SUBJ_IDX.search(str(row.get("subject") or ""))
    if m:
        return int(m.group(1))
    m = _REF_IDX.search(str(row.get("ref") or ""))
    if m:
        return int(m.group(1))
    return None


def _finding_count(row):
    """How many findings a hand-off row carried (sum of its kind counts), from `kinds` or `findings`."""
    kinds = row.get("kinds")
    if isinstance(kinds, dict) and kinds:
        try:
            return sum(int(v) for v in kinds.values())
        except Exception:
            pass
    f = row.get("findings")
    if isinstance(f, list):
        return len(f)
    return 0


def _is_edge(row):
    return (str(row.get("kind") or "") == EDGE_KIND
            and str(row.get("author_role") or row.get("author") or "") == EDGE_ROLE)


def analyse(board_rows, findings_rows):
    """Join communication edges (coordinator result rows) to the shadow hand-off that says what each
    edge would carry, per campaign. Returns per-campaign rows and the global buckets. Pure."""
    # hand-off findings, keyed by (campaign_id, subtask_index). A re-settled slice may appear twice;
    # keep the richest (most findings) so a carrying edge is not hidden by an empty re-record.
    handoff = {}
    hkinds = {}
    for r in findings_rows:
        cid = str(r.get("campaign_id") or "")
        idx = r.get("subtask_index")
        if not cid or not isinstance(idx, int):
            continue
        c = _finding_count(r)
        key = (cid, idx)
        if c >= handoff.get(key, -1):
            handoff[key] = c
            k = r.get("kinds") if isinstance(r.get("kinds"), dict) else {}
            hkinds[key] = {kk: int(vv) for kk, vv in k.items()}

    campaigns_with_handoff = {cid for (cid, _idx) in handoff}

    buckets = {"board_rows": len(board_rows), "edges": 0, "non_edge_rows": 0,
              "edges_unparsed_index": 0, "findings_rows": len(findings_rows),
              "campaigns_with_handoff": len(campaigns_with_handoff)}

    per = {}  # campaign -> {"edges": [(idx, carried_count)]}
    for row in board_rows:
        if not _is_edge(row):
            buckets["non_edge_rows"] += 1
            continue
        cid = str(row.get("campaign_id") or "")
        if not cid:
            buckets["non_edge_rows"] += 1
            continue
        buckets["edges"] += 1
        idx = _edge_index(row)
        if idx is None:
            buckets["edges_unparsed_index"] += 1
        carried = handoff.get((cid, idx)) if idx is not None else None
        per.setdefault(cid, {"edges": []})["edges"].append((idx, carried))

    rows = []
    for cid in sorted(per):
        edges = per[cid]["edges"]
        n_edges = len(edges)
        carrying = sum(1 for _i, c in edges if c is not None and c >= 1)
        unmatched = sum(1 for _i, c in edges if c is None)
        empty = sum(1 for _i, c in edges if c == 0)
        findings_carried = sum(c for _i, c in edges if isinstance(c, int) and c > 0)
        wasteful = unmatched + empty
        has_handoff = cid in campaigns_with_handoff
        rows.append({
            "campaign": cid, "edges": n_edges, "carrying": carrying,
            "unmatched": unmatched, "empty": empty, "wasteful": wasteful,
            "findings_carried": findings_carried,
            "roi": (findings_carried / n_edges) if n_edges else 0.0,
            "wasteful_share": (wasteful / n_edges) if n_edges else 0.0,
            "qualifies": n_edges >= MIN_EDGES and has_handoff,
        })
    return {"buckets": buckets, "rows": rows}


def summarise(result):
    qual = [r for r in result["rows"] if r["qualifies"]]
    edges = sum(r["edges"] for r in qual)
    carrying = sum(r["carrying"] for r in qual)
    wasteful = sum(r["wasteful"] for r in qual)
    findings = sum(r["findings_carried"] for r in qual)
    return {
        "qualifying_campaigns": len(qual),
        "total_campaigns": len(result["rows"]),
        "edges": edges, "carrying_edges": carrying, "wasteful_edges": wasteful,
        "findings_carried": findings,
        "roi": (findings / edges) if edges else None,
        "wasteful_share": (wasteful / edges) if edges else None,
    }


def verdict(summary):
    """(verdict line) from summarise(). A pruning decision is never made from an unmeasured quantity."""
    if summary["qualifying_campaigns"] < MIN_CAMPAIGNS:
        if summary["edges"] == 0:
            return ("INSUFFICIENT (no communication edge found: no coordinator `result` row in the "
                    "board; the board is off or P0 wrote nothing)")
        return "INSUFFICIENT (<%d qualifying campaigns)" % MIN_CAMPAIGNS
    if summary["wasteful_share"] is None:
        return "INSUFFICIENT (no edges to score)"
    if summary["wasteful_share"] > WASTEFUL_SHARE_LIMIT:
        return "PRUNE_CANDIDATES"
    return "EDGES_EARN_COST"


def _pct(v):
    return "n/a" if v is None else "%.1f%%" % (100.0 * v)


def _roi(v):
    return "n/a" if v is None else "%.2f" % v


def render(result, summary, top=50):
    b = result["buckets"]
    lines = ["# Communication-ROI report (Phase C, shadow: edges that carry nothing are pruning "
             "candidates)", ""]
    lines += ["## Verdict", "", "**%s**" % verdict(summary), "",
              "Thresholds (script constants): a campaign is scored with >= %d communication edges "
              "and a hand-off to join; need >= %d qualifying campaigns; wasteful-edge share > %s "
              "=> PRUNE_CANDIDATES, otherwise EDGES_EARN_COST. Shadow only: nothing is pruned."
              % (MIN_EDGES, MIN_CAMPAIGNS, _pct(WASTEFUL_SHARE_LIMIT)), ""]
    lines += ["## Counts", "",
              "- board rows read: %d" % b["board_rows"],
              "- communication edges (coordinator result rows): %d" % b["edges"],
              "- non-edge board rows (other kinds/authors): %d" % b["non_edge_rows"],
              "- edges whose subtask index could not be parsed: %d" % b["edges_unparsed_index"],
              "- shadow hand-off rows read: %d" % b["findings_rows"],
              "- campaigns with at least one hand-off recorded: %d" % b["campaigns_with_handoff"],
              ""]
    lines += ["## Measures", "",
              "- R1 edges (qualifying campaigns): %d" % summary["edges"],
              "- R2 carrying edges (>= 1 finding forwarded): %d" % summary["carrying_edges"],
              "- R3 findings carried (verified-useful payload): %d" % summary["findings_carried"],
              "- R4 communication ROI (findings per edge): %s" % _roi(summary["roi"]),
              "- R5 wasteful edges (carry nothing; shadow pruning candidates): %d (%s of edges)"
              % (summary["wasteful_edges"], _pct(summary["wasteful_share"])),
              "- denominator: %d campaigns qualify of %d seen"
              % (summary["qualifying_campaigns"], summary["total_campaigns"]), ""]
    qual = [r for r in result["rows"] if r["qualifies"]]
    qual.sort(key=lambda r: (-r["wasteful"], -r["edges"]))
    qual = qual[:max(0, top)]
    lines += ["## Per campaign (qualifying, most wasteful edges first)", ""]
    if not qual:
        lines.append("none")
    else:
        lines += ["| campaign | edges | carrying | wasteful | findings | roi | wasteful share |",
                  "|---|---|---|---|---|---|---|"]
        for r in qual:
            lines.append("| %s | %d | %d | %d | %d | %s | %s |" % (
                r["campaign"], r["edges"], r["carrying"], r["wasteful"],
                r["findings_carried"], _roi(r["roi"]), _pct(r["wasteful_share"])))
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Read-only communication-ROI report; shadow pruning candidates, nothing pruned.")
    ap.add_argument("--fleet-dir", default=None,
                    help="directory holding the fleet records (default: the live one, read-only)")
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    fleet_dir = args.fleet_dir or os.path.join(REPO, ".fleet")
    board, findings = load_fleet(fleet_dir)
    result = analyse(board, findings)
    summary = summarise(result)
    if args.format == "json":
        out = json.dumps({"verdict": verdict(summary), "summary": summary,
                          "buckets": result["buckets"], "rows": result["rows"]},
                         ensure_ascii=False, indent=2) + "\n"
    else:
        out = render(result, summary, top=args.top)
    try:
        sys.stdout.write(out)
    except UnicodeEncodeError:
        sys.stdout.write(out.encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

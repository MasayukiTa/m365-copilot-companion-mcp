"""Record the SWE-bench Pro 50-run result into the self-improvement ledgers so the dashboard shows it.

This is the *record-on-grade* step that closes the loop the self-improvement dashboard visualizes: once
the Pro 50 has been graded on the eval host, this turns the REAL graded outcome into

  1. one Archive entry  -> a pass@1 trend point (the dashboard headline metric is derived from here)
  2. burned-ledger rows -> the 50 instances are marked seen, so they can never be reused for a future
                           headline claim or A/B slice (cf. feedback_no_benchmark_overfitting)
  3. a dashboard.json regen so the WPF dashboard reflects it on its next ~1s tick

It writes NOTHING that wasn't actually measured: pass@1 = resolved / graded, the genome records the
config the run truly used (no invented knobs), and the gate_verdict is "measured" (a measurement
record, NOT a significance-gated keep -- that requires an A/B, which this single arm is not).

Usage:
  # dry-run (prints what it would record, writes nothing):
  python -m bench.pro_record_result --grade <resolved.json> --preds .fleet/swe/pro_preds_50.json
  # commit:
  python -m bench.pro_record_result --grade <resolved.json> --preds .fleet/swe/pro_preds_50.json --commit

--grade accepts any of:
  * a JSON list of resolved instance_ids:                ["id1", "id2", ...]
  * {"resolved": [...], "total": N}                      (total optional; defaults to len(preds))
  * a per-instance map {"id1": true, "id2": false, ...}  (truthy / {"resolved": true} both work)
"""
import argparse
import json
import math
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.selfimprove.archive import Archive, descriptors          # noqa: E402
from relay.selfimprove.guards import BurnedRegistry                 # noqa: E402
from relay.selfimprove import dashboard                             # noqa: E402

SLICE_LABEL = "SWE-bench Pro 50 (multi-language)"


def _wilson(k, n, z=1.96):
    """95% Wilson interval for a binomial pass rate -- honest small-N error bars for the trend point."""
    if n == 0:
        return [0.0, 0.0]
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _load_preds(path):
    """Return the ordered list of instance_ids in the preds file (+ patch length per id for diff_size)."""
    ids, patch_len = [], {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = data if isinstance(data, list) else data.get("predictions", [])
    for r in rows:
        iid = r.get("instance_id")
        if not iid:
            continue
        ids.append(iid)
        patch_len[iid] = len(r.get("patch") or r.get("model_patch") or "")
    return ids, patch_len


def _load_resolved(path, all_ids):
    """Normalize the grade file into a set of resolved instance_ids, intersected with the graded slice."""
    with open(path, encoding="utf-8") as f:
        g = json.load(f)
    resolved = set()
    if isinstance(g, list):
        resolved = {x for x in g if isinstance(x, str)}
    elif isinstance(g, dict) and "resolved" in g and isinstance(g["resolved"], list):
        resolved = set(g["resolved"])
    elif isinstance(g, dict):
        for iid, v in g.items():
            ok = v is True or (isinstance(v, dict) and (v.get("resolved") is True or v.get("pass") is True))
            if ok:
                resolved.add(iid)
    return {i for i in resolved if i in set(all_ids)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--grade", required=True, help="grade result JSON (resolved ids / map / {resolved,total})")
    ap.add_argument("--preds", default=os.path.join(REPO, ".fleet", "swe", "pro_preds_50.json"))
    ap.add_argument("--per-tab-mb", type=int, default=None, help="the measured autoscale_per_tab_mb the run used")
    ap.add_argument("--parent-id", default=None, help="prior genome id this builds on (if any)")
    ap.add_argument("--commit", action="store_true", help="actually write (default: dry-run)")
    args = ap.parse_args(argv)

    ids, patch_len = _load_preds(args.preds)
    if not ids:
        print("ERROR: no instance_ids in preds %s" % args.preds)
        return 2
    resolved = _load_resolved(args.grade, ids)
    n = len(ids)
    k = len(resolved)
    pass_at_1 = round(k / n, 4)
    ci = _wilson(k, n)

    # Per-instance records for behaviour descriptors: diff_size from the captured patch, miss_class
    # = "none" when resolved (a real solve), "other" otherwise (we don't claim a finer class here).
    recs = [{"diff_size": patch_len.get(i, 0), "turns": 0,
             "miss_class": "none" if i in resolved else "other"} for i in ids]
    desc = descriptors(recs)

    # The genome is exactly what the run used -- no invented knobs. The card under measurement is the
    # interface-first public-contract strengthening in bench/pro_stage_goals.py.
    genome = {
        "knobs": {
            "SWE_SIDEPAGE_RESERVE": "0",
            "effort": "auto",
            "batch": 8,
            "autoscale_per_tab_mb": args.per_tab_mb,
        },
        "cards": {"interface_first_public_contract": True},
        "parent_id": args.parent_id,
    }
    reason = "%s 2026-06-24 (N=%d)" % (SLICE_LABEL, n)

    print("=== Pro 50 record (%s) ===" % ("COMMIT" if args.commit else "DRY-RUN"))
    print("  graded      : %d / %d resolved" % (k, n))
    print("  pass@1      : %.4f  (95%% CI %s)" % (pass_at_1, ci))
    print("  descriptors : %s" % desc)
    print("  genome.knobs: %s" % genome["knobs"])
    print("  burn reason : %s" % reason)
    if not args.commit:
        print("  (dry-run: nothing written; re-run with --commit)")
        return 0

    arc = Archive()
    eid = arc.add(genome, slice_ids=ids, pass_at_1=pass_at_1, ci=ci,
                  gate_verdict="measured", descriptors=desc)
    burned = BurnedRegistry()
    n_new = burned.add(ids, reason)
    out = dashboard.write_json()
    print("  archive id  : %s" % eid)
    print("  burned new  : %d (already-burned skipped)" % n_new)
    print("  dashboard   : %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

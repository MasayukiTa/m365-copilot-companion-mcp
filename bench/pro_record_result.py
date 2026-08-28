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


from bench import swe_run_facts
from relay.outcomes import scoring_of


def _wilson(k, n, z=1.96):
    """95% Wilson interval for a binomial pass rate -- honest small-N error bars for the trend point."""
    if n == 0:
        return [0.0, 0.0]
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _load_run_config(path):
    """What the run recorded about which arm it was. Missing reads as empty, never as a guess."""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def _load_preds(path):
    """Instance ids in the preds file (+ patch length per id), and what is wrong with them.

    THE DENOMINATOR WAS WHATEVER HAPPENED TO BE IN THIS FILE. Nothing checked that the ids
    were unique or that they were the slice anyone meant to measure, so a partial file scored
    as a complete run -- a five-instance file with four solves is "pass@1 = 0.80" and reads
    like a triumph. Duplicates were worse than merely wrong: an id appearing twice counted
    twice in the denominator while `resolved` is a set and counted once, so the same file
    could not even score itself consistently.

    Returns (ids, patch_len, problems) with ids DEDUPED in first-seen order. `problems` is a
    list of strings; the caller refuses to commit while it is non-empty.
    """
    ids, patch_len, problems = [], {}, []
    seen, dupes = set(), []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = data if isinstance(data, list) else data.get("predictions", [])
    blank = 0
    for r in rows:
        iid = r.get("instance_id")
        if not iid:
            blank += 1
            continue
        if iid in seen:
            dupes.append(iid)
            continue
        seen.add(iid)
        ids.append(iid)
        patch_len[iid] = len(r.get("patch") or r.get("model_patch") or "")
    if blank:
        problems.append("%d prediction row(s) carry no instance_id" % blank)
    if dupes:
        problems.append("%d duplicate instance_id(s), e.g. %s"
                        % (len(dupes), ", ".join(sorted(set(dupes))[:3])))
    return ids, patch_len, problems


def _slice_problems(ids, expected_path):
    """How the graded slice differs from the canonical one, if the canonical one is readable.

    The grade file documents a `total` this program ignored, and no step asserted that the
    fifty instances of the named slice were all present. A run that staged badly, timed out
    halfway, or was pointed at a hand-edited file produced a smaller denominator and a higher
    rate, and the archive recorded it under the same slice label as a complete run.
    """
    try:
        with open(expected_path, encoding="utf-8") as f:
            rows = json.load(f)
        expected = {r["instance_id"] for r in rows if isinstance(r, dict) and r.get("instance_id")}
    except (OSError, ValueError, KeyError, TypeError):
        # NOT a problem to block on: the canonical file is not always beside the recorder, and
        # refusing to record because it is missing would be a new way to lose a real result.
        return [], None
    got = set(ids)
    missing, extra = expected - got, got - expected
    problems = []
    if missing:
        problems.append("%d of the slice's %d instances are absent from the predictions "
                        "(e.g. %s)" % (len(missing), len(expected),
                                       ", ".join(sorted(missing)[:3])))
    if extra:
        problems.append("%d prediction(s) are not part of this slice (e.g. %s)"
                        % (len(extra), ", ".join(sorted(extra)[:3])))
    return problems, len(expected)


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
    ap.add_argument("--note", default=None,
                    help="why this measurement was taken -- required in spirit when re-measuring "
                         "a genome already in the archive, because the identical id already says "
                         "WHICH row is replaced and only the reason is missing")
    ap.add_argument("--history", default=os.path.join(REPO, ".fleet", "history.json"),
                    help="the fleet ledger this run wrote; supplies each instance's outcome "
                         "and turn count")
    ap.add_argument("--wtmap", default=os.path.join(REPO, ".fleet", "swe", "pro_wt_map.json"),
                    help="instance_id -> worktree path, the key the ledger joins on")
    ap.add_argument("--run-config",
                    default=os.path.join(REPO, ".fleet", "swe", "pro_run_config.json"),
                    help="what the run wrote about which arm it was (effort, harness id, "
                         "resolved parameters)")
    ap.add_argument("--slice-file",
                    default=os.path.join(REPO, ".fleet", "swe", "pro_slice50_full.json"),
                    help="the canonical slice this measurement claims to be of; the "
                         "predictions are checked against it")
    ap.add_argument("--force", action="store_true",
                    help="record even though the slice does not check out. The reason must be "
                         "in --note, because the archive will carry this row as though it "
                         "measured the named slice.")
    ap.add_argument("--commit", action="store_true", help="actually write (default: dry-run)")
    args = ap.parse_args(argv)

    ids, patch_len, problems = _load_preds(args.preds)
    if not ids:
        print("ERROR: no instance_ids in preds %s" % args.preds)
        return 2
    slice_problems, slice_size = _slice_problems(ids, args.slice_file)
    problems += slice_problems
    resolved = _load_resolved(args.grade, ids)

    # WHAT THE FLEET RECORDED ABOUT THIS RUN, joined onto the graded slice.
    #
    # `pro_capture.py` writes a prediction row for every worktree that still exists, whatever
    # the worker's outcome was. A goal a human STOPPED and a goal whose connection never
    # established both arrive here as an empty patch, grade as unresolved, and used to enter
    # the denominator as failures -- measuring the operator and the environment rather than
    # the agent. The outcome was recorded all along, in the fleet's own ledger; nothing read it.
    # READ FIRST, because the ledger join below is scoped by the run's start time.
    run_cfg = _load_run_config(args.run_config)
    # SCOPED TO THIS RUN. The ledger is global and the worktree map is not: without a start
    # time, a path reused by an earlier run contributes that run's DONE (making the instance
    # immortally solved) and its turn count (making this run's cost the path's lifetime cost).
    since = run_cfg.get("started")
    facts = swe_run_facts.load(args.wtmap, args.history, since=since)
    join = swe_run_facts.join_report(ids, facts)
    if since is None and join["joined"]:
        problems.append("the run start time is unknown, so the ledger was joined UNSCOPED -- "
                        "outcomes and turn counts here may belong to an earlier run")

    # FAIL-CLOSED. An instance the ledger does not cover STAYS IN THE DENOMINATOR. Excluding
    # what we could not verify is the direction that flatters the score, and it is the
    # direction an unread ledger would take every instance in.
    # THE TURN COUNT DECIDES, not the outcome alone. A goal stopped before its first turn was
    # never attempted; one stopped after several turns was attempted and then given up on, and
    # excluding those is how a habit of stopping doomed runs raises the reported rate.
    excluded = [i for i in ids
                if i in facts
                and scoring_of(facts[i]["outcome"], facts[i].get("turns")) == "excluded"]
    graded_ids = [i for i in ids if i not in set(excluded)]

    n_all = len(ids)
    n = len(graded_ids)
    k = len([i for i in resolved if i in set(graded_ids)])
    if not n:
        print("ERROR: every instance was excluded -- there is nothing to score")
        return 2
    pass_at_1 = round(k / n, 4)
    ci = _wilson(k, n)
    # TWO RATES, NEVER ONE. Excluding work RAISES pass@1 because excluding leaves the
    # denominator; `end_to_end` is the one an unhealthy run cannot flatter.
    end_to_end = round(k / n_all, 4)

    # Per-instance records for behaviour descriptors: diff_size from the captured patch, miss_class
    # = "none" when resolved (a real solve), "other" otherwise (we don't claim a finer class here).
    # `turns` was a hardcoded 0 -- a field that is always zero is worse than a missing one,
    # because every turn-based descriptor computed from it was a statement about nothing.
    recs = [{"diff_size": patch_len.get(i, 0),
             "turns": (facts.get(i) or {}).get("turns", 0),
             "miss_class": "none" if i in resolved else "other"} for i in graded_ids]
    desc = descriptors(recs)

    # The genome is exactly what the run used -- no invented knobs. The card under measurement is the
    # interface-first public-contract strengthening in bench/pro_stage_goals.py.
    # THE ARM THIS RUN ACTUALLY WAS, read from what the run wrote rather than restated here.
    # `effort` was the literal "auto", so every archived result claimed the same arm whatever
    # had run -- and since the panel and research budgets were not manifest parameters either,
    # two efforts also hashed to the SAME harness_id. A remembered conclusion about which
    # effort scored higher could not be checked against anything in the archive.
    genome = {
        "knobs": {
            "SWE_SIDEPAGE_RESERVE": "0",
            # UNKNOWN, not "auto". A run whose arm was not recorded must read as unknown: a
            # guess here is how an unlabelled row joins the arm it did not belong to.
            "effort": run_cfg.get("effort") or "unknown",
            "harness_id": run_cfg.get("harness_id") or "",
            # The resolved knobs, so a reader can see what the effort MEANT for this run
            # without having to find the manifest it was taken from.
            "parameters": run_cfg.get("parameters") or {},
            "batch": 8,
            "autoscale_per_tab_mb": args.per_tab_mb,
        },
        "cards": {"interface_first_public_contract": True},
        "parent_id": args.parent_id,
    }
    reason = "%s 2026-06-24 (N=%d)" % (SLICE_LABEL, n)

    print("=== Pro 50 record (%s) ===" % ("COMMIT" if args.commit else "DRY-RUN"))
    print("  graded      : %d / %d resolved" % (k, n))
    print("  pass@1      : %.4f  (95%% CI %s)   [conditional on a gradable attempt]" % (pass_at_1, ci))
    print("  end-to-end  : %.4f  (%d of %d asked for)" % (end_to_end, k, n_all))
    print("  ledger      : %d/%d instances joined (coverage %s)"
          % (join["joined"], join["graded"],
             "n/a" if join["coverage"] is None else "%.0f%%" % (100 * join["coverage"])))
    if excluded:
        print("  excluded    : %d (%s)"
              % (len(excluded),
                 ", ".join("%s=%s" % (i, facts[i]["outcome"]) for i in excluded[:6])
                 + (" ..." if len(excluded) > 6 else "")))
    if join["coverage"] is not None and join["coverage"] < 1.0:
        # NOT a warning to be scrolled past: every unjoined instance is one this run could not
        # tell apart from a real failure, so the number below is a floor on the true rate.
        print("  NOTE        : %d instance(s) had no ledger row and were scored as attempted"
              % len(join["missing"]))
    print("  descriptors : %s" % desc)
    print("  arm         : effort=%s  harness=%s"
          % (genome["knobs"]["effort"], (genome["knobs"]["harness_id"] or "?")[:16]))
    if not run_cfg:
        # LOUD, because an unknown arm is the state in which two efforts get archived as one.
        print("  WARNING     : no run config -- this result records an UNKNOWN arm and cannot "
              "be compared against a labelled one")
    print("  genome.knobs: %s" % genome["knobs"])
    print("  burn reason : %s" % reason)
    # NOT A CHOICE ANYONE MAKES. Two rows from one run are one measurement graded twice (the
    # later corrects the earlier); rows from different runs are different measurements. The id
    # comes from the run itself, so there is nothing to declare and nothing to get wrong.
    print("  row kind    : %s"
          % ("a measurement of run %s -- another run of this scaffold counts toward pass^k"
             % run_cfg["run_id"] if run_cfg.get("run_id")
             else "UNIDENTIFIED RUN -- supersedes any earlier row for this scaffold, so a "
                  "genuine repeat recorded this way would overwrite rather than accumulate"))
    if args.note:
        print("  note        : %s" % args.note)
    # THE CHECKS REFUSE, THEY DO NOT ADVISE.
    #
    # Every safeguard in this program used to be a printed line, and a printed line is not a
    # safeguard: the archive is read later by a dashboard and by an evolution loop, neither of
    # which saw the console. A slice that does not check out, or a ledger that could not be
    # read at all, produces a row that claims to measure something it did not.
    # NOT A REFUSAL, deliberately. A run with no ledger still measures pass@1 honestly: with
    # no outcomes, nothing is excluded and every instance stays in the denominator, which is
    # the conservative direction. What it loses is turns, and what was actually wrong before
    # was that the loss left no trace -- coverage was printed to a console and not archived.
    # It is archived now (measurement.ledger_coverage), so a reader can see that a row's turn
    # counts are zeroes rather than measurements. Blocking here would only stop real results
    # from being recorded.
    if problems:
        print()
        for prob in problems:
            print("  PROBLEM     : %s" % prob)
        if not args.force:
            print("  REFUSED     : not recording a measurement of %r that does not check out."
                  % SLICE_LABEL)
            print("                Fix the run, or pass --force WITH --note saying why this "
                  "row is still worth keeping.")
            return 2
        if not args.note:
            print("  REFUSED     : --force needs --note. A row recorded over a failed check "
                  "with no reason is indistinguishable from one that passed.")
            return 2
        print("  FORCED      : recording anyway -- %s" % args.note)

    if not args.commit:
        print("  (dry-run: nothing written; re-run with --commit)")
        return 0

    arc = Archive()
    eid = arc.add(genome, slice_ids=graded_ids, pass_at_1=pass_at_1, ci=ci,
                  gate_verdict="measured", descriptors=desc, note=args.note,
                  # WHICH instances passed, not only how many. Two rows on one slice at 0.40
                  # may be the same twenty twice or two disjoint twenties, and those are
                  # opposite findings about the same number. A rate cannot be un-summed later.
                  resolved_ids=sorted(set(resolved) & set(graded_ids)),
                  run_id=run_cfg.get("run_id"),
                  # THE QUALIFICATIONS TRAVEL WITH THE NUMBER. These were printed and
                  # discarded, so the archive held a conditional rate presented as a plain
                  # one -- and a run that excluded half its slice was indistinguishable from
                  # one that excluded nothing. Excluding raises `pass_at_1`; only
                  # `end_to_end` cannot be raised that way.
                  measurement={
                      "end_to_end": end_to_end,
                      "asked_for": n_all,
                      "gradable": n,
                      "excluded": {i: facts[i]["outcome"] for i in excluded},
                      "excluded_rate": round(len(excluded) / n_all, 4) if n_all else None,
                      "ledger_coverage": join["coverage"],
                      "ledger_missing": len(join["missing"]),
                      "slice_size": slice_size,
                      # Empty on a clean run. Non-empty means somebody passed --force, and the
                      # row must carry that fact rather than leaving it in a console nobody
                      # kept.
                      "problems": problems,
                      "forced": bool(problems),
                  })
    burned = BurnedRegistry()
    n_new = burned.add(ids, reason)
    out = dashboard.write_json()
    print("  archive id  : %s" % eid)
    print("  burned new  : %d (already-burned skipped)" % n_new)
    print("  dashboard   : %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

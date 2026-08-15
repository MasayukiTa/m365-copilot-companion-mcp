"""Self-improvement loop driver (L1 semi-auto).

Composes the existing hands -- decoupled solve (bench/swe_solve_decoupled.py) and the the eval host grade
(bench/swe_grade_swebench.py) -- under the guardrails in guards.py, to run ONE rigorous VALIDATE
iteration of a scaffold change and emit a keep/revert verdict. It does NOT re-implement solve or
grade; it orchestrates them with the discipline a human applied by hand on 2026-06-21:

  - select a FRESH slice (burned instances excluded)
  - solve ON and OFF arms of the A/B (env-gated change), durably (detached, reaper-proof)
  - grade both arms on the clean host
  - partition infra faults out, apply the McNemar significance gate
  - burn the slice, write a report, recommend keep or revert

This is L1: a human kicks it and reviews the verdict before committing. L2/L3 (cron, auto-commit
behind the gate, held-out rotation) build on this entry point (task #24).

  # dry-run: just select the fresh slice + show the plan, no solving
  python -m relay.selfimprove.loop --n 200 --dry-run
  # full validate of the MISS85 quality-cards change on a fresh N=200 slice
  python -m relay.selfimprove.loop --n 200 --toggle SWE_MISS85_DISCIPLINE --dataset Verified
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SWEDIR = os.path.join(REPO, ".fleet", "swe")
VENVPY = os.path.join(REPO, ".venv", "Scripts", "python.exe")
SOLVER = os.path.join(REPO, "bench", "swe_solve_decoupled.py")
GRADER = os.path.join(REPO, "bench", "swe_grade_swebench.py")
GRADE_RESULTS = os.path.join(SWEDIR, "grade_results.jsonl")

sys.path.insert(0, REPO)
from relay.selfimprove import guards as G

DATASETS = {
    "Verified": "princeton-nlp/SWE-bench_Verified",
    "Lite": "princeton-nlp/SWE-bench_Lite",
}


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def select_fresh_slice(spec_path, n, burned, seed):
    """Pick n instance ids from the spec that are NOT burned. Deterministic (seeded)."""
    import random
    spec = json.load(open(spec_path, encoding="utf-8"))
    ids = sorted(s["instance_id"] for s in spec)
    fresh = burned.filter_fresh(ids)
    if n and n < len(fresh):
        fresh = sorted(random.Random(seed).sample(fresh, n))
    return fresh


def _run_solve_arm(spec_path, targets_file, preds_dir, tag, toggle, on, chunk, conc, turns, floor):
    """Run one solve arm to completion as a BLOCKING child of this driver.

    loop.py is itself the durable, detached parent (launched via Start-Process / launch_detached),
    so the solve orchestrator runs as a normal tracked child here -- the same parent/child shape that
    survived for hours in the manual runs. (An earlier version launched the arm *detached from this
    already-detached driver*; the double-detach orphaned it and it was reaped mid-run.) Returns
    (rc, env_log); rc==0 and a fresh done marker mean the arm finished cleanly.
    """
    env_log = os.path.join(SWEDIR, "solve_decoupled_%s.log" % tag)
    for p in (os.path.join(SWEDIR, "solve_decoupled_%s.lock" % tag),
              os.path.join(SWEDIR, "goals_solve_chunk.jsonl")):
        try:
            os.remove(p)
        except OSError:
            pass
    env = dict(os.environ)
    env["SWE_SIDEPAGE_RESERVE"] = "0"
    if toggle:
        env[toggle] = "1" if on else "0"
    args = [VENVPY, SOLVER, "--spec", spec_path, "--targets-file", targets_file,
            "--preds-dir", preds_dir, "--tag", tag, "--chunk", str(chunk),
            "--max-concurrent", str(conc), "--max-turns", str(turns), "--effort", "auto",
            "--floor-gb", str(floor)]
    r = subprocess.run(args, cwd=REPO, env=env)
    done = G.done_after_last_start(env_log, "decoupled solve start", "solve done/paused")
    return r.returncode, done


def _grade_arm(preds_dir, targets_file, dataset, run_id, max_wait_min=200):
    """Run the the eval host batch grade synchronously.

    Returns (resolved_ids, graded_count, failed_ids, infra_ids) for this run_id:

      resolved_ids  verdict RESOLVED
      failed_ids    verdict 'not' -- a real task failure
      infra_ids     anything else (EVALERR, crash, missing) -- the grader did not judge it
      graded_count  len(resolved) + len(failed), i.e. how many got a REAL verdict

    graded_count == 0 means the grade never actually ran -- the eval host unreachable, scp failed,
    EVALERR-everything -- i.e. an INFRA fault, NOT a real 0%. The caller must distinguish that from a
    genuine low pass rate (which has 'not' verdicts).

    The three SETS matter as much as the counts. A paired test, the sentinel, and any later
    failure mining all need to know WHICH instances, not how many; returning only totals is
    what left the sentinel unevaluable and the archive slice empty.
    """
    args = [VENVPY, GRADER, "--preds-dir", preds_dir, "--targets-file", targets_file,
            "--dataset-name", dataset, "--max-workers", "12", "--run-id", run_id,
            "--max-wait-min", str(max_wait_min)]
    subprocess.run(args, cwd=REPO)
    resolved = set()
    failed = set()
    infra = set()
    graded = 0
    if os.path.isfile(GRADE_RESULTS):
        latest = {}
        for line in open(GRADE_RESULTS, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("runid") == run_id:
                latest[r["instance_id"]] = r["verdict"]
        resolved = {i for i, v in latest.items() if v == "RESOLVED"}
        # a REAL verdict is RESOLVED or 'not'; EVALERR / missing = the grader did not actually judge it.
        failed = {i for i, v in latest.items() if v == "not"}
        # Per-INSTANCE infra, which the arm-level guards below cannot see: an instance the
        # grader could not judge (EVALERR, crash, timeout) is not a task failure, and
        # counting it as one silently depresses the arm it happened to hit. The arm guards
        # catch "the grade never ran at all"; this catches "the grade ran and could not
        # judge these three".
        infra = {i for i, v in latest.items() if v not in ("RESOLVED", "not")}
        graded = len(resolved) + len(failed)

    # A TARGET THE GRADER NEVER MENTIONED. The classification above can only see instances
    # that produced a line, so an instance the grader skipped entirely -- the run died
    # partway, the prediction file was never written, the id was dropped in transit -- was
    # in none of the three sets and vanished from the accounting. It has to land somewhere,
    # and the only honest place is infra: we do not know how it would have gone, and
    # silently shrinking the denominator is how an arm that half-ran looks like an arm that
    # ran. This also covers GRADE_RESULTS being absent, where every target is unjudged.
    try:
        with open(targets_file, encoding="utf-8") as fh:
            targets = {ln.strip() for ln in fh if ln.strip()}
    except OSError:
        targets = set()
    infra |= (targets - resolved - failed - infra)
    return resolved, graded, failed, infra


def validate(toggle, spec_path, n, seed, dataset_key, alpha, min_n, min_pp,
             chunk, conc, turns, floor, dry_run, burned_path):
    burned = G.BurnedRegistry(burned_path) if burned_path else G.BurnedRegistry()
    fresh = select_fresh_slice(spec_path, n, burned, seed)
    targets_file = os.path.join(SWEDIR, "_selfimprove_slice.txt")
    with open(targets_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(fresh) + "\n")
    log("fresh slice: %d instances (burned excluded: %d) -> %s" % (len(fresh), len(burned), targets_file))

    plan = {"toggle": toggle, "n": len(fresh), "dataset": dataset_key, "alpha": alpha,
            "min_n": min_n, "min_pp": min_pp, "targets_file": targets_file}
    if dry_run:
        log("DRY-RUN plan: " + json.dumps(plan, ensure_ascii=False))
        return plan

    dataset = DATASETS[dataset_key]
    on_dir = os.path.join(SWEDIR, "preds_si_on")
    off_dir = os.path.join(SWEDIR, "preds_si_off")
    os.makedirs(on_dir, exist_ok=True)
    os.makedirs(off_dir, exist_ok=True)

    # THESE DIRECTORIES ARE REUSED BETWEEN RUNS, and the "did the solve capture anything"
    # guard below counted every json file in them. A solve that produced nothing therefore
    # passed the guard on the strength of the PREVIOUS run's predictions, which were then
    # graded and reported as this experiment's evidence -- the exact failure the guard was
    # written to prevent, wearing the guard's own uniform. Mark the moment the arm starts
    # and count only what appears after it.
    def _captured(pred_dir, since):
        """Prediction files written by THIS run, not whatever was lying in the directory."""
        if not os.path.isdir(pred_dir):
            return 0
        n = 0
        for name in os.listdir(pred_dir):
            if not name.endswith(".json"):
                continue
            try:
                if os.path.getmtime(os.path.join(pred_dir, name)) >= since:
                    n += 1
            except OSError:
                pass
        return n

    # ON arm (blocking child; resumable so a transient blip just re-runs the uncaptured chunk)
    # ONE TIMESTAMP FOR BOTH ARMS was not enough: a file touched in the OFF directory while
    # the ON arm was still running predates the OFF arm and still counted as an OFF capture.
    # Each arm is marked when it starts.
    on_started_at = time.time()
    log("solve ON (%s=1) ..." % toggle)
    rc, done = _run_solve_arm(spec_path, targets_file, on_dir, "sion", toggle, True, chunk, conc, turns, floor)
    if not done:
        log("ON solve did not reach its done marker (rc=%s); aborting" % rc); return None
    # INFRA-ABORT GUARD: the orchestrator writes "solve done/paused" even when it aborts a chunk on
    # the disk floor or a wedge, so `done` is not enough. If nothing was captured, the solve never
    # actually ran -- this is an INFRA fault, not a measurement. Do NOT grade, gate, or burn (burning
    # a slice that was never solved would silently consume fresh instances; cf. the disk-floor
    # incident that wrongly burned 200). Return an infra_abort status so the caller retries later.
    on_cap = _captured(on_dir, on_started_at)
    if on_cap == 0:
        log("ON solve captured 0 predictions -> INFRA ABORT (disk floor / wedge); NOT burning, NOT gating")
        return {"status": "infra_abort", "arm": "ON", "reason": "ON solve produced no predictions (infra)",
                "burned": False, "report": None}
    on_resolved, on_graded, on_failed, on_infra = _grade_arm(
        on_dir, targets_file, dataset, "sion" + time.strftime("%m%d%H%M"))
    log("ON resolved: %d/%d (graded %d)" % (len(on_resolved), len(fresh), on_graded))
    # GRADE-INFRA GUARD: 0 real verdicts means the grade never ran (the eval host down / scp failed / EVALERR-
    # everything) -- NOT a real 0%. Gating/burning on a failed grade would record a garbage A/B and burn
    # the slice (the the eval host-down incident: ON solved 200 fine but the grade returned 0/200). Treat as infra.
    if on_graded == 0:
        log("ON grade produced 0 real verdicts -> INFRA ABORT (the eval host unreachable / scp failed); NOT gating, NOT burning")
        return {"status": "infra_abort", "arm": "ON-grade",
                "reason": "ON grade did not run (infra: the eval host unreachable / scp failed)",
                "burned": False, "report": None}

    # OFF arm
    off_started_at = time.time()
    log("solve OFF (%s=0) ..." % toggle)
    rc, done = _run_solve_arm(spec_path, targets_file, off_dir, "sioff", toggle, False, chunk, conc, turns, floor)
    if not done:
        log("OFF solve did not reach its done marker (rc=%s); aborting" % rc); return None
    off_cap = _captured(off_dir, off_started_at)
    if off_cap == 0:
        log("OFF solve captured 0 predictions -> INFRA ABORT; NOT burning, NOT gating")
        return {"status": "infra_abort", "arm": "OFF", "reason": "OFF solve produced no predictions (infra)",
                "burned": False, "report": None}
    off_resolved, off_graded, off_failed, off_infra = _grade_arm(
        off_dir, targets_file, dataset, "sioff" + time.strftime("%m%d%H%M"))
    log("OFF resolved: %d/%d (graded %d)" % (len(off_resolved), len(fresh), off_graded))
    if off_graded == 0:
        log("OFF grade produced 0 real verdicts -> INFRA ABORT (the eval host unreachable / scp failed); NOT gating, NOT burning")
        return {"status": "infra_abort", "arm": "OFF-grade",
                "reason": "OFF grade did not run (infra: the eval host unreachable / scp failed)",
                "burned": False, "report": None}

    # THE GATE WAS STILL HANDED THE WHOLE SLICE. Per-instance infra was being classified
    # correctly above and then discarded here: an instance the grader could not judge on
    # either arm entered McNemar as "not resolved on that arm", which is evidence of failure
    # rather than absence of evidence. Half the previous fix, undone one line later. The
    # paired set is the slice minus anything either arm could not judge.
    infra_either = set(on_infra) | set(off_infra)
    paired = [i for i in fresh if i not in infra_either]
    if len(paired) < len(fresh):
        log("excluded %d instance(s) from the pair: ungradable on at least one arm"
            % (len(fresh) - len(paired)))
    gate = G.significance_gate(on_resolved, off_resolved, paired,
                               alpha=alpha, min_n=min_n, min_pp=min_pp)
    burned.add(fresh, reason="selfimprove A/B %s" % toggle, ts=int(time.time()))
    # PER-INSTANCE, not just counts. The sets were already computed above and then thrown
    # away, which is why the sentinel had nothing to check, the archive recorded an empty
    # slice, and no paired test was possible: every one of those needs identity, not a
    # total. Counts stay for back-compat with existing readers.
    report = {
        **plan,
        "on_resolved": len(on_resolved), "off_resolved": len(off_resolved), "gate": gate,
        "slice_ids": list(fresh),
        "on": {"resolved_ids": sorted(on_resolved), "failed_ids": sorted(on_failed),
               "infra_ids": sorted(on_infra)},
        "off": {"resolved_ids": sorted(off_resolved), "failed_ids": sorted(off_failed),
                "infra_ids": sorted(off_infra)},
    }
    out = os.path.join(SWEDIR, "selfimprove_report_%s.json" % time.strftime("%m%d%H%M"))
    json.dump(report, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    log("GATE verdict=%s keep=%s | %s" % (gate["verdict"], gate["keep"], gate["reason"]))
    log("report: %s ; slice burned (%d)" % (out, len(fresh)))
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--toggle", default="SWE_MISS85_DISCIPLINE", help="env var that turns the change on/off")
    ap.add_argument("--spec", default=os.path.join(SWEDIR, "verified_fresh_spec.json"))
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20260621)
    ap.add_argument("--dataset", default="Verified", choices=list(DATASETS))
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--min-n", type=int, default=100)
    ap.add_argument("--min-pp", type=float, default=1.0)
    ap.add_argument("--chunk", type=int, default=20)
    ap.add_argument("--max-concurrent", type=int, default=3)
    ap.add_argument("--max-turns", type=int, default=50)
    ap.add_argument("--floor-gb", type=float, default=7.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--burned", default="", help="burned registry path (default: module default)")
    a = ap.parse_args()
    validate(a.toggle, a.spec, a.n, a.seed, a.dataset, a.alpha, a.min_n, a.min_pp,
             a.chunk, a.max_concurrent, a.max_turns, a.floor_gb, a.dry_run, a.burned or None)


if __name__ == "__main__":
    main()

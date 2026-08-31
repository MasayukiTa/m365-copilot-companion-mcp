"""Stage, run, capture, GRADE, discard -- one small batch at a time.

WHAT THIS ADDS TO pro_run_50.py, WHICH ALREADY BATCHED. That driver stages a batch, runs it,
captures the diffs and deletes the worktrees, which is the right shape and is not what went
wrong. Two things were missing, and both showed up as disk:

  1. GRADING WAS DEFERRED. Predictions accumulated for the whole run and were graded afterwards,
     so a run had to survive to the end before it was worth anything. When one froze on
     2026-08-31 -- coordinator pid 22884, stuck holding its worktrees -- the batch that never
     reached capture kept 971 MB and the free space fell to 2.10 GB, under the 3.0 GB floor
     that then refuses to start the next run. Nothing was graded and nothing could restart.

  2. NOTHING WATCHED THE DISK. The floor is checked when a run starts, not between batches, so
     a run could walk itself into a state where it could not be resumed.

Grading each batch as it lands means a stop at any point leaves everything before it already
counted, and the store stays flat: one batch of worktrees exists at a time and is deleted
before the next is staged.

    python -m bench.pro_cycle --batch 4                 # every ungraded instance in the slice
    python -m bench.pro_cycle --batch 4 --limit 12      # just the next 12
    python -m bench.pro_cycle --dry-run                 # what it would do, touching nothing

RESUMABLE BY CONSTRUCTION. The results file is the ledger: an instance already in it is skipped,
so re-running after a stop continues rather than repeats. That also means a re-run cannot
silently re-measure an instance that has already been counted, which is the mistake that makes
a benchmark number drift upward without anybody deciding to cheat.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SW = os.path.join(REPO, ".fleet", "swe")
PY = os.path.join(REPO, ".venv", "Scripts", "python.exe")
if not os.path.isfile(PY):
    PY = sys.executable

LOG = os.path.join(SW, "pro_cycle.log")
PREDS = os.path.join(SW, "pro_cycle_preds.json")
RESULTS = os.path.join(SW, "pro_cycle_results.json")
GOALS = os.path.join(SW, "pro_cycle_goals.jsonl")
STATUS = os.path.join(REPO, ".fleet", "status.json")

#: Stop before the run's own floor does. fleet_runner refuses to start under 3.0 GB, so a cycle
#: that keeps going until it hits that leaves the operator with a benchmark that cannot resume
#: and a disk they have to clear by hand. Stopping a batch early is recoverable; stopping
#: mid-batch with worktrees still on disk is what happened last time.
DISK_FLOOR_GB = float(os.environ.get("SWE_CYCLE_FLOOR_GB", "3.4"))

#: A batch that has not finished in this long is not going to. The point of small batches is
#: that giving up on one is cheap.
BATCH_TIMEOUT_S = float(os.environ.get("SWE_CYCLE_BATCH_TIMEOUT_S", "3600"))


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def free_gb(path=None):
    return shutil.disk_usage(path or os.path.splitdrive(REPO)[0] + os.sep).free / 1e9


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def graded_ids():
    """Instances already counted. THE RESULTS FILE IS THE LEDGER -- see the module note on why
    re-measuring silently is worse than stopping."""
    data = _load(RESULTS, {})
    if isinstance(data, dict):
        return set(data.keys())
    if isinstance(data, list):
        return {r.get("instance_id") for r in data if isinstance(r, dict)}
    return set()


def slice_ids():
    from bench import pro_stage_goals as G
    return sorted(G.BY_ID)


def burned_ids():
    """Instances already used in a measured run. Never raises; an unreadable registry returns
    nothing, and the caller treats that as "cannot prove it is fresh"."""
    ids = set()
    path = os.path.join(REPO, "relay", "selfimprove", "burned.jsonl")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                for key in ("instance_id", "id", "instance"):
                    if row.get(key):
                        ids.add(row[key])
    except OSError:
        pass
    return ids


def check_slice_is_fresh(ids, allow_burned=False):
    """Refuse to measure instances that have already been measured. Returns the ids to run.

    MEASURED BEFORE THE FIRST CYCLE RAN: the DEFAULT slice is
    .fleet/swe/pro_slice50_full.json and all fifty of its instances are in the burned
    registry. A run started without SWE_SLICE_FILE would have re-measured every one of them
    and produced a number that had already seen its own answers -- which is the one thing a
    benchmark must never do quietly, and the rule this repository already holds
    (feedback_no_benchmark_overfitting).

    Fail closed: burned instances are dropped, and if that leaves nothing the cycle stops and
    says which slice to point at. --allow-burned is for a deliberate re-measure, and says so
    in the log rather than in somebody's memory.
    """
    burned = burned_ids()
    fresh = [i for i in ids if i not in burned]
    reused = len(ids) - len(fresh)
    if not reused:
        return ids
    if allow_burned:
        log("WARNING: %d of %d instances are BURNED and are being re-measured on purpose "
            "(--allow-burned). This number cannot be reported as a fresh result."
            % (reused, len(ids)))
        return ids
    log("%d of %d instances are already burned and were dropped" % (reused, len(ids)))
    if not fresh:
        log("NOTHING FRESH IN THIS SLICE. The default is pro_slice50_full.json, every instance "
            "of which has been measured before. Point at a fresh draw, e.g.")
        log("    set SWE_SLICE_FILE=.fleet/swe/pro_slice40_fresh.json")
        log("or pass --allow-burned if a deliberate re-measure is what you want.")
    return fresh


def batches(ids, size):
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def run(cmd, timeout, label):
    """Run one step. Returns (ok, tail-of-output). Never raises."""
    log("  $ %s" % " ".join(cmd[1:] if cmd and cmd[0] == PY else cmd))
    try:
        p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        log("  %s TIMED OUT after %.0fs" % (label, timeout))
        return False, "timeout"
    tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
    for ln in tail[-6:]:
        log("    | " + ln[:160])
    return p.returncode == 0, "\n".join(tail[-6:])


def worktrees_present():
    work = os.path.join(SW, "work")
    if not os.path.isdir(work):
        return 0, 0.0
    total = 0
    for dirpath, _dirs, files in os.walk(work):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return len(os.listdir(work)), total / 1e6


def cycle(batch_size, limit=None, dry_run=False, effort="auto", allow_burned=False):
    todo = check_slice_is_fresh(slice_ids(), allow_burned)
    todo = [i for i in todo if i not in graded_ids()]
    if limit:
        todo = todo[:limit]
    log("=" * 72)
    log("cycle start: %d instance(s) to do, batch=%d, free=%.2f GB, floor=%.2f GB"
        % (len(todo), batch_size, free_gb(), DISK_FLOOR_GB))
    if not todo:
        log("nothing ungraded in the slice -- done")
        return 0

    done = 0
    for n, group in enumerate(batches(todo, batch_size), start=1):
        have = free_gb()
        if have < DISK_FLOOR_GB:
            log("STOP before batch %d: %.2f GB free is under the %.2f GB floor. "
                "Everything graded so far is recorded; re-run to continue."
                % (n, have, DISK_FLOOR_GB))
            break
        log("-" * 72)
        log("batch %d: %s  (free %.2f GB)" % (n, ", ".join(x[:40] for x in group), have))
        if dry_run:
            done += len(group)
            continue

        ok, _ = run([PY, os.path.join("bench", "pro_stage_goals.py"),
                     "--ids", ",".join(group), "--out", GOALS], 1800, "stage")
        if not ok:
            log("  staging failed -- skipping this batch, nothing left behind")
            _discard()
            continue

        # THE TERMS, RECORDED BEFORE THE WORKER'S FIRST TURN, BY THE CONTROLLER.
        #
        # Until now the only thing between a worker and a DONE was its own judgement that it
        # had finished -- measured at 0.718 precision, 11 of 39 claims wrong. A worker that
        # picks its own acceptance test after the fact picks one it passes.
        #
        # The command written here is the repository's OWN test command, which the goal text
        # already tells the worker to run. It is not the hidden acceptance test and cannot
        # leak it: those are graded offline and this process never sees them.
        _write_contracts(group)

        run([PY, "-m", "relay.fleet_runner", "--goals-file", GOALS,
             "--effort", effort, "--max-concurrent", str(min(4, batch_size))],
            BATCH_TIMEOUT_S, "fleet")

        # CAPTURE BEFORE ANYTHING ELSE, and unconditionally. A fleet that timed out still has
        # work on disk worth diffing, and the frozen run of 2026-08-31 lost a batch precisely
        # because capture was downstream of a clean finish.
        run([PY, os.path.join("bench", "pro_capture.py"), "--preds", PREDS], 900, "capture")

        # SHADOW. The records are compared against what each worker claimed, and the verdict is
        # written down beside the reported outcome. Nothing is gated on it: switching a gate
        # from permissive to closed without measuring first is a mistake this repository has
        # already been corrected for, and the number this produces is exactly what says whether
        # gating would help.
        _shadow_assess(group)

        graded_before = len(graded_ids())
        run([PY, os.path.join("bench", "swe_grade_batch.py"),
             "--instances"] + group, BATCH_TIMEOUT_S, "grade")
        gained = len(graded_ids()) - graded_before
        log("  graded this batch: %d of %d" % (gained, len(group)))

        _discard()
        done += len(group)
        log("  after batch %d: free %.2f GB, %d worktree dir(s) left" % (n, free_gb(), worktrees_present()[0]))

    log("cycle end: %d instance(s) attempted, %d graded in total, free %.2f GB"
        % (done, len(graded_ids()), free_gb()))
    return 0


def _write_contracts(group):
    """One acceptance contract per instance, at admission. Never raises.

    A missing contract is REPORTED rather than swallowed: "nobody wrote a check" and "this task
    has no mechanical oracle" look identical at the end of a run, and only one of them is a
    problem. Saying it here, at admission, is the whole point of writing them first.
    """
    try:
        from relay import acceptance_contract as AC
        from bench import pro_stage_goals as G
        wrote = 0
        for inst in group:
            row = G.BY_ID.get(inst) or {}
            hint = G.TESTHINT.get(row.get("repo_language") or "")
            checks = [{"id": "project_tests", "command": hint}] if hint else []
            AC.ensure(inst, goal=(row.get("problem_statement") or ""), checks=checks,
                      cwd=G.wt_for(inst))
            wrote += 1
        missing = AC.missing_contract_tasks(group)
        log("  contracts: %d written, %d missing" % (wrote, len(missing)))
        if missing:
            log("  NO CONTRACT for: %s -- these cannot be verified mechanically and must not "
                "be counted as verified" % ", ".join(m[:40] for m in missing))
    except Exception as exc:
        log("  contract step failed (%s: %s) -- the batch still runs, but nothing in it can "
            "be promoted past a self-report" % (type(exc).__name__, str(exc)[:120]))


SHADOW = os.path.join(SW, "pro_cycle_shadow.jsonl")


def _shadow_assess(group):
    """Compare each worker's DONE against the ledger, and write the verdict down. Never raises.

    Reads three things that now exist: the claim (the run's own outcome), the contract written
    at admission, and the tool events. Until today only the first existed, which is why the
    refuter judged hearsay and precision sat at 0.718.
    """
    try:
        import json as _json
        from relay import acceptance_contract as AC
        from relay import evidence_manifest as EM
        from tools import tool_ledger as TL

        # WHICH WORKER WAS THIS INSTANCE. Matched on the worktree path, which the goal text
        # carries verbatim -- not guessed from ordering, which changes between runs and has
        # already caused one instance's reads to be attributed to another.
        from bench import pro_stage_goals as G
        status = _load(STATUS, {})
        claim_by_inst = {}
        for w in status.get("workers") or []:
            goal = str(w.get("goal") or "")
            for inst in group:
                wt = G.wt_for(inst).replace("\\", "/")
                if wt and (wt in goal.replace("\\", "/")):
                    claim_by_inst[inst] = str(w.get("outcome") or "")

        rows, verdicts = [], []
        for inst in group:
            events = TL.for_task(inst)
            contract = AC.load(inst)
            # NO CLAIM FOUND IS NOT A CLAIM OF SUCCESS. If the run recorded nothing for this
            # instance, there is nothing to check and the verdict says so, rather than
            # inventing a DONE from the presence of tool calls.
            claimed = claim_by_inst.get(inst, "") == "DONE"
            v = EM.assess(claimed, contract, events)
            verdicts.append(v)
            rows.append({"ts": time.time(), "instance": inst, "claimed_done": bool(claimed),
                         "verdict": v.get("verdict"), "reasons": v.get("reasons"),
                         "evidence": v.get("evidence")})
        with open(SHADOW, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(_json.dumps(r, ensure_ascii=False) + "\n")
        s = EM.summarise(verdicts)
        log("  shadow: supported=%d contradicted=%d unverifiable=%d (share=%s)"
            % (s[EM.SUPPORTED], s[EM.CONTRADICTED], s[EM.UNVERIFIABLE],
               ("%.2f" % s["supported_share"]) if s["supported_share"] is not None else "n/a"))
        if s[EM.CONTRADICTED]:
            for r in rows:
                if r["verdict"] == EM.CONTRADICTED:
                    log("    CONTRADICTED %s: %s" % (r["instance"][:40],
                                                     "; ".join(r["reasons"])[:120]))
    except Exception as exc:
        log("  shadow assessment failed (%s: %s) -- grading is unaffected"
            % (type(exc).__name__, str(exc)[:120]))


def _discard():
    """Delete the batch's worktrees. THE POINT OF THE WHOLE SHAPE: one batch on disk at a time.

    pro_capture.py already deletes what it captured; this is the sweep for what it did not --
    a staging that failed halfway, a directory a timed-out worker still held. Shallow clones
    are regenerable from pro_stage_goals, so nothing here is irreplaceable.
    """
    work = os.path.join(SW, "work")
    if not os.path.isdir(work):
        return
    for name in os.listdir(work):
        path = os.path.join(work, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bench.pro_cycle", description=__doc__.splitlines()[0])
    ap.add_argument("--batch", type=int, default=4,
                    help="instances per cycle (default 4: one round is short enough to watch)")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many instances")
    ap.add_argument("--effort", default=os.environ.get("SWE_EFFORT", "auto"))
    ap.add_argument("--dry-run", action="store_true",
                    help="print the batches and touch nothing")
    ap.add_argument("--allow-burned", action="store_true",
                    help="re-measure instances that have already been used; the result is not "
                         "a fresh number and the log says so")
    a = ap.parse_args(argv)
    os.makedirs(SW, exist_ok=True)
    return cycle(a.batch, a.limit or None, a.dry_run, a.effort, a.allow_burned)


if __name__ == "__main__":
    raise SystemExit(main())

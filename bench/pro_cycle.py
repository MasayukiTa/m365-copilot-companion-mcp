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
import stat
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
    """Never raises. A LOGGING CALL TOOK THE WHOLE CYCLE DOWN once already: this console is
    cp932, a grader printed a replacement character, and print() died with UnicodeEncodeError
    eight minutes into a forty-instance run. A line of output is not worth a run."""
    line = time.strftime("%H:%M:%S ") + str(msg)
    try:
        print(line, flush=True)
    except Exception:
        try:
            enc = (getattr(sys.stdout, "encoding", None) or "utf-8")
            print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)
        except Exception:
            pass
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


def captured_ids():
    """Instances whose PATCH is already on disk. The expensive half is finished for these.

    GRADING IS A SEPARATE, OFFLINE STEP, and it can be unavailable for reasons that have
    nothing to do with the work: the eval host's docker was down for this entire run, so every
    batch returned EVALERR and graded_ids() stayed empty. A restart then re-ran instances whose
    patch was already captured -- eight of them, about eighty minutes of the tenant quota that
    is the binding constraint here, thrown away to reproduce a file that already existed.

    An empty patch does NOT count. That is an instance that ran and produced nothing, which is
    a result worth retrying, not a result worth keeping.
    """
    out = set()
    for row in _load(PREDS, []) or []:
        if not isinstance(row, dict):
            continue
        inst = row.get("instance_id")
        patch = (row.get("patch") or row.get("model_patch") or "").strip()
        if inst and patch:
            out.add(inst)
    return out


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


#: What one instance of each language costs on disk once its dependencies are installed.
#: MEASURED, not guessed: a staged checkout is 19-22 MB whatever the language, and the cost
#: that matters arrives later -- a NodeBB worktree reached 564 MB after npm install, an
#: ansible one stayed small. Disk is the binding constraint on this machine, so this table is
#: what decides how many can run at once.
#: go was 200 here and that was WRONG, measured the wrong thing. 200 MB is what a go worktree
#: weighs; the cost that matters is the module cache, which `go test ./...` fills OUTSIDE the
#: worktree in ~/go/pkg/mod, where the per-batch discard cannot see it. Three go instances put
#: 2.01 GB there in ninety minutes and drove the run into the disk floor. ~670 MB each,
#: rounded up because instances from different repositories share almost nothing.
LANG_DISK_MB = {"js": 560, "ts": 560, "python": 120, "go": 700}

#: Caches that language toolchains fill OUTSIDE the worktree, so per-batch discard never sees
#: them and they grow across the whole run. Cleared only as a last resort -- see _reclaim.
def _clear_toolchain_caches():
    """Empty the module/package caches that live outside the worktrees. Returns [(name, mb)].

    ONLY CALLED WHEN THE RUN IS ABOUT TO STOP for lack of disk, because everything here is
    re-downloaded afterwards: clearing it between every batch would trade the disk problem for
    a network one. As a last resort before stopping it is plainly worth it -- it recovered
    2.05 GB and let a stalled run continue.
    """
    import shutil as _sh
    freed = []
    go = _sh.which("go") or r"C:\Program Files\Goin\go.exe"
    if os.path.exists(go):
        before = free_gb()
        try:
            subprocess.run([go, "clean", "-modcache"], capture_output=True, timeout=900)
            freed.append(("go module cache", (free_gb() - before) * 1000))
        except Exception:
            pass
    npm = _sh.which("npm")
    if npm:
        before = free_gb()
        try:
            subprocess.run([npm, "cache", "clean", "--force"], capture_output=True,
                           timeout=900, shell=True)
            freed.append(("npm cache", (free_gb() - before) * 1000))
        except Exception:
            pass
    return freed
DEFAULT_DISK_MB = 300


def lang_of(inst):
    try:
        from bench import pro_stage_goals as G
        return (G.BY_ID.get(inst) or {}).get("repo_language") or ""
    except Exception:
        return ""


def concurrency_for(langs, free):
    """How many of these may run together without crossing the fleet's own 3.0 GB floor.

    The fleet refuses to START under 3.0 GB, so the question is not "does it fit" but "does
    what remains still admit a run". One at a time is the floor of this function, because a
    batch of zero makes no progress and lowering the disk floor to force one through is the
    thing this repository has a standing rule against.
    """
    cost = max([LANG_DISK_MB.get(l, DEFAULT_DISK_MB) for l in langs] or [DEFAULT_DISK_MB])
    headroom_mb = max(0.0, (free - 3.05) * 1000.0)
    return max(1, min(4, int(headroom_mb // cost)))


def batches(ids, size):
    """Group by language, heaviest last, so cheap instances can run several at a time.

    THE SLICE IS NOT UNIFORM. Of the fresh forty: 16 python, 11 go, 11 js, 2 ts. Only the
    js/ts ones carry a node_modules, so batching in slice order forced every instance down to
    the pace the heaviest one sets. Grouping by language lets the 27 cheap ones run in
    parallel and keeps the expensive ones serial, with no extra disk.
    """
    by_lang = {}
    for i in ids:
        by_lang.setdefault(lang_of(i), []).append(i)
    order = sorted(by_lang, key=lambda l: LANG_DISK_MB.get(l, DEFAULT_DISK_MB))
    for lang in order:
        group = by_lang[lang]
        width = size if size else concurrency_for([lang], free_gb())
        for i in range(0, len(group), width):
            yield group[i:i + width]


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


def cycle(batch_size, limit=None, dry_run=False, effort="auto", allow_burned=False,
          redo_captured=False):
    todo = check_slice_is_fresh(slice_ids(), allow_burned)
    done_already = graded_ids() | (set() if redo_captured else captured_ids())
    todo = [i for i in todo if i not in done_already]
    if limit:
        todo = todo[:limit]
    log("=" * 72)
    log("cycle start: %d instance(s) to do, batch=%s, free=%.2f GB, floor=%.2f GB"
        % (len(todo), batch_size or "by language", free_gb(), DISK_FLOOR_GB))
    held = len(captured_ids())
    if held and not redo_captured:
        log("%d instance(s) already have a captured patch and are skipped; grade them with "
            "bench.swe_grade_batch rather than re-running them" % held)
    if not todo:
        log("nothing ungraded in the slice -- done")
        return 0

    done = 0
    for n, group in enumerate(batches(todo, batch_size), start=1):
        have = free_gb()
        if have < DISK_FLOOR_GB:
            # FREE SPACE BEFORE GIVING UP, NEVER LOWER THE FLOOR. The idle Edge profiles this
            # project drives accumulate regenerable cache -- 609 MB in one measured run, which
            # is more than two batches' worth. Reclaiming it is the sanctioned move; moving the
            # line is the forbidden one, and it is why this reads the floor twice instead of
            # relaxing it once.
            have = _reclaim(have)
        if have < DISK_FLOOR_GB:
            log("STOP before batch %d: %.2f GB free is under the %.2f GB floor. "
                "Everything graded so far is recorded; re-run to continue."
                % (n, have, DISK_FLOOR_GB))
            break
        log("-" * 72)
        log("batch %d: %d x %s  (free %.2f GB)  %s"
            % (n, len(group), lang_of(group[0]) or "?", have,
               ", ".join(x[:36] for x in group)))
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

        # ONE WORKER PER INSTANCE IN THE BATCH, and no more. The batch was already sized
        # against the disk; letting the fleet open more tabs than there are instances would
        # spend RAM and admission slots on nothing, and every extra concurrent worker makes
        # the shared tool-planner limiter more likely to refuse -- measured, median 35
        # concurrent replies at a refusal against 5 at a recovery.
        run([PY, "-m", "relay.fleet_runner", "--goals-file", GOALS,
             "--effort", effort, "--max-concurrent", str(len(group))],
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

        # STEP 4, ALSO SHADOW. Step 3 reads what the worker happened to run; this runs the
        # contract's own commands here, after the worker stopped, against a tree it hashed
        # before and after. It is the only place DONE is produced -- and while it is in shadow
        # it produces it into a file, not into the run's outcome.
        _shadow_verify(group)

        graded_before = len(graded_ids())
        # THE TWO HALVES SPEAK DIFFERENT SHAPES, and I wired them together without reading
        # either. pro_capture.py writes ONE json file holding a list of {instance_id, patch};
        # swe_grade_batch.py reads a DIRECTORY of <instance_id>.json each holding
        # [{"model_patch": ...}]. The first run died on that. Converting here keeps both
        # scripts untouched -- they each have other callers.
        preds_dir = _explode_preds()
        if preds_dir:
            run([PY, os.path.join("bench", "swe_grade_batch.py"),
                 "--preds-dir", preds_dir, "--results", RESULTS,
                 "--instances"] + group, BATCH_TIMEOUT_S, "grade")
        else:
            log("  no patch captured for this batch -- nothing to grade")
        # COUNTED AGAINST THIS BATCH, not against the whole ledger. The first version
        # subtracted two totals and printed "graded 4 of 1", which is not a number.
        after = graded_ids()
        gained = len([i for i in group if i in after])
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
            events = TL.for_task(inst, root=G.wt_for(inst))
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
        # HANDED TO THE VERIFIER, not just written down. The two assessments were being made
        # independently and reported side by side, so one log line said CONTRADICTED and the
        # next said CANDIDATE_DONE about the same task. Where the acceptance command cannot be
        # run at all -- the normal case in a staged worktree with no dependencies installed --
        # the ledger is the only signal there is, and it was being discarded.
        _EVIDENCE_BY_INSTANCE.clear()
        for r in rows:
            _EVIDENCE_BY_INSTANCE[r["instance"]] = {"verdict": r["verdict"],
                                                    "reasons": r.get("reasons")}
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


VERIFY = os.path.join(SW, "pro_cycle_verify.jsonl")

#: The evidence assessment for the batch being processed, so the verifier can consult it
#: where it could not run anything itself. Refilled per batch; never read across batches.
_EVIDENCE_BY_INSTANCE = {}


def _shadow_verify(group):
    """Run each instance's acceptance commands ourselves and record what they said."""
    try:
        import json as _json
        from relay import acceptance_contract as AC
        from relay import supervisor_verify as SV
        from bench import pro_stage_goals as G

        status = _load(STATUS, {})
        claimed = set()
        for w in status.get("workers") or []:
            goal = str(w.get("goal") or "").replace("\\", "/")
            if (w.get("outcome") or "") != "DONE":
                continue
            for inst in group:
                if G.wt_for(inst).replace("\\", "/") in goal:
                    claimed.add(inst)

        counts = {}
        with open(VERIFY, "a", encoding="utf-8") as fh:
            for inst in group:
                contract = AC.load(inst)
                cwd = G.wt_for(inst)
                v = SV.verify(contract, cwd=cwd)
                state = SV.promote(inst in claimed, v, _EVIDENCE_BY_INSTANCE.get(inst)) or "NO_CLAIM"
                counts[state] = counts.get(state, 0) + 1
                fh.write(_json.dumps({
                    "ts": time.time(), "instance": inst, "claimed_done": inst in claimed,
                    "state": state, "verify_state": v.get("state"),
                    "evidence_verdict": (_EVIDENCE_BY_INSTANCE.get(inst) or {}).get("verdict"),
                    "reasons": v.get("reasons"),
                    "tree_stable": bool(v.get("tree_before")) and
                                   v.get("tree_before") == v.get("tree_after"),
                    # THE EVIDENCE, NOT JUST THE VERDICT. This record used to carry id, ok and
                    # duration and nothing else, so "project_tests failed in 3.4s" was the
                    # entire account -- and the reason it failed (the project's dependencies
                    # were not installed, so collection died and no test ran) was invisible for
                    # a whole run. A record that states a failure without what it saw cannot be
                    # checked, which is the same defect this pipeline exists to fix in workers.
                    "checks": [{"id": c.get("id"), "ok": c.get("ok"),
                                "duration_s": c.get("duration_s"),
                                "unavailable": bool(c.get("unavailable")),
                                "why_unavailable": c.get("why_unavailable", ""),
                                "output_tail": (c.get("output") or "")[-600:]}
                               for c in v.get("checks") or []],
                }, ensure_ascii=False) + "\n")
        log("  verify(shadow): " + ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
    except Exception as exc:
        log("  shadow verification failed (%s: %s) -- grading is unaffected"
            % (type(exc).__name__, str(exc)[:120]))


def _explode_preds():
    """Turn the single preds file into the per-instance directory the grader expects.

    Returns the directory, or "" when there is nothing to grade. Never raises: a conversion
    failure must leave the cycle running, because the capture it depends on has already
    happened and the patches are on disk either way.
    """
    try:
        rows = _load(PREDS, [])
        if isinstance(rows, dict):
            rows = list(rows.values())
        out = os.path.join(SW, "pro_cycle_preds_dir")
        os.makedirs(out, exist_ok=True)
        n = 0
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            inst = row.get("instance_id")
            patch = row.get("patch") or row.get("model_patch") or ""
            if not inst:
                continue
            with open(os.path.join(out, "%s.json" % inst), "w", encoding="utf-8") as fh:
                json.dump([{"instance_id": inst, "model_patch": patch}], fh, ensure_ascii=False)
            n += 1
        return out if n else ""
    except Exception as exc:
        log("  could not prepare predictions for grading (%s)" % type(exc).__name__)
        return ""


def _reclaim(have):
    """Give back what can be regenerated, and say what it came to. Returns the new free GB.

    Only the caches of the project's OWN Edge profiles, and only ones no browser is using --
    the guards live in edge_recover.trim_profile_caches, which refuses anything else including
    the user's own browser. Never raises: a benchmark must not die trying to make room.
    """
    try:
        from relay.edge_recover import trim_profile_caches
        freed, notes = trim_profile_caches()
    except Exception as exc:
        log("  could not reclaim disk (%s)" % type(exc).__name__)
        return have
    for note in notes:
        log("  reclaim: %s" % note)
    if free_gb() < DISK_FLOOR_GB:
        # STILL SHORT. The toolchain caches are the last thing to give back, because they cost
        # a re-download to rebuild -- but a stopped run costs more.
        for name, mb in _clear_toolchain_caches():
            log("  reclaim: %s freed %.0f MB" % (name, mb))
    now = free_gb()
    log("  reclaimed %.0f MB: %.2f -> %.2f GB free" % (freed, have, now))
    return now


def _discard():
    """Delete the batch's worktrees. THE POINT OF THE WHOLE SHAPE: one batch on disk at a time.

    pro_capture.py already deletes what it captured; this is the sweep for what it did not --
    a staging that failed halfway, a directory a timed-out worker still held. Shallow clones
    are regenerable from pro_stage_goals, so nothing here is irreplaceable.
    """
    work = os.path.join(SW, "work")
    if not os.path.isdir(work):
        return
    left = []
    for name in os.listdir(work):
        path = os.path.join(work, name)
        if not os.path.isdir(path):
            continue
        # ignore_errors=True WAS HIDING A REAL FAILURE. Windows marks the files under .git
        # read-only, and rmtree cannot unlink those; with errors ignored, each batch left 4-8 MB
        # behind and reported nothing. Eight batches in, eight directories were still there and
        # the log had never once mentioned it. Clear the bit and retry, then SAY what survived.
        shutil.rmtree(path, onerror=_force_writable)
        if os.path.isdir(path):
            left.append(name)
    if left:
        log("  could not delete %d worktree dir(s): %s" % (len(left), ", ".join(sorted(left))))


def _force_writable(func, path, _exc):
    """rmtree's onerror: drop the read-only bit and retry once. A file still held open by a
    process survives this, which is the case worth reporting rather than ignoring."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bench.pro_cycle", description=__doc__.splitlines()[0])
    ap.add_argument("--batch", type=int, default=0,
                    help="instances per batch; 0 (default) sizes each batch from the language's "
                         "measured disk cost and the free space, so cheap instances run "
                         "several at a time and heavy ones stay serial")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many instances")
    ap.add_argument("--effort", default=os.environ.get("SWE_EFFORT", "auto"))
    ap.add_argument("--dry-run", action="store_true",
                    help="print the batches and touch nothing")
    ap.add_argument("--allow-burned", action="store_true",
                    help="re-measure instances that have already been used; the result is not "
                         "a fresh number and the log says so")
    ap.add_argument("--redo-captured", action="store_true",
                    help="re-run instances whose patch is already captured; the default skips "
                         "them, because grading being down is not a reason to redo the work")
    a = ap.parse_args(argv)
    os.makedirs(SW, exist_ok=True)
    return cycle(a.batch, a.limit or None, a.dry_run, a.effort, a.allow_burned,
                 a.redo_captured)


if __name__ == "__main__":
    raise SystemExit(main())

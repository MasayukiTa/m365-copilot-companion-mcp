"""Disk-safe batched SWE-bench Pro 50-run orchestrator.

Loop: stage a batch (shallow, STRENGTHENED interface-first goals) -> run the fleet on it ->
capture git diffs + DELETE the worktrees -> next batch. The dev box has ~8GB free, so only ~BATCH
shallow worktrees ever exist at once. Progress -> .fleet/swe/pro_run_50.log; predictions
accumulate in .fleet/swe/pro_preds_50.json (fed to the the eval host Pro grader afterwards).
"""
import json, os, subprocess, time, uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SW = os.path.join(REPO, ".fleet", "swe")
PY = os.path.join(REPO, ".venv", "Scripts", "python.exe")
STATUS = os.path.join(REPO, ".fleet", "status.json")
LOG = os.path.join(SW, "pro_run_50.log")
PREDS = os.path.join(SW, "pro_preds_50.json")
BATCH = 8
#: Which effort arm this run is. Named once, used both on the fleet's command line and in the
#: run config the recorder reads, so the two cannot disagree about what ran.
EFFORT = os.environ.get("SWE_EFFORT", "auto")
RUN_CONFIG = os.path.join(SW, "pro_run_config.json")
PER_BATCH_TIMEOUT = 3600  # 60 min/batch safety net


def log(m):
    line = time.strftime("%H:%M:%S ") + m
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def status():
    try:
        return json.load(open(STATUS, encoding="utf-8-sig"))
    except Exception:
        return {}


def main():
    rows = sorted(json.load(open(os.environ.get("SWE_SLICE_FILE")
                                 or os.path.join(SW, "pro_slice50_full.json"), encoding="utf-8")),
                  key=lambda r: r["instance_id"])
    ids = [r["instance_id"] for r in rows]
    open(LOG, "w").close()
    # A FRESH ACCUMULATOR, EVERY RUN. This kept whatever was already on disk and the capture
    # step replaces only instances whose worktree still exists -- so a staging failure, a
    # timeout or a skipped batch left the PREVIOUS run's patch in the file, and the grader
    # then returned a real grade for a run that never happened. That is the shortest path
    # from this harness to a benchmark number nobody ran, and it needed no bad intent: an
    # interrupted run followed by a shorter one produces it.
    #
    # The old file is kept beside the new one rather than deleted, because a partial run is
    # evidence about the harness and deleting it destroys the only record of what happened.
    if os.path.exists(PREDS):
        keep = PREDS + ".prev"
        try:
            os.replace(PREDS, keep)
            log("previous predictions moved to %s (NOT reused)" % os.path.basename(keep))
        except OSError as exc:
            log("WARNING: could not set the previous predictions aside (%s)" % exc)
    json.dump([], open(PREDS, "w"))
    # AND THE WORKTREE MAP WITH IT. The map accumulates ACROSS BATCHES (it is written once per
    # batch and the join needs all of them), which means it must be cleared once per RUN --
    # otherwise it accumulates across runs too, and the ledger join starts attributing an
    # older run's outcomes and turn counts to this one.
    _wt = os.path.join(SW, "pro_wt_map.json")
    if os.path.exists(_wt):
        try:
            os.replace(_wt, _wt + ".prev")
        except OSError:
            pass
    # WRITE DOWN WHICH ARM THIS IS, at launch, in the run's own directory.
    #
    # The effort was a literal on the command line below and the harness was whatever manifest
    # happened to be active, so a finished run left nothing saying which of the two it had
    # been. The record step then hardcoded "auto" -- a constant standing in for a fact -- and
    # every result in the archive claimed the same arm whatever had run. A remembered
    # conclusion about which effort scored higher could not be checked against anything.
    #
    # The recorder reads this rather than being told again, because a second place to state
    # the arm is a second place for it to be wrong.
    try:
        from relay.selfimprove import manifest as _M
        from relay.selfimprove import runtime_config as _RC
        _active = _RC.active_manifest(refresh=True)
        # A RUN ID, GENERATED HERE, NEVER DECLARED BY ANYONE.
        #
        # The archive needed to tell a CORRECTION of a measurement from a genuine REPEAT of
        # it, and the first attempt asked an operator to say which by passing a flag. That is
        # a self-report, and a self-report nobody verifies is worse than no field at all: on a
        # real repeat, forgetting it overwrites the earlier row and hides actual instability;
        # on a correction, adding it keeps the corrupt grade beside the fixed one and
        # manufactures instability that never happened. Both directions corrupt pass^k, and
        # neither leaves a symptom.
        #
        # An id minted at launch answers the same question without asking anybody: two rows
        # from ONE run are the same measurement (so the later corrects the earlier), and rows
        # from DIFFERENT runs are different measurements. Nothing to remember, nothing to get
        # wrong.
        _run_id = uuid.uuid4().hex[:12]
        json.dump({"effort": EFFORT,
                   "run_id": _run_id,
                   "started": time.time(),
                   "harness_id": _M.harness_id(_active),
                   "parameters": dict(_active.get("parameters") or {})},
                  open(RUN_CONFIG, "w", encoding="utf-8"), ensure_ascii=False)
        log("arm: effort=%s harness=%s run=%s"
            % (EFFORT, _M.harness_id(_active)[:16], _run_id))
    except Exception as exc:
        # Never fatal: this run must still happen. But say so, because a missing arm record
        # is the state the recorder has to refuse to guess its way out of.
        log("WARNING: could not write the run config (%s) -- the recorder will have no arm"
            % exc)
    log("START Pro 50-run: %d instances, batch=%d, strengthened goals" % (len(ids), BATCH))
    bgoals = os.path.join(SW, "pro_batch_goals.jsonl")

    for bi in range(0, len(ids), BATCH):
        batch = ids[bi:bi + BATCH]
        bn = bi // BATCH + 1
        log("=== batch %d/%d: %d instances ===" % (bn, (len(ids) + BATCH - 1) // BATCH, len(batch)))

        # 1) stage (shallow) + build strengthened interface-first goals for this batch
        r = subprocess.run([PY, os.path.join(REPO, "bench", "pro_stage_goals.py"),
                            "--ids", ",".join(batch), "--out", bgoals],
                           cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace")
        log("stage: " + ((r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else "(no output)"))
        ng = sum(1 for _ in open(bgoals, encoding="utf-8")) if os.path.exists(bgoals) else 0
        if ng == 0:
            log("batch %d: 0 goals staged -- skip" % bn)
            continue

        # 2) launch the fleet on this batch HIDDEN (no console window -- the cockpit's card UI is
        #    the surface; raw fleet stdout goes to a log). agent URL resolves from .env.
        # SWE_SIDEPAGE_RESERVE=0: admit by MAIN-tab count only (don't pre-reserve each worker's
        # research+refuter peak), so auto-effort tasks PARALLELIZE on this RAM-tight box. Side-pages
        # still open lazily under their own ram_room gate -> worst case a worker stalls, never the
        # RAM-balloon crash. Lets the cap (e.g. 4) run 4 workers instead of one 3-tab worker.
        env = dict(os.environ, PYTHONIOENCODING="utf-8", SWE_SIDEPAGE_RESERVE="0")
        flog = open(os.path.join(SW, "pro_fleet_batch.log"), "w", encoding="utf-8")
        subprocess.Popen([PY, "-m", "relay.fleet_runner", "--goals-file", bgoals,
                          "--state-dir", os.path.join(REPO, ".fleet"), "--effort", EFFORT],
                         cwd=REPO, env=env, stdout=flog, stderr=subprocess.STDOUT,
                         creationflags=0x08000000)  # CREATE_NO_WINDOW

        # 3) wait for THIS batch fleet to finish (saw running True, then running False)
        t0 = time.time()
        started = False
        while time.time() - t0 < PER_BATCH_TIMEOUT:
            st = status()
            run = st.get("running")
            if run:
                started = True
            if started and run is False:
                log("batch %d fleet done: %s/%s" % (bn, st.get("done_count"), st.get("total")))
                break
            time.sleep(15)
        else:
            log("batch %d TIMEOUT (%ds) -- capturing partial" % (bn, PER_BATCH_TIMEOUT))

        # 4) capture diffs into the preds accumulator + delete the batch worktrees
        r = subprocess.run([PY, os.path.join(REPO, "bench", "pro_capture.py"), "--preds", PREDS],
                           cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace")
        log("capture: " + ((r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else "(no output)"))

    n = len(json.load(open(PREDS, encoding="utf-8")))
    log("DONE Pro 50-run: %d predictions -> %s (grade next on the eval host)" % (n, PREDS))


if __name__ == "__main__":
    main()

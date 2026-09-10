"""Batch-grade captured diffs on the eval host with swebench's NATIVE parallel evaluator.

One swebench process grades the WHOLE predictions file (--max_workers N): each repo's env image
is built ONCE and shared, instances run concurrently inside that process. This replaces the
broken N-separate-grade.py approach (concurrent processes raced on the same env-image build and
returned EVALERR even though the eval succeeded), and it actually uses the eval host's 16 cores.

  python bench/swe_grade_swebench.py --targets-file _chunk1.txt --max-workers 12
  python bench/swe_grade_swebench.py                      # everything in preds_solve/
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swe_check_remote as R

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWEDIR = os.path.join(REPO, ".fleet", "swe")
PREDS = os.path.join(SWEDIR, "preds_solve")
RESULTS = os.path.join(SWEDIR, "grade_results.jsonl")
RUNNER_LOCAL = os.path.join(REPO, "bench", "evalhost_batch_grade.py")

#: THE INTERPRETER THAT ACTUALLY HAS swebench, which the eval host's system python does not and
#: cannot easily be given. `pip install --break-system-packages swebench` gets past PEP 668 and
#: then aborts anyway: it must replace `click 8.1.8`, which Debian installed with no RECORD
#: file, so pip refuses to uninstall it and the whole transaction rolls back (measured
#: 2026-09-10). Forcing it with --ignore-installed would shadow an OS package and move the
#: breakage somewhere later. A dedicated venv stops the eval environment competing with the
#: OS package manager at all.
EVAL_PY = os.environ.get("SWE_EVAL_PYTHON", "/opt/swebench-venv/bin/python")
TMP = os.path.join(SWEDIR, "_grade_batch")


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preds-dir", default=PREDS)
    ap.add_argument("--targets-file", default=None)
    ap.add_argument("--instances", nargs="*", default=None)
    ap.add_argument("--results", default=RESULTS)
    ap.add_argument("--max-workers", type=int, default=12)
    ap.add_argument("--poll-s", type=int, default=30)
    ap.add_argument("--max-wait-min", type=int, default=120,
                    help="batch grade ceiling. Full/repo-heavy runs can exceed 60min on first image builds")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--dataset-name", default="princeton-nlp/SWE-bench_Lite",
                    help="swebench dataset to grade against (e.g. princeton-nlp/SWE-bench_Verified)")
    a = ap.parse_args()

    want = None
    if a.instances:
        want = list(a.instances)
    elif a.targets_file:
        p = a.targets_file if os.path.isabs(a.targets_file) else os.path.join(SWEDIR, a.targets_file)
        want = [l.strip() for l in open(p, encoding="utf-8") if l.strip()]

    preds = []
    for fn in sorted(os.listdir(a.preds_dir)):
        if not fn.endswith(".json"):
            continue
        inst = fn[:-5]
        if want is not None and inst not in want:
            continue
        try:
            patch = json.load(open(os.path.join(a.preds_dir, fn), encoding="utf-8"))[0].get("model_patch") or ""
        except Exception:
            continue
        preds.append({"instance_id": inst, "model_patch": patch, "model_name_or_path": "companion"})
    if not preds:
        log("no predictions to grade"); return
    insts = [p["instance_id"] for p in preds]
    log("grading %d instances via swebench --max_workers %d on the eval host" % (len(preds), a.max_workers))

    runid = a.run_id or ("b" + re.sub(r"[^a-z0-9]", "", time.strftime("%m%d%H%M%S")))
    os.makedirs(TMP, exist_ok=True)
    preds_local = os.path.join(TMP, "preds_" + runid + ".json")
    json.dump(preds, open(preds_local, "w", encoding="utf-8"))

    # 1) stage predictions + the the eval host-side runner
    R._ssh_ps("New-Item -ItemType Directory -Force '%s' | Out-Null; 'ok'" % R.REMOTE_DIR)
    preds_win = "%s/preds_%s.json" % (R.REMOTE_DIR, runid)
    preds_wsl = "/mnt/c/wsl-setup/preds_%s.json" % runid
    runner_win = "%s/evalhost_batch_grade.py" % R.REMOTE_DIR
    runner_wsl = "/mnt/c/wsl-setup/evalhost_batch_grade.py"
    def _scp_retry(local, remote, n=6):
        # the eval host sshd has a very low MaxStartups; a single scp can be transiently rejected.
        # retry a few times (waiting for the handshake window to clear) before giving up.
        for _i in range(n):
            if R._scp(local, remote):
                return True
            time.sleep(7)
        return False
    if not _scp_retry(preds_local, preds_win):
        log("scp predictions failed"); return
    if not _scp_retry(RUNNER_LOCAL, runner_win):
        log("scp runner failed"); return

    # 2) RUN THE BATCH INSIDE A HELD SESSION. Not detached -- detachment does not survive on
    # this host, and three days of EVALERR were that fact refusing to be noticed.
    #
    # MEASURED 2026-09-10, after the grader itself was finally proven correct (a synchronous
    # run of the identical command finished in 107 seconds and returned a real verdict,
    # resolved=1). Handed to `systemd-run --no-block` the same command is STOPPED after 44 and
    # 51 seconds: the journal says "Stopping ... Deactivated successfully" with no error, no
    # OOM and no timeout, and dmesg shows journald flushing its runtime journal -- the distro's
    # systemd is re-initialised once no wsl.exe session is holding it, and every unit that
    # belonged to the previous one goes with it. `setsid nohup` dies the same way. Touching the
    # distro every 20 seconds does NOT rescue it (measured: two arms, held and unheld, both
    # died at ~2 heartbeats), because a new session brings up a new systemd rather than
    # re-adopting the old one's units. dockerd survives here only because a scheduled task
    # holds a session for it.
    #
    # So the work has to run inside a session that stays open for its whole length. The SSH
    # call therefore blocks for the duration, with --max-wait-min as its ceiling, and the
    # runner's log goes to the Windows-shared mount rather than /tmp so a run that dies is
    # still readable afterwards (/tmp here is cleaned within minutes -- the workdir was gone
    # while the run was still being polled).
    # ASK WHETHER THE GRADER CAN RUN BEFORE SPENDING A RUN ON IT. Every A/B grade on
    # 2026-09-09 came back EVALERR, and one of the causes was a single import: swebench was not
    # installed on the interpreter the runner used. A run that cannot import its grader looks
    # exactly like a run whose instances all failed, which is why that took days to see. One
    # cheap question here names it instead.
    probe = R._wsl_token("%s -c 'import swebench.harness.run_evaluation' >/dev/null 2>&1 "
                         "&& echo Y || echo N" % EVAL_PY)
    if probe != "Y":
        log("%s cannot import swebench.harness.run_evaluation (probe=%r). Nothing was run and "
            "no verdict was written -- this is an eval-host setup fault, not a graded outcome. "
            "Fix: python3 -m venv %s && %s/bin/pip install swebench (or point SWE_EVAL_PYTHON "
            "at an interpreter that has it)."
            % (EVAL_PY, probe, os.path.dirname(os.path.dirname(EVAL_PY)),
               os.path.dirname(os.path.dirname(EVAL_PY))))
        return

    log_wsl = "/mnt/c/wsl-setup/%s.log" % runid
    log_win = "%s/%s.log" % (R.REMOTE_DIR, runid)
    body = (EVAL_PY + " " + runner_wsl + " " + preds_wsl + " " + runid + " " + str(a.max_workers)
            + " " + a.dataset_name + " > " + log_wsl + " 2>&1")
    hold_s = max(120, int(a.max_wait_min * 60))
    run_ps = ("$j = Start-Job { (wsl.exe -d " + R.DISTRO + " -u root -- bash -lc \"" + body + "\" 2>$null)"
              " -join '' }; if(Wait-Job $j -Timeout " + str(hold_s) + "){ Receive-Job $j } else { 'TIMEOUT' };"
              " Remove-Job $j -Force")
    log("grading inside a held session (runid=%s, ceiling %d min). This blocks until it finishes."
        % (runid, hold_s // 60))
    t0 = time.time()
    R._ssh_ps(run_ps, hold_s + 60)
    log("session returned after %.0fs" % (time.time() - t0))

    # 3) read the result the runner wrote (same durable location it has always used)
    remote_done = "%s/verdicts/%s.batchresult.json.done" % (R.REMOTE_DIR, runid)
    remote_res = "%s/verdicts/%s.batchresult.json" % (R.REMOTE_DIR, runid)
    local_res = os.path.join(TMP, runid + ".batchresult.json")
    local_done = os.path.join(TMP, runid + ".done")
    result = None
    if R._scp_from(remote_done, local_done) and R._scp_from(remote_res, local_res):
        try:
            result = json.load(open(local_res, encoding="utf-8"))
        except Exception:
            result = None
    if result is None:
        # The run did not produce a result. Say what the remote log says instead of guessing,
        # and write NO row: an infra fault is not a graded outcome (loop.py reads zero rows as
        # INFRA_ABORT, which is the honest reading).
        local_log = os.path.join(TMP, runid + ".log")
        tail = ""
        if R._scp_from(log_win, local_log):
            try:
                tail = open(local_log, encoding="utf-8", errors="replace").read()[-3000:]
            except Exception:
                tail = ""
        log("no batch result for %s. Nothing was written to the ledger. Remote log tail:"
            % runid)
        log(tail or "(the log itself could not be read)")
        return
    if result.get("stderr_tail"):
        # evalhost_batch_grade.py only sets this when it produced zero real verdicts -- the run
        # completed (a result WAS written) but swebench itself never resolved anything, and this
        # is swebench's own reason why, not a guess. Surfaced here so the caller does not have to
        # go fetch %s.run.out by hand to find out the batch ran but failed for a real reason.
        log("swebench produced 0 real verdicts (returncode=%s). stderr tail:\n%s"
            % (result.get("returncode"), result["stderr_tail"]))

    resolved = set(result.get("resolved", []))
    unresolved = set(result.get("unresolved", []))
    errored = set(result.get("error", [])) | set(result.get("empty", []))
    # 4) write per-instance verdicts
    with open(a.results, "a", encoding="utf-8", newline="\n") as f:
        for inst in insts:
            if inst in resolved:
                v = "RESOLVED"
            elif inst in unresolved:
                v = "not"
            elif inst in errored:
                v = "not"   # empty/error patch = not resolved (a real miss, not an eval-host fault)
            else:
                v = "EVALERR"  # swebench never returned a verdict for it (genuine eval gap)
            f.write(json.dumps({"instance_id": inst, "verdict": v, "runid": runid,
                                "ts": int(time.time())}, ensure_ascii=False) + "\n")

    nres = len([i for i in insts if i in resolved])
    ngraded = len([i for i in insts if i in resolved or i in unresolved or i in errored])
    nevalerr = len(insts) - ngraded
    log("=== GRADE SUMMARY: %d/%d instances | RESOLVED %d / graded %d | EVALERR %d ==="
        % (len(insts), len(preds), nres, ngraded, nevalerr))
    if ngraded:
        log("    pass@1 = %d/%d = %.1f%%" % (nres, ngraded, 100.0 * nres / ngraded))


if __name__ == "__main__":
    main()

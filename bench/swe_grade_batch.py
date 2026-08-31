"""Phase 2 of the decoupled run: BATCH-grade captured diffs on the the eval host eval host.

Reads preds_solve/<inst>.json (written by swe_solve_decoupled.py), and for each instance ships
its diff to the eval host, runs the OFFICIAL swebench Docker eval there (the eval host has 503 GB free, so no
local disk wall), and records the verdict. Grades CONCURRENTLY (the eval host is beefy) and is resumable
(an instance already in the results file is skipped).

  python bench/swe_grade_batch.py                                  # grade everything in preds_solve/
  python bench/swe_grade_batch.py --instances django__django-10924 ...
  python bench/swe_grade_batch.py --targets-file .fleet/swe/_chunk1.txt --concurrency 4

Fixes the EVALERR-despite-RESOLVED bug seen with swe_check_remote on a re-grade: the systemd unit
name == run_id, and a unit that already exists (even completed) makes `systemd-run --unit` fail to
start, so the grade never runs and the poll times out as EVALERR. Here the run_id carries a
per-invocation NONCE, so every grade gets a FRESH systemd unit AND a fresh swebench report dir --
no collision, no stale verdict.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import swe_check_remote as R   # reuse the proven SSH/scp/wsl plumbing

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWEDIR = os.path.join(REPO, ".fleet", "swe")
PREDS = os.path.join(SWEDIR, "preds_solve")
RESULTS = os.path.join(SWEDIR, "grade_results.jsonl")
TMP = os.path.join(SWEDIR, "_grade_patches")


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def load_results(path):
    """Instances that have a REAL verdict. EVALERR is not one.

    EVALERR means the evaluation could not be run -- the host was unreachable, the container
    would not start -- and it says nothing about the patch. Counting it as "already graded"
    makes the failure permanent: the instance is skipped forever and can never be scored, even
    once the cause is gone.

    Measured tonight: the eval host was unreachable for one run because of a stale lock file on
    THIS machine. 36 EVALERR rows were written. When the transport was fixed, the grader
    reported "40 preds, 37 already graded, 3 to grade" and would have left 36 patches unscored
    for good. A failed attempt recorded as a result is the same defect this repository keeps
    finding elsewhere, in the one file whose whole job is to hold results.
    """
    out = {}
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if str(d.get("verdict") or "").upper() == "EVALERR":
                    continue
                out[d["instance_id"]] = d
            except Exception:
                pass
    return out


def append_result(path, rec):
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def make_runid(inst, diff, nonce):
    h = hashlib.md5((diff or "").encode("utf-8")).hexdigest()[:8]
    return "g" + re.sub(r"[^A-Za-z0-9]", "", inst) + h + nonce


def launch_grade(inst, diff, runid):
    """Stage the diff on the eval host + launch grade_runner detached. Returns True if launched."""
    os.makedirs(TMP, exist_ok=True)
    lp = os.path.join(TMP, runid + ".patch")
    with open(lp, "w", encoding="utf-8", newline="\n") as f:
        f.write(diff)
    R._ssh_ps("New-Item -ItemType Directory -Force '%s' | Out-Null; 'ok'" % R.REMOTE_DIFFS_WIN)
    remote_win = "%s/%s.patch" % (R.REMOTE_DIFFS_WIN, runid)
    remote_wsl = "%s/%s.patch" % (R.REMOTE_DIFFS_WSL, runid)
    if not R._scp(lp, remote_win):
        return False
    # fresh unit (nonce in runid) -> never collides; reset-failed is belt-and-suspenders.
    launch = ("$j = Start-Job { (wsl.exe -d " + R.DISTRO + " -u root -- bash -lc "
              "'systemctl reset-failed " + runid + " 2>/dev/null; rm -f /tmp/grade_" + runid + ".log; "
              "systemd-run --no-block --unit=" + runid + " bash " + R.RUNNER_WSL
              + " " + inst + " " + remote_wsl + " " + runid + "' 2>$null) -join '' }; "
              "if(Wait-Job $j -Timeout 25){ Receive-Job $j } else { 'TO' }; Remove-Job $j -Force")
    R._ssh_ps(launch, 55)
    return True


def poll_verdict(runid):
    """scp the verdict file back; return 'RESOLVED'/'not'/'' (not done yet)."""
    remote_verdict = "%s/verdicts/%s.verdict" % (R.REMOTE_DIR, runid)
    lv = os.path.join(TMP, runid + ".verdict")
    if R._scp_from(remote_verdict, lv):
        try:
            content = open(lv, encoding="utf-8", errors="replace").read()
        except Exception:
            content = ""
        if "RUNNER_DONE" in content:
            m = re.search(r"VERDICT=([A-Za-z]+)", content)
            return m.group(1) or "EVALERR"
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preds-dir", default=PREDS)
    ap.add_argument("--instances", nargs="*", default=None)
    ap.add_argument("--targets-file", default=None)
    ap.add_argument("--results", default=RESULTS)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--poll-s", type=int, default=20)
    ap.add_argument("--max-wait-min", type=int, default=40, help="per-instance grade ceiling")
    a = ap.parse_args()

    # which instances to grade
    want = None
    if a.instances:
        want = list(a.instances)
    elif a.targets_file:
        p = a.targets_file if os.path.isabs(a.targets_file) else os.path.join(SWEDIR, a.targets_file)
        want = [l.strip() for l in open(p, encoding="utf-8") if l.strip()]
    preds = {}
    for fn in sorted(os.listdir(a.preds_dir)):
        if not fn.endswith(".json"):
            continue
        inst = fn[:-5]
        if want is not None and inst not in want:
            continue
        try:
            preds[inst] = json.load(open(os.path.join(a.preds_dir, fn), encoding="utf-8"))[0]["model_patch"]
        except Exception:
            pass

    done = load_results(a.results)
    todo = [i for i in preds if i not in done]
    log("grade batch: %d preds, %d already graded, %d to grade (concurrency=%d)"
        % (len(preds), len(done) if want is None else len([i for i in want if i in done]), len(todo), a.concurrency))
    if not todo:
        _summary(a.results, want or list(preds))
        return

    nonce = "n" + hex(int(time.time()))[-6:]
    inflight = {}    # runid -> (inst, deadline)
    queue = list(todo)
    while queue or inflight:
        # fill the pool
        while queue and len(inflight) < a.concurrency:
            inst = queue.pop(0)
            runid = make_runid(inst, preds[inst], nonce)
            if not preds[inst].strip():
                append_result(a.results, {"instance_id": inst, "verdict": "not",
                                          "note": "empty patch", "ts": int(time.time())})
                log("  %s -> not (empty patch, skipped eval)" % inst)
                continue
            if launch_grade(inst, preds[inst], runid):
                inflight[runid] = (inst, time.time() + a.max_wait_min * 60)
                log("  launched %s (runid=%s) [%d in flight]" % (inst, runid, len(inflight)))
            else:
                # NAME THE CAUSE, DO NOT DESCRIBE THE SYMPTOM. "launch failed" was reported
                # for an entire night's grades and read as the eval host being down. The host
                # had never been addressed: ssh was being run with an empty hostname because
                # no host was configured. A message that points at the transport when the
                # problem is configuration sends every reader to the wrong machine.
                _why = R.configured() or "launch/scp failed"
                append_result(a.results, {"instance_id": inst, "verdict": "EVALERR",
                                          "note": _why, "ts": int(time.time())})
                log("  %s -> EVALERR (%s)" % (inst, _why))
        # poll in-flight
        time.sleep(a.poll_s)
        for runid in list(inflight):
            inst, deadline = inflight[runid]
            v = poll_verdict(runid)
            if v:
                append_result(a.results, {"instance_id": inst, "verdict": v, "runid": runid,
                                          "ts": int(time.time())})
                log("  VERDICT %s -> %s" % (inst, v))
                del inflight[runid]
            elif time.time() > deadline:
                append_result(a.results, {"instance_id": inst, "verdict": "EVALERR",
                                          "note": "timeout", "runid": runid, "ts": int(time.time())})
                log("  %s -> EVALERR (exceeded %dmin)" % (inst, a.max_wait_min))
                del inflight[runid]

    _summary(a.results, want or list(preds))


def _summary(results_path, insts):
    d = load_results(results_path)
    sub = {i: d[i] for i in insts if i in d}
    n = len(sub)
    resolved = sum(1 for r in sub.values() if r.get("verdict") == "RESOLVED")
    notr = sum(1 for r in sub.values() if r.get("verdict") == "not")
    evalerr = sum(1 for r in sub.values() if r.get("verdict") == "EVALERR")
    graded = resolved + notr
    log("=== GRADE SUMMARY: %d graded -> RESOLVED %d / not %d | EVALERR %d (excluded) ==="
        % (graded, resolved, notr, evalerr))
    if graded:
        log("    pass@1 (graded only) = %d/%d = %.1f%%" % (resolved, graded, 100.0 * resolved / graded))


if __name__ == "__main__":
    main()

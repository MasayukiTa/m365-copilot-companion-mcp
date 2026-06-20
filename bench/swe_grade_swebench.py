"""Batch-grade captured diffs on kiyus with swebench's NATIVE parallel evaluator.

One swebench process grades the WHOLE predictions file (--max_workers N): each repo's env image
is built ONCE and shared, instances run concurrently inside that process. This replaces the
broken N-separate-grade.py approach (concurrent processes raced on the same env-image build and
returned EVALERR even though the eval succeeded), and it actually uses kiyus's 16 cores.

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

REPO = r"C:\Users\USER\companion-mcp"
SWEDIR = os.path.join(REPO, ".fleet", "swe")
PREDS = os.path.join(SWEDIR, "preds_solve")
RESULTS = os.path.join(SWEDIR, "grade_results.jsonl")
RUNNER_LOCAL = os.path.join(REPO, "bench", "kiyus_batch_grade.py")
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
    log("grading %d instances via swebench --max_workers %d on kiyus" % (len(preds), a.max_workers))

    runid = a.run_id or ("b" + re.sub(r"[^a-z0-9]", "", time.strftime("%m%d%H%M%S")))
    os.makedirs(TMP, exist_ok=True)
    preds_local = os.path.join(TMP, "preds_" + runid + ".json")
    json.dump(preds, open(preds_local, "w", encoding="utf-8"))

    # 1) stage predictions + the kiyus-side runner
    R._ssh_ps("New-Item -ItemType Directory -Force '%s' | Out-Null; 'ok'" % R.REMOTE_DIR)
    preds_win = "%s/preds_%s.json" % (R.REMOTE_DIR, runid)
    preds_wsl = "/mnt/c/wsl-setup/preds_%s.json" % runid
    runner_win = "%s/kiyus_batch_grade.py" % R.REMOTE_DIR
    runner_wsl = "/mnt/c/wsl-setup/kiyus_batch_grade.py"
    if not R._scp(preds_local, preds_win):
        log("scp predictions failed"); return
    if not R._scp(RUNNER_LOCAL, runner_win):
        log("scp runner failed"); return

    # 2) launch ONE swebench batch eval, detached (survives SSH drops; long build+run)
    inner = ("systemctl reset-failed " + runid + " 2>/dev/null; rm -f /tmp/gb_" + runid + ".log; "
             "systemd-run --no-block --unit=" + runid + " bash -lc "
             "'python3 " + runner_wsl + " " + preds_wsl + " " + runid + " " + str(a.max_workers)
             + " " + a.dataset_name
             + " > /tmp/gb_" + runid + ".log 2>&1'")
    launch = ("$j = Start-Job { (wsl.exe -d " + R.DISTRO + " -u root -- bash -lc \"" + inner + "\" 2>$null)"
              " -join '' }; if(Wait-Job $j -Timeout 30){ Receive-Job $j } else { 'TO' }; Remove-Job $j -Force")
    R._ssh_ps(launch, 55)
    log("launched swebench batch (runid=%s). polling for result..." % runid)

    # 3) poll for the .done marker, then pull the result json
    remote_done = "%s/verdicts/%s.batchresult.json.done" % (R.REMOTE_DIR, runid)
    remote_res = "%s/verdicts/%s.batchresult.json" % (R.REMOTE_DIR, runid)
    local_done = os.path.join(TMP, runid + ".done")
    local_res = os.path.join(TMP, runid + ".batchresult.json")
    deadline = time.time() + a.max_wait_min * 60
    result = None
    while time.time() < deadline:
        time.sleep(a.poll_s)
        if R._scp_from(remote_done, local_done):
            if R._scp_from(remote_res, local_res):
                try:
                    result = json.load(open(local_res, encoding="utf-8"))
                    break
                except Exception:
                    pass
        # progress heartbeat from the kiyus eval log
        tail = R._wsl_token("tail -1 /tmp/gb_" + runid + ".log 2>/dev/null | tr -cd 'A-Za-z0-9:%=/ .' | tail -c 80")
        if tail:
            log("  ...kiyus: %s" % tail)
    if result is None:
        log("TIMEOUT after %d min -- no batch result. Check /tmp/gb_%s.log on kiyus." % (a.max_wait_min, runid))
        return

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

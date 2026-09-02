# -*- coding: utf-8 -*-
"""Grade SWE-bench-Pro predictions on the eval host. The reusable entry point that was missing.

WHY THIS EXISTS. Thirty-nine captured patches went unscored for a night, and the reason was
that the only grader anyone could invoke -- bench/swe_grade_batch.py, via grade.py on the eval
host -- passes `--dataset_name princeton-nlp/SWE-bench_Lite`. The instances were Pro (NodeBB,
teleport, protonmail, tutanota); they are not in Lite, so the harness could not find them, wrote
no report, and every verdict came back EVALERR. That reads exactly like a broken eval host, and
it was reported as one. Twice.

The Pro pipeline was on the host the whole time -- pro_repo/swe_bench_pro_eval.py, the harness,
the run_scripts, the cached images. What did NOT exist was any way to run it that was not a
one-off script with a smoke run's filenames baked in (pro_smoke_preds.json, pro_raw_4.jsonl).
So the capability existed and could not be reached, which for practical purposes is the same as
not having it.

TWO SAFETY RULES ARE COMPILED IN HERE, not left to memory:

  1. NO PRUNING, EVER. The eval host's drive is append-only by the owner's decision, made after
     a drive was destroyed by exactly this kind of churn. The predecessor script says so in its
     own comments and had its janitor disabled for it. Nothing here deletes images.

  2. THE CLOUDFLARED LOCKS ARE CLEARED BEFORE CONNECTING. Killing a hung cloudflared leaves
     0-byte .lock files in ~/.cloudflared, and the next cloudflared waits on them forever --
     which presents as "Connection timed out during banner exchange" and gets misdiagnosed as
     the host being down. That cost a night. It is done by the transport now so it cannot be
     forgotten again.

ONE HELD SESSION. The WSL VM tears down when a `wsl -d ... --exec` command returns, taking
dockerd and /tmp with it. The grade therefore runs inside a single foreground ssh session held
open for its whole duration, which is what the earlier launcher did and why.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SW = os.path.join(REPO, ".fleet", "swe")

#: Where the Pro pipeline lives on the eval host, Windows side and WSL side.
REMOTE_WIN = "C:/swe-grade"
REMOTE_WSL = "/mnt/c/swe-grade"
VENV_PY = "/root/swe-venv/bin/python"


def log(msg):
    """Print so it can actually be READ while this runs.

    Nineteen bare print() calls meant a block-buffered stdout: launched into a log file, as it
    normally is, this produced an EMPTY file for tens of minutes and there was no way to tell a
    working run from a hung one. Encode-safe for the same reason pro_cycle's is -- this console
    is cp932 and a replacement character in a grader's output has taken a long run down before,
    and a line of output is not worth a run.
    """
    try:
        print(msg, flush=True)
    except Exception:
        try:
            enc = (getattr(sys.stdout, "encoding", None) or "utf-8")
            print(str(msg).encode(enc, "replace").decode(enc, "replace"), flush=True)
        except Exception:
            # LAST RESORT ONLY. A bare pass here hid a RecursionError for three checks when the
            # body of this function accidentally called itself: every message vanished and the
            # run looked silent rather than broken. If even the encode-safe print fails there is
            # nowhere left to write, but it must not look like success.
            try:
                sys.__stderr__.write("log() failed" + chr(10))
            except Exception:
                pass


def ssh_host() -> str:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    return (os.environ.get("EVAL_SSH_HOST", "")
            or os.environ.get("SWE_EVAL_HOST", "")).strip()


def clear_stale_locks(home: str = None) -> int:
    """Remove 0-byte cloudflared token locks left by a killed cloudflared. Returns how many.

    THE STEP THAT WAS MISSING FROM THE PROCEDURE. Killing the process is documented; the locks
    it leaves are not, and `Get-Process cloudflared` returning zero was read as proof of
    innocence. It is not: the locks outlive the process, the next cloudflared blocks on them,
    and the symptom is a banner-exchange timeout that looks like someone else's machine.

    Only an EMPTY lock is removed, and only when no cloudflared is running -- a lock somebody
    is holding is not ours to break.
    """
    if _cloudflared_running():
        return 0
    root = home or os.path.join(os.path.expanduser("~"), ".cloudflared")
    removed = 0
    for path in glob.glob(os.path.join(root, "*.lock")):
        try:
            if os.path.getsize(path) == 0:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def _cloudflared_running() -> bool:
    """Fail CLOSED: if this cannot be determined, report that one IS running, so the locks are
    left alone. Breaking a live lock is worse than leaving a stale one."""
    try:
        import psutil
    except Exception:
        return True
    try:
        for p in psutil.process_iter(["name"]):
            if "cloudflared" in (p.info.get("name") or "").lower():
                return True
    except Exception:
        return True
    return False


def _ssh_base(host: str):
    # Windows OpenSSH explicitly, not whatever `ssh` resolves to. Git Bash ships its own and
    # the ProxyCommand is a Windows path; mixing them is one more way to spend an hour.
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "OpenSSH", "ssh.exe")
    if not os.path.isfile(exe):
        exe = "ssh"
    return [exe, "-o", "ConnectTimeout=45", "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=30", host]


def ssh(host: str, command: str, timeout: float = 300):
    """One command on the eval host's Windows shell. Locks cleared first, every time."""
    clear_stale_locks()
    import base64
    b64 = base64.b64encode(("$ProgressPreference='SilentlyContinue';" + command)
                           .encode("utf-16-le")).decode()
    r = subprocess.run(_ssh_base(host) + ["powershell", "-NoProfile", "-EncodedCommand", b64],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def scp_to(host: str, local: str, remote_win: str, timeout: float = 600) -> bool:
    clear_stale_locks()
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "OpenSSH", "scp.exe")
    if not os.path.isfile(exe):
        exe = "scp"
    r = subprocess.run([exe, "-o", "ConnectTimeout=45", "-o", "BatchMode=yes",
                        local, "%s:%s" % (host, remote_win)],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0


def scp_from(host: str, remote_win: str, local: str, timeout: float = 600) -> bool:
    clear_stale_locks()
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "OpenSSH", "scp.exe")
    if not os.path.isfile(exe):
        exe = "scp"
    r = subprocess.run([exe, "-o", "ConnectTimeout=45", "-o", "BatchMode=yes",
                        "%s:%s" % (host, remote_win), local],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0 and os.path.exists(local) and os.path.getsize(local) > 0


# ── shaping the inputs ────────────────────────────────────────────────────────────────────

def gradeable(preds):
    """The predictions worth sending, in the shape the Pro harness reads: instance_id + patch.

    An EMPTY patch is dropped rather than sent. The harness would score it as unresolved, which
    is a verdict about a model that produced nothing -- and here an empty row usually means the
    capture REFUSED an oversize diff, which is a different fact entirely. Sending it would turn
    "we have no patch" into "the patch was wrong".
    """
    out = []
    for p in preds or []:
        if not isinstance(p, dict):
            continue
        inst = p.get("instance_id")
        patch = (p.get("patch") or p.get("model_patch") or "")
        if inst and patch.strip():
            out.append({"instance_id": inst, "patch": patch})
    return out


def grade_script(raw_wsl: str, preds_wsl: str, out_wsl: str, out_log: str,
                 workers: int = 2, pull_parallel: int = 3) -> str:
    """The shell script the eval host runs, modelled on the one that graded 40 instances before.

    NO JANITOR AND NO PRUNE. The predecessor had a floor-janitor that deleted completed images
    to make room; it was disabled after the owner ruled the drive append-only, having already
    lost one to that pattern. This script measures and stops rather than deleting, and a test
    asserts no prune command can creep back in.
    """
    return "\n".join([
        "#!/bin/bash",
        "exec > %s 2>&1" % out_log,
        "set -u",
        "REPO=%s/pro_repo; PY=%s" % (REMOTE_WSL, VENV_PY),
        'RAW="%s"; PREDS="%s"; OUT="%s"' % (raw_wsl, preds_wsl, out_wsl),
        'rm -rf "$OUT"; mkdir -p "$OUT"; cd "$REPO"',
        "freeG(){ df -BG /mnt/c | awk 'NR==2{gsub(/G/,\"\",$4);print $4}'; }",
        'echo "[$(date +%H:%M:%S)] START pro grade free=$(freeG)G"',
        "# STOP rather than delete: the drive is append-only by the owner's decision.",
        'if [ "$(freeG)" -lt 40 ]; then echo "ABORT: under 40G free, refusing to start"; exit 3; fi',
        '( "$PY" -c "import json;[print(\'jefzda/sweap-images:\'+json.loads(l)[\'dockerhub_tag\']) for l in open(\'$RAW\')]"'
        " | xargs -P%d -I{} bash -c 'for t in 1 2 3 4 5; do timeout 800 docker pull \"{}\" >/dev/null 2>&1 && exit 0; sleep 12; done' ) & PUL=$!"
        % pull_parallel,
        'timeout 30000 "$PY" swe_bench_pro_eval.py --use_local_docker --num_workers %d \\' % workers,
        '  --raw_sample_path "$RAW" --patch_path "$PREDS" --scripts_dir run_scripts \\',
        '  --dockerhub_username jefzda --output_dir "$OUT" 2>&1 | tail -3',
        'kill "$PUL" 2>/dev/null',
        '"$PY" -c "import json;m=json.load(open(\'$OUT/eval_results.json\'));'
        "r=sum(1 for v in m.values() if v);print('RESOLVED %d/%d = %.1f%%'%(r,len(m),100*r/max(1,len(m))))\"",
        'echo "DONE_PRO_GRADE $(date +%H:%M:%S) free=$(freeG)G"',
    ]) + "\n"


def ingest(eval_results: dict, existing_path: str) -> int:
    """Fold the harness's {instance_id: bool} into the run's verdict ledger. Returns rows added.

    Written as RESOLVED / not, the same vocabulary the rest of the pipeline uses, so a Pro grade
    and a Lite grade are readable side by side. EVALERR is never written from here: this
    function only sees instances the harness actually reported on.
    """
    have = set()
    if os.path.isfile(existing_path):
        with open(existing_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if str(row.get("verdict") or "").upper() != "EVALERR":
                    have.add(row.get("instance_id"))
    added = 0
    with open(existing_path, "a", encoding="utf-8", newline="\n") as fh:
        for inst, resolved in (eval_results or {}).items():
            if inst in have:
                continue
            fh.write(json.dumps({"instance_id": inst,
                                 "verdict": "RESOLVED" if resolved else "not",
                                 "grader": "swe_bench_pro_eval"}, ensure_ascii=False) + "\n")
            added += 1
    return added


# ── the run ───────────────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(prog="bench.pro_grade_remote", description=__doc__.splitlines()[0])
    ap.add_argument("--preds", default=os.path.join(SW, "pro_cycle_preds.json"))
    ap.add_argument("--results", default=os.path.join(SW, "pro_cycle_results.json"))
    ap.add_argument("--tag", default="cycle40", help="names the remote files for this run")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--instances", nargs="*", default=None,
                    help="grade only these instance ids from --preds; the cycle passes "
                         "one batch at a time so a batch is scored while the next runs")
    ap.add_argument("--dry-run", action="store_true", help="stage nothing; print what would run")
    a = ap.parse_args(argv)

    host = ssh_host()
    if not host:
        log("no eval host configured: set EVAL_SSH_HOST (or SWE_EVAL_HOST)")
        return 2

    with open(a.preds, encoding="utf-8-sig") as fh:
        preds = json.load(fh)
    rows = gradeable(preds)
    if a.instances:
        # A BATCH, NOT THE LEDGER. The preds file accumulates every capture ever made;
        # grading all of it after each batch would re-score eighty instances to learn
        # about three. The caller names the three.
        # STRIPPED, AND A MISS IS REPORTED. A list built on Windows and passed through the
        # shell arrives with a trailing carriage return on every line but the last, and the
        # filter then matched exactly one of fifteen instances -- and said "graded 1
        # instance(s)" as though that were the whole job. Silently grading a fraction of what
        # was asked for is worse than refusing.
        want = {str(i).strip() for i in a.instances if str(i).strip()}
        have = {r.get("instance_id") for r in rows}
        missing = sorted(want - have)
        rows = [r for r in rows if r.get("instance_id") in want]
        if missing:
            log("%d of %d requested instance(s) have no gradeable patch and were NOT graded:"
                % (len(missing), len(want)))
            for i in missing[:5]:
                log("    %s" % i)
            if len(missing) > 5:
                log("    ... and %d more" % (len(missing) - 5))
    log("%d of %d predictions are gradeable (empty patches dropped)" % (len(rows), len(preds)))
    if not rows:
        return 1

    preds_win = "%s/pro_preds_%s.json" % (REMOTE_WIN, a.tag)
    preds_wsl = "%s/pro_preds_%s.json" % (REMOTE_WSL, a.tag)
    raw_wsl = "%s/pro_raw_%s.jsonl" % (REMOTE_WSL, a.tag)
    out_wsl = "%s/pro_out_%s" % (REMOTE_WSL, a.tag)
    out_log = "%s/pro_grade_%s.out" % (REMOTE_WSL, a.tag)
    sh_win = "%s/pro_grade_%s.sh" % (REMOTE_WIN, a.tag)
    sh_wsl = "%s/pro_grade_%s.sh" % (REMOTE_WSL, a.tag)

    script = grade_script(raw_wsl, preds_wsl, out_wsl, out_log, workers=a.workers)
    if a.dry_run:
        log("would stage %d predictions to %s" % (len(rows), preds_win))
        log("--- script ---")
        log(script)
        return 0

    local_preds = os.path.join(SW, "pro_preds_%s.json" % a.tag)
    with open(local_preds, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(rows, fh, ensure_ascii=False)
    local_sh = os.path.join(SW, "pro_grade_%s.sh" % a.tag)
    with open(local_sh, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(script)

    locks = clear_stale_locks()
    if locks:
        log("cleared %d stale cloudflared lock(s) before connecting" % locks)

    log("staging predictions and script...")
    if not scp_to(host, local_preds, preds_win):
        log("could not send the predictions")
        return 2
    if not scp_to(host, local_sh, sh_win):
        log("could not send the grade script")
        return 2

    log("building the raw dataset rows for these instances on the host...")
    build = ("wsl.exe -d Ubuntu -u root -- %s -c \"import json;"
             "ids={p['instance_id'] for p in json.load(open('%s'))};"
             "from datasets import load_dataset;"
             "ds=load_dataset('ScaleAI/SWE-bench_Pro',split='test');"
             "rows=[dict(r) for r in ds if r['instance_id'] in ids];"
             "open('%s','w').write(chr(10).join(json.dumps(r,ensure_ascii=False) for r in rows));"
             "print('RAWROWS',len(rows))\"" % (VENV_PY, preds_wsl, raw_wsl))
    code, out = ssh(host, build, timeout=1800)
    log(out.strip()[-400:])
    if "RAWROWS" not in out:
        log("the dataset rows could not be built; not starting a grade that cannot score")
        return 2

    log("running the grade in ONE held session (the WSL VM tears down between commands)...")
    run = 'wsl.exe -d Ubuntu -u root -- bash %s' % sh_wsl
    code, out = ssh(host, run, timeout=30000)
    log(out.strip()[-600:])

    local_results = os.path.join(SW, "pro_eval_results_%s.json" % a.tag)
    if not scp_from(host, "%s/pro_out_%s/eval_results.json" % (REMOTE_WIN, a.tag), local_results):
        log("the grade produced no eval_results.json to collect")
        return 2
    with open(local_results, encoding="utf-8") as fh:
        results = json.load(fh)
    added = ingest(results, a.results)
    resolved = sum(1 for v in results.values() if v)
    log("graded %d instance(s): RESOLVED %d/%d = %.1f%%  (%d new rows in the ledger)"
          % (len(results), resolved, len(results),
             100.0 * resolved / max(1, len(results)), added))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""swe_check_remote.py -- grade a SWE-bench instance on the the eval host eval host (over SSH),
mirroring bench/swe_check.py's interface so round_runner can swap it in transparently.

    python bench/swe_check_remote.py <instance_id> <worktree_dir>
    exit 0 = RESOLVED, 1 = not resolved, 2 = EVALERR (infra)

Why remote: the local 16 GB machine's WSL vhdx fills C: during Docker grading (the
hardware wall). the eval host has 503 GB free, runs the official swebench Docker eval, and returns
just a verdict. SOLVE still happens locally (M365 Copilot); only GRADE moves off-box.

Switched on by SWE_GRADE_REMOTE=1 in round_runner; default OFF keeps local swe_check.py so
single / offline runs still work unchanged.

Hard-won remote-exec rules (see project_the eval host_swe_eval_host memory):
  * Transfer scripts/patches with scp (NOT echo/base64/bash -c -- nested quotes break
    through PS->SSH->wsl->bash).
  * Drive WSL via base64-encoded PowerShell (-EncodedCommand) + $ProgressPreference=Silent.
  * Read back only single short tokens (multi-line output is dropped intermittently).
  * The eval VM is kept alive by the SweDockerd scheduled task; dockerd persists.
"""
import base64
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time

# THE HOST COMES FROM THE ENVIRONMENT, WITH NO DEFAULT. It used to default to the machine's
# actual name, which put an operator's hostname in a public repository -- and a wrong default
# here fails obscurely anyway, because ssh resolves it and reports a name nobody recognises.
#
# THREE FAIL-OPEN LAYERS SAT HERE, AND TOGETHER THEY COST A WHOLE BENCHMARK RUN. Every grade
# for an entire night returned "EVALERR (launch failed)", which reads as the eval host being
# down, and was reported that way repeatedly. The host was never contacted at all:
#
#   1. .env was never loaded by this module, so a value written there could not arrive;
#   2. the name here and the name in .env were different, so even a loaded .env missed;
#   3. the guard that would have said so is behind `__name__ == "__main__"`, and the batch
#      grader IMPORTS this module. The one path anybody uses skipped the check, built
#      `ssh` with an empty host, and failed with a message about the wrong thing.
#
# A guard that only runs on the path nobody takes is not a guard.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass

SSH_HOST = (os.environ.get("EVAL_SSH_HOST", "")
            or os.environ.get("SWE_EVAL_HOST", "")).strip()


def configured() -> str:
    """'' when the eval host is set, otherwise why it is not. Callers check this instead of
    discovering an empty hostname several layers down as a transport failure."""
    if SSH_HOST:
        return ""
    return ("no eval host is configured: set EVAL_SSH_HOST (or SWE_EVAL_HOST) in .env or the "
            "environment. Nothing was sent anywhere; this is not the host being unreachable.")


if not SSH_HOST and __name__ == "__main__":
    raise SystemExit(configured())
DISTRO = os.environ.get("EVAL_HOST_WSL_DISTRO", "Ubuntu")
REMOTE_DIR = "C:/wsl-setup"                       # Windows-side staging on the eval host
REMOTE_DIFFS_WIN = REMOTE_DIR + "/diffs"
REMOTE_DIFFS_WSL = "/mnt/c/wsl-setup/diffs"
GRADE_PY_WSL = "/mnt/c/wsl-setup/grade.py"
RUNNER_WSL = "/mnt/c/wsl-setup/grade_runner.sh"
POLL_SECONDS = int(os.environ.get("EVAL_HOST_POLL_SECONDS", "30"))
POLL_MAX = int(os.environ.get("EVAL_HOST_POLL_MAX", "120"))    # 120 * 30s = 60 min ceiling

_SSH_BASE = ["ssh", "-o", "ConnectTimeout=30", "-o", "BatchMode=yes",
             "-o", "ServerAliveInterval=20", SSH_HOST]


def _ssh_ps(ps_script, timeout=60, tries=3):
    """Run a PowerShell snippet on the eval host via -EncodedCommand. Returns stdout (NULs
    stripped). Retries on the flaky tunnel; returns '' if every attempt is empty."""
    if not SSH_HOST:
        # FAIL CLOSED HERE TOO, not only at __main__. Running `ssh` with an empty host argument
        # is what produced a night of "launch failed" verdicts about a host that was never
        # addressed.
        return ""
    full = "$ProgressPreference='SilentlyContinue';" + ps_script
    b64 = base64.b64encode(full.encode("utf-16-le")).decode()
    for _ in range(tries):
        try:
            r = subprocess.run(_SSH_BASE + ["powershell", "-NoProfile", "-EncodedCommand", b64],
                               capture_output=True, text=True, timeout=timeout)
            out = (r.stdout or "").replace("\x00", "")
            if out.strip():
                return out
        except Exception:
            pass
    return ""


def _wsl_token(bashcmd, timeout=50):
    """Run one wsl bash command that echoes a single token as R=<token>; return <token>."""
    ps = ("$j = Start-Job { (wsl.exe -d " + DISTRO + " -u root -- bash -lc 'echo R=$("
          + bashcmd + ")' 2>$null) -join '' }; "
          "if(Wait-Job $j -Timeout 22){ Receive-Job $j } else { 'HUNG' }; Remove-Job $j -Force")
    out = _ssh_ps(ps, timeout)
    m = re.search(r"R=(\S*)", out)
    return m.group(1) if m else ""


def _scp(local_path, remote_win_path):
    if not SSH_HOST:
        return False        # no host: nothing was sent anywhere. See configured().
    try:
        r = subprocess.run(["scp", "-o", "ConnectTimeout=30", "-o", "BatchMode=yes",
                            local_path, "%s:%s" % (SSH_HOST, remote_win_path)],
                           capture_output=True, text=True, timeout=120)
        return r.returncode == 0
    except Exception:
        return False


def _scp_from(remote_win_path, local_path):
    """Pull a file the eval host->here. scp is reliable where grep-over-SSH silently drops output,
    so the verdict is read back as a file rather than parsed from a remote grep."""
    if not SSH_HOST:
        return False        # no host: nothing was fetched from anywhere. See configured().
    try:
        r = subprocess.run(["scp", "-o", "ConnectTimeout=30", "-o", "BatchMode=yes",
                            "%s:%s" % (SSH_HOST, remote_win_path), local_path],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0 and os.path.exists(local_path) and os.path.getsize(local_path) > 0
    except Exception:
        return False


def main():
    if len(sys.argv) < 3:
        print("usage: swe_check_remote.py <instance_id> <worktree_dir>", file=sys.stderr)
        return 2
    inst, wt = sys.argv[1], sys.argv[2]
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", inst)        # for the diff filename

    # 1) capture the candidate diff from the worktree (BEFORE building run_id, which hashes it)
    try:
        diff = subprocess.run(["git", "-C", wt, "diff"], capture_output=True, text=True,
                              timeout=60).stdout
    except Exception as e:
        print("REMOTE_GRADE diff failed: %s" % e, file=sys.stderr)
        return 2

    # run id doubles as the systemd unit name + swebench --run_id; keep it strictly alphanumeric
    # ('__' / '-' in a systemd-run --unit name silently fails to start). CRITICAL: it must be UNIQUE
    # PER (instance, diff) so swebench creates a FRESH report dir and never reuses a stale verdict
    # from a previous run or the other A/B arm. A constant-per-instance run_id is exactly what let a
    # days-old report be scored as this run's result (the "ON arm graded in 46s" stale-report bug
    # that made ⑤OFF and ⑤ON look identical). Hashing the diff guarantees a different diff -> a
    # different run_id -> a real eval.
    diff_hash = hashlib.md5((diff or "").encode("utf-8")).hexdigest()[:10]
    runid = "g" + re.sub(r"[^A-Za-z0-9]", "", inst) + diff_hash

    tf = tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, newline="\n",
                                     encoding="utf-8")
    tf.write(diff)
    tf.close()

    # 2) stage on the eval host: ensure diffs dir, scp the patch
    _ssh_ps("New-Item -ItemType Directory -Force '%s' | Out-Null; 'ok'" % REMOTE_DIFFS_WIN)
    remote_patch_win = "%s/%s.patch" % (REMOTE_DIFFS_WIN, safe)
    remote_patch_wsl = "%s/%s.patch" % (REMOTE_DIFFS_WSL, safe)
    if not _scp(tf.name, remote_patch_win):
        print("REMOTE_GRADE scp(diff) failed", file=sys.stderr)
        return 2
    try:
        os.unlink(tf.name)
    except Exception:
        pass

    # 3) launch grade.py detached as a transient systemd unit (survives SSH drops; the eval
    #    can take many minutes on the first per-repo Docker image build).
    launch = ("$j = Start-Job { (wsl.exe -d " + DISTRO + " -u root -- bash -lc "
              "'systemctl reset-failed " + runid + " 2>/dev/null; rm -f /tmp/grade_" + runid + ".log; "
              "systemd-run --no-block --unit=" + runid + " bash " + RUNNER_WSL
              + " " + inst + " " + remote_patch_wsl + " " + runid + "' 2>$null) -join '' }; "
              "if(Wait-Job $j -Timeout 25){ Receive-Job $j } else { 'TO' }; Remove-Job $j -Force")
    _ssh_ps(launch, 55)

    # 4) poll for the verdict FILE (grade_runner.sh writes VERDICT=.. + RUNNER_DONE to a
    #    Windows-side file). scp it back each tick -- reliable, unlike grep-over-SSH which
    #    drops multi-line output on this tunnel.
    remote_verdict = "%s/verdicts/%s.verdict" % (REMOTE_DIR, runid)
    lv = tempfile.NamedTemporaryFile(suffix=".verdict", delete=False)
    lv.close()
    verdict = ""
    for _ in range(POLL_MAX):
        time.sleep(POLL_SECONDS)
        if _scp_from(remote_verdict, lv.name):
            try:
                content = open(lv.name, encoding="utf-8", errors="replace").read()
            except Exception:
                content = ""
            if "RUNNER_DONE" in content:
                m = re.search(r"VERDICT=([A-Za-z]+)", content)
                verdict = m.group(1) if m else ""
                break
    try:
        os.unlink(lv.name)
    except Exception:
        pass

    print("REMOTE_GRADE %s -> %s" % (inst, verdict or "EVALERR"))
    if verdict == "RESOLVED":
        return 0
    if verdict == "not":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

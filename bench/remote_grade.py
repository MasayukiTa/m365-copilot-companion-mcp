# -*- coding: utf-8 -*-
"""Grade ONE SWE-bench instance / ONE patch file on the remote grading host.

    python bench/remote_grade.py <instance_id> <patch_file> [--timeout SECONDS] [--json]

Exit code: 0 = resolved, 1 = graded and NOT resolved, 2 = infrastructure failure (no verdict).

WHY A SEPARATE CLIENT. bench/swe_check_remote.py grades a worktree and returns only an exit
code, and bench/swe_grade_batch.py grades a directory. The effort benchmark needs the one thing
neither gives: "this patch file, this instance -> a structured result whose failure class says
whether the PATCH failed or the HOST did". Those two are opposite kinds of evidence. A patch
that does not resolve is a measurement; a host that did not answer is not, and counting it as a
failure would move a pass rate by an amount that has nothing to do with the patch.

Only the transport is remote. The grading itself is C:/wsl-setup/grade.py on the host, run by
grade_runner.sh inside the grading WSL distro (SWE-bench Lite dataset, instance-level cache).

CONNECTION SETTINGS ARE NOT IN THIS REPOSITORY. The ssh host alias comes from EVAL_SSH_HOST or
SWE_EVAL_HOST (environment or .env), exactly like bench/swe_check_remote.py; keys and the proxy
command live in the operator's ~/.ssh/config. Nothing here logs the alias, a key, or anything
read from the environment: error text from the far side is passed through _scrub() first.

THE SSH/SCP LAYER IS INJECTED (the Transport class), so the whole flow is testable with a fake.

TWO RULES LEARNED ON THIS HOST, kept from the earlier clients:
  * Transfer with scp, never echo/base64/nested quoting; drive PowerShell via -EncodedCommand.
  * The grade runs inside ONE held foreground session. Nothing else may start a WSL session on
    the host while it runs (its exit can tear the VM, and the grade, down).
This module never starts, stops or repairs anything on the host. If the distro is down, the
result is an infra failure that says so.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
import bench.verdicts as _V  # noqa: E402  (the one definition of a verdict)
from tools.childproc import run as _child_run  # noqa: E402  (decodes without the locale codec)

REMOTE_DIR = "C:/wsl-setup"
REMOTE_DIFFS_WIN = REMOTE_DIR + "/diffs"
REMOTE_DIFFS_WSL = "/mnt/c/wsl-setup/diffs"
REMOTE_VERDICTS_WIN = REMOTE_DIR + "/verdicts"
RUNNER_WSL = "/mnt/c/wsl-setup/grade_runner.sh"
DISTRO = os.environ.get("EVAL_HOST_WSL_DISTRO", "Ubuntu")
DEFAULT_TIMEOUT = 1800          # matches grade.py's own 1800 s harness timeout

# error classes
NONE = "none"                   # resolved
GRADED_FAIL = "graded-fail"     # the patch was graded and did not resolve
INFRA = "infra"                 # no verdict: says nothing about the patch


class TransportError(Exception):
    """The transport could not do what was asked (unreachable, refused, timed out)."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(kind)
        self.kind = kind            # "unreachable" | "timeout"
        self.detail = detail


@dataclass
class GradeResult:
    instance: str
    resolved: bool
    verdict: str                    # RESOLVED | not | EVALERR | (raw text if malformed)
    seconds: float
    error_class: str                # none | graded-fail | infra
    infra_reason: str = ""          # not_configured|unreachable|timeout|scp_failed|malformed|evalerr|bad_input
    detail: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# -- configuration / redaction -------------------------------------------------------------------

def configured_host() -> str:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    return (os.environ.get("EVAL_SSH_HOST", "") or os.environ.get("SWE_EVAL_HOST", "")).strip()


def _scrub(text: str, host: str = "", limit: int = 300) -> str:
    """Bound and de-identify far-side text before it is stored or printed."""
    t = (text or "").replace("\x00", "")
    if host:
        t = t.replace(host, "<host>")
    t = re.sub(r"(?i)(pass(word)?|token|secret|api[_-]?key)\s*[=:]\s*\S+", r"\1=<redacted>", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]


# -- the injectable transport --------------------------------------------------------------------

class Transport:
    """Real ssh/scp. Tests substitute an object with the same three methods."""

    def __init__(self, host: str):
        self.host = host

    def _exe(self, name: str) -> str:
        p = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", name + ".exe")
        return p if os.path.isfile(p) else name

    def _run(self, argv, timeout):
        try:
            r = _child_run(argv, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TransportError("timeout")
        except OSError as exc:
            raise TransportError("unreachable", type(exc).__name__)
        return r

    def ps(self, script: str, timeout: float):
        """PowerShell on the host. Returns (returncode, output). Raises TransportError."""
        b64 = base64.b64encode(("$ProgressPreference='SilentlyContinue';" + script)
                               .encode("utf-16-le")).decode()
        r = self._run([self._exe("ssh"), "-o", "ConnectTimeout=45", "-o", "BatchMode=yes",
                       "-o", "ServerAliveInterval=30", self.host,
                       "powershell", "-NoProfile", "-EncodedCommand", b64], timeout)
        return r.returncode, (r.stdout or "").replace("\x00", "") + "\n" + (r.stderr or "")

    def scp_to(self, local: str, remote_win: str, timeout: float = 120) -> bool:
        r = self._run([self._exe("scp"), "-o", "ConnectTimeout=45", "-o", "BatchMode=yes",
                       local, "%s:%s" % (self.host, remote_win)], timeout)
        return r.returncode == 0

    def scp_from(self, remote_win: str, local: str, timeout: float = 120) -> bool:
        r = self._run([self._exe("scp"), "-o", "ConnectTimeout=45", "-o", "BatchMode=yes",
                       "%s:%s" % (self.host, remote_win), local], timeout)
        return r.returncode == 0 and os.path.exists(local) and os.path.getsize(local) > 0


# -- the grade ------------------------------------------------------------------------------------

def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


def _run_id(instance: str, patch: bytes, nonce: str) -> str:
    # alphanumeric only (used as a swebench run_id / dir name); unique per (instance, patch,
    # invocation) so a stale verdict file from an earlier grade can never be read as this one's.
    h = hashlib.md5(patch + nonce.encode()).hexdigest()[:10]
    return "r" + re.sub(r"[^A-Za-z0-9]", "", instance) + h


def grade(instance: str, patch_file: str, transport, timeout: float = DEFAULT_TIMEOUT,
          host: str = "", clock=time.monotonic, nonce: str = "") -> GradeResult:
    t0 = clock()

    def done(resolved, verdict, cls, reason="", detail=""):
        return GradeResult(instance, resolved, verdict, round(clock() - t0, 2), cls, reason,
                           _scrub(detail, host))

    if not re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+", instance or ""):
        return done(False, "", INFRA, "bad_input", "instance id shape is not owner__repo-number")
    try:
        with open(patch_file, "rb") as f:
            patch = f.read()
    except OSError as exc:
        return done(False, "", INFRA, "bad_input", "cannot read patch file: " + type(exc).__name__)

    rid = _run_id(instance, patch, nonce or str(time.time_ns()))
    remote_patch_win = "%s/%s.patch" % (REMOTE_DIFFS_WIN, rid)
    remote_patch_wsl = "%s/%s.patch" % (REMOTE_DIFFS_WSL, rid)
    remote_verdict = "%s/%s.verdict" % (REMOTE_VERDICTS_WIN, rid)
    hold = max(120, int(timeout))

    try:
        # stage: make the diffs dir (idempotent), then copy the patch
        rc, out = transport.ps("New-Item -ItemType Directory -Force '%s' | Out-Null; 'ok'"
                               % REMOTE_DIFFS_WIN, 90)
        if "ok" not in [ln.strip() for ln in out.splitlines()]:
            return done(False, "", INFRA, "unreachable", "stage command gave no answer (rc=%s): %s" % (rc, out))
        if not transport.scp_to(patch_file, remote_patch_win):
            return done(False, "", INFRA, "scp_failed", "patch upload failed")

        # run in ONE held session; the runner writes <rid>.verdict on the Windows side
        body = "bash %s %s %s %s" % (RUNNER_WSL, instance, remote_patch_wsl, rid)
        script = ("$j = Start-Job { (wsl.exe -d %s -u root -- bash -lc \"%s\" 2>$null) -join '' }; "
                  "if(Wait-Job $j -Timeout %d){ Receive-Job $j; 'HELD_DONE' } else { 'HELD_TIMEOUT' }; "
                  "Remove-Job $j -Force" % (DISTRO, body, hold))
        rc, out = transport.ps(script, hold + 90)
        if "HELD_TIMEOUT" in out:
            return done(False, "", INFRA, "timeout", "grade exceeded %d s" % hold)

        # read the verdict FILE back (scp is reliable where remote grep output is dropped)
        tf = tempfile.NamedTemporaryFile(suffix=".verdict", delete=False)
        tf.close()
        try:
            got = transport.scp_from(remote_verdict, tf.name)
            content = ""
            if got:
                try:
                    content = open(tf.name, encoding="utf-8", errors="replace").read()
                except OSError:
                    content = ""
        finally:
            try:
                os.unlink(tf.name)
            except OSError:
                pass
    except TransportError as exc:
        return done(False, "", INFRA, exc.kind, exc.detail or exc.kind)

    if not got or "RUNNER_DONE" not in content:
        return done(False, "", INFRA, "malformed",
                    "no complete verdict file (wsl distro or dockerd may be down); host said: " + out)
    m = re.search(r"VERDICT=([A-Za-z]+)", content)
    verdict = m.group(1) if m else ""
    if _V.is_resolved(verdict):
        return done(True, verdict, NONE)
    if _V.normalise(verdict) == "NOT":
        return done(False, verdict, GRADED_FAIL)
    if verdict and not _V.is_measurement(verdict):
        return done(False, verdict, INFRA, "evalerr", "grader ran but produced no report")
    return done(False, verdict, INFRA, "malformed", "unrecognised verdict text: " + content)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("instance")
    ap.add_argument("patch_file")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--json", action="store_true", help="print the result as one JSON line")
    a = ap.parse_args(argv)
    host = configured_host()
    if not host:
        res = GradeResult(a.instance, False, "", 0.0, INFRA, "not_configured",
                          "set EVAL_SSH_HOST (or SWE_EVAL_HOST) in .env or the environment; nothing was sent")
    else:
        res = grade(a.instance, a.patch_file, Transport(host), a.timeout, host=host)
    print(res.to_json() if a.json else
          "%s resolved=%s verdict=%s class=%s%s seconds=%.1f%s" % (
              res.instance, res.resolved, res.verdict or "-", res.error_class,
              "/" + res.infra_reason if res.infra_reason else "", res.seconds,
              (" detail=" + res.detail) if res.detail else ""))
    return 0 if res.resolved else (1 if res.error_class == GRADED_FAIL else 2)


if __name__ == "__main__":
    sys.exit(main())

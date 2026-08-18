"""Guardrail primitives for the autonomous self-improvement loop.

Each guard encodes one piece of the judgment that, on 2026-06-21, a human supplied by hand while
driving the measure->diagnose->propose->validate->keep/revert loop. They are deliberately small,
pure where possible, and unit-tested (test_guards.py) so the loop driver (task #23) can compose them
without re-deriving the discipline.

  1. BurnedRegistry        -- instances used for diagnosis/A/B never re-used for score or A/B
  2. overfit_lint          -- reject scaffold edits that name a specific repo/instance/file/test
  3. mcnemar_exact_p /
     significance_gate     -- keep a change only if the paired A/B is significant AND positive
  4. classify_outcome /
     partition_outcomes    -- separate infra faults from real misses; only real feed diagnosis
  5. proc_alive /
     launch_detached /
     done_after_last_start -- process discipline learned the hard way this session
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from typing import Iterable

# --------------------------------------------------------------------------------------------------
# 1. Burned-instance registry
# --------------------------------------------------------------------------------------------------

_DEFAULT_BURNED = os.path.join(os.path.dirname(__file__), "burned.jsonl")


class BurnedRegistry:
    """Append-only ledger of instances that have been *seen* (diagnosed or A/B'd).

    Anything in here is excluded from future headline-score claims and from future A/B slices, so the
    loop can never quietly score itself on data it already learned from -- the failure that makes the
    burned Lite-300 unusable as a headline (cf. feedback_no_benchmark_overfitting).
    """

    def __init__(self, path: str = _DEFAULT_BURNED):
        self.path = path
        self._seen: set[str] = set()
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._seen.add(json.loads(line)["instance_id"])
                    except Exception:
                        pass

    def add(self, instances: Iterable[str], reason: str, ts: int | None = None) -> int:
        """Burn instances with a reason. Returns the count of newly-burned (already-burned skipped)."""
        new = [i for i in instances if i not in self._seen]
        if new:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                for i in new:
                    rec = {"instance_id": i, "reason": reason}
                    if ts is not None:
                        rec["ts"] = ts
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._seen.update(new)
        return len(new)

    def is_burned(self, instance_id: str) -> bool:
        return instance_id in self._seen

    def filter_fresh(self, candidates: Iterable[str]) -> list[str]:
        """Return only the candidates that have not been burned (order preserved, de-duplicated)."""
        out, seen = [], set()
        for c in candidates:
            if c not in self._seen and c not in seen:
                out.append(c)
                seen.add(c)
        return out

    def __len__(self) -> int:
        return len(self._seen)


# --------------------------------------------------------------------------------------------------
# 2. Overfit linter
# --------------------------------------------------------------------------------------------------

# Patterns that mean a scaffold edit has leaked instance-specific knowledge. A domain-general lesson
# never needs to name a concrete repo, instance id, source file, or test.
_OVERFIT_PATTERNS = [
    ("instance_id", re.compile(r"\b[a-z][\w-]*__[\w.-]+-\d+\b")),          # django__django-12345
    ("source_path", re.compile(r"\b[\w./\\-]+\.(?:py|pyx|js|ts|c|cc|cpp|h|hpp|java|go|rb|rs)\b")),
    ("test_name", re.compile(r"\btest_[A-Za-z0-9_]+\b")),
    ("dunder_test", re.compile(r"\b[A-Za-z0-9_]+::test[A-Za-z0-9_]*\b")),
]
# Specific OSS repos that recur in SWE-bench; naming one in a "general" card is a red flag.
_REPO_DENYLIST = (
    "django", "sympy", "sphinx", "astropy", "matplotlib", "scikit-learn", "sklearn",
    "pytest", "pylint", "requests", "flask", "seaborn", "xarray", "pydata", "pallets",
)


def overfit_lint(text: str) -> list[str]:
    """Return a list of overfit violations found in proposed scaffold text. Empty list == clean."""
    violations: list[str] = []
    for label, pat in _OVERFIT_PATTERNS:
        for m in pat.findall(text or ""):
            violations.append("%s:%s" % (label, m))
    low = (text or "").lower()
    for repo in _REPO_DENYLIST:
        # word-boundary match so 'requests' the word survives but not as a bare repo name token
        if re.search(r"\b" + re.escape(repo) + r"\b", low):
            violations.append("repo_name:%s" % repo)
    # de-duplicate, keep order
    seen, out = set(), []
    for v in violations:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def is_domain_general(text: str) -> bool:
    """True iff the text carries no instance-specific leakage."""
    return not overfit_lint(text)


# --------------------------------------------------------------------------------------------------
# 3. Significance gate (paired A/B)
# --------------------------------------------------------------------------------------------------

def mcnemar_exact_p(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value on the discordant pair counts b, c.

    b = ON-resolved & OFF-unresolved (the change helped); c = the reverse (the change hurt).
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2.0 * tail)


def significance_gate(on_resolved: Iterable[str], off_resolved: Iterable[str],
                      instances: Iterable[str], alpha: float = 0.05,
                      min_n: int = 100, min_pp: float = 1.0) -> dict:
    """Decide keep/revert for a paired A/B over the SAME instance set.

    Keeps the change only when it is statistically significant (McNemar p < alpha), positive in
    direction, of at least min_pp percentage points, and measured on at least min_n paired
    instances. The 2026-06-21 case (+6.2pp, p=0.18, N=80) must return keep=False with verdict
    'suggestive' -> the correct action is "enlarge N", not "commit".
    """
    ids = list(instances)
    on, off = set(on_resolved), set(off_resolved)
    n = len(ids)
    both = sum(1 for i in ids if i in on and i in off)
    b = sum(1 for i in ids if i in on and i not in off)       # helped
    c = sum(1 for i in ids if i not in on and i in off)       # hurt
    neither = n - both - b - c
    n_on = sum(1 for i in ids if i in on)
    n_off = sum(1 for i in ids if i in off)
    p = mcnemar_exact_p(b, c)
    net_pp = 100.0 * (n_on - n_off) / n if n else 0.0
    positive = n_on > n_off

    if n < min_n:
        keep, verdict = False, "underpowered"
        reason = "N=%d < min_n=%d; enlarge the fresh slice before deciding" % (n, min_n)
    elif b == 0 and c == 0:
        # ZERO DISCORDANT PAIRS CARRY NO INFORMATION, and calling that a verdict on the
        # candidate is the error this gate exists to prevent, pointing the other way.
        #
        # McNemar reads the pairs that DISAGREED; concordant pairs cancel and tell you
        # nothing about the difference. With b=c=0 the test has no power at all -- "the change
        # did nothing" and "the sample could not have detected anything" are the same
        # observation, and only one of them is a statement about the candidate.
        #
        # It fell through to `non-positive` before, which reads as "the measurement says this
        # is not an improvement". A live run produced exactly that: four episodes, all four
        # passing on both arms, reported as REJECT. The prediction written before the run said
        # INCONCLUSIVE, and the prediction was right.
        #
        # `n < min_n` did not catch it because that counts PAIRS, and four pairs clears any
        # small threshold. The quantity that has to be large enough is the discordant count.
        keep, verdict = False, "underpowered"
        reason = ("N=%d but no pair disagreed (helped 0 / hurt 0): McNemar has nothing to "
                  "work with, so this says the sample could not detect a difference, not "
                  "that there is none" % n)
    elif not positive:
        keep, verdict = False, "non-positive"
        reason = "net %+.1f pp is not an improvement" % net_pp
    elif p >= alpha:
        keep, verdict = False, "suggestive"
        reason = ("direction positive (%+.1f pp, helped %d / hurt %d) but McNemar p=%.3f >= %.2f; "
                  "enlarge N" % (net_pp, b, c, p, alpha))
    elif net_pp < min_pp:
        keep, verdict = False, "negligible"
        reason = "significant (p=%.3f) but net %+.1f pp < min_pp=%.1f" % (p, net_pp, min_pp)
    else:
        keep, verdict = True, "keep"
        reason = "significant (+%.1f pp, p=%.3f, helped %d / hurt %d, N=%d)" % (net_pp, p, b, c, n)

    return {"keep": keep, "verdict": verdict, "reason": reason, "p": p, "net_pp": net_pp,
            "n": n, "b": b, "c": c, "both": both, "neither": neither,
            "n_on": n_on, "n_off": n_off, "alpha": alpha, "min_n": min_n, "min_pp": min_pp}


# --------------------------------------------------------------------------------------------------
# 4. Infra-vs-real outcome classifier
# --------------------------------------------------------------------------------------------------

# Signatures that mean a non-resolved outcome was an infrastructure fault, NOT a real code miss.
# These must never become a scaffold "lesson" (cf. project_swe_eval_host_confound).
_INFRA_SIGNATURES = (
    "evalerr", "eval_error", "eval error",
    "banner exchange timeout", "connection reset", "connection refused", "timed out",
    "disk", "no space", "floor", "admission",
    "consent", "unlock", "permission", "書き込む内容を教えて",  # MCP/UI consent-card stall
    "0xc0000142", "create_new_process", "desktop heap",
    "capture failed", "model_patch: null", "model_patch null",
    "docker", "image build", "containererror",
)


def classify_outcome(verdict: str, log_tail: str = "", patch: str | None = None) -> str:
    """Classify a graded outcome as 'resolved', 'infra', or 'real'.

    - 'resolved': the verdict says RESOLVED.
    - 'infra'   : verdict/log indicates an eval-host / harness / process / capture fault.
    - 'real'    : a genuine wrong / underfit / regressed patch -> the only bucket diagnosis uses.
    """
    v = (verdict or "").strip().lower()
    if v in ("resolved", "pass", "passed"):
        return "resolved"
    blob = (verdict or "") + "\n" + (log_tail or "")
    blob_low = blob.lower()
    if v in ("evalerr", "error"):
        return "infra"
    if patch is not None and not str(patch).strip():
        # an empty captured patch is a capture/solve-infra fault, not a graded code miss
        return "infra"
    for sig in _INFRA_SIGNATURES:
        if sig in blob_low or sig in blob:
            return "infra"
    return "real"


def partition_outcomes(records: Iterable[dict]) -> dict:
    """Partition graded records into resolved / real_miss / infra id-lists.

    Each record: {"instance_id", "verdict", optional "log_tail", optional "patch"}.
    """
    out = {"resolved": [], "real_miss": [], "infra": []}
    for r in records:
        cls = classify_outcome(r.get("verdict", ""), r.get("log_tail", ""), r.get("patch"))
        key = {"resolved": "resolved", "infra": "infra", "real": "real_miss"}[cls]
        out[key].append(r["instance_id"])
    return out


# --------------------------------------------------------------------------------------------------
# 5. Process discipline
# --------------------------------------------------------------------------------------------------

def proc_alive(cmdline_substr: str) -> int:
    """Count live processes whose command line contains cmdline_substr.

    Uses psutil's real cmdline (NOT `tasklist /FI "PID eq"`), because a venv `python.exe` is a shim
    that re-launches the global interpreter under a different pid -- a PID filter on the shim pid
    reports a false "died", which mis-drove this session three times.
    """
    try:
        import psutil
    except Exception:
        return _proc_alive_cim(cmdline_substr)
    n = 0
    for p in psutil.process_iter(["cmdline"]):
        try:
            cl = " ".join(p.info.get("cmdline") or [])
        except Exception:
            continue
        if cmdline_substr in cl:
            n += 1
    return n


def _proc_alive_cim(cmdline_substr: str) -> int:
    """Fallback liveness via PowerShell CIM when psutil is unavailable."""
    esc = cmdline_substr.replace("'", "''")
    ps = ("(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
          "$_.CommandLine.Contains('%s') } | Measure-Object).Count" % esc)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
        return int((r.stdout or "0").strip() or "0")
    except Exception:
        return 0


def launch_detached(args: list[str], cwd: str, stdout_path: str, stderr_path: str) -> int:
    """Launch a durable background process that survives this shell / the harness reaper.

    The python equivalent of `Start-Process -WindowStyle Hidden`: a new process group, detached, no
    console. Git Bash `nohup &` and harness `run_in_background`+`exec` both got reaped this session;
    this does not. Returns the child pid.
    """
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)
    os.makedirs(os.path.dirname(stdout_path) or ".", exist_ok=True)
    out = open(stdout_path, "ab")
    err = open(stderr_path, "ab")
    p = subprocess.Popen(args, cwd=cwd, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                         creationflags=creationflags, close_fds=True)
    return p.pid


def done_after_last_start(log_path: str, start_marker: str, done_marker: str) -> bool:
    """True iff `done_marker` appears AFTER the last `start_marker` in an append-reused log.

    A log file reused across runs keeps the previous run's done line; a naive grep sees it and
    declares the *current* run finished. This anchors detection to the most recent run-start.
    """
    if not os.path.isfile(log_path):
        return False
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return False
    last_start = -1
    for i, ln in enumerate(lines):
        if start_marker in ln:
            last_start = i
    if last_start < 0:
        return False
    return any(done_marker in ln for ln in lines[last_start:])

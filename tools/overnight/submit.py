"""Hand one goal to the fleet the way a person does: scripts/win/submit_via_ui.ps1, one call per item.

Only that route is used (no API, no fleet internals). A failed call is retried with exponential
backoff; before each retry the fleet is asked whether the goal landed anyway (a timed-out call
can still have been accepted), so a retry can not create a second copy.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tools import childproc
from tools.overnight.items import MAX_GOAL_CHARS

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SCRIPT = REPO / "scripts" / "win" / "submit_via_ui.ps1"

BACKOFF_SECONDS = (10, 40, 120)
CALL_TIMEOUT_S = 420

_DUP_RE = re.compile(r"DUP_SIMILARITY|duplicate|near-duplicate|重複|既に投入|同じ目標|not accepted", re.I)


@dataclass
class SubmitResult:
    ok: bool = False
    duplicate: bool = False
    landed_anyway: bool = False
    attempts: int = 0
    output: str = ""


def check_goal(goal: str) -> None:
    if "\n" in goal or "\r" in goal:
        raise ValueError("a goal must be a single line")
    if not goal.strip() or len(goal) > MAX_GOAL_CHARS:
        raise ValueError("goal length out of range: %d" % len(goal))


def _real_runner(cmd, timeout):
    kw = {"timeout": timeout}
    try:
        kw["creationflags"] = childproc.headless_creationflags()
    except Exception:
        pass
    try:
        p = childproc.run(cmd, **kw)
    except subprocess.TimeoutExpired:
        return 124, "timeout after %ss" % timeout
    except OSError as e:
        return 127, "cannot start: %s" % e
    return p.returncode, (p.stdout or "") + (p.stderr or "")


class Submitter:
    def __init__(self, script=None, runner=None, sleep=None, log=None, backoff=BACKOFF_SECONDS,
                 timeout=CALL_TIMEOUT_S):
        self.script = str(script or DEFAULT_SCRIPT)
        self.runner = runner or _real_runner
        self.sleep = sleep or (lambda s: None)
        self.log = log or (lambda m: None)
        self.backoff = tuple(backoff)
        self.timeout = timeout

    def _cmd(self, *extra):
        return ["powershell.exe", "-NoProfile", "-File", self.script, *extra]

    def submit(self, goal: str, probe=None) -> SubmitResult:
        """probe(): True when the fleet already shows this goal (landed despite an error)."""
        check_goal(goal)
        res = SubmitResult()
        tries = len(self.backoff) + 1
        for n in range(tries):
            res.attempts = n + 1
            rc, out = self.runner(self._cmd("-Goal", goal), self.timeout)
            res.output = (out or "")[-2000:]
            if rc == 0:
                res.ok = True
                return res
            if _DUP_RE.search(out or ""):
                res.duplicate = True
                return res
            self.log("submit attempt %d failed (rc=%s): %s" % (n + 1, rc, res.output[-300:].replace("\n", " ")))
            if probe is not None:
                try:
                    if probe():
                        res.ok = res.landed_anyway = True
                        return res
                except Exception:
                    pass
            if n < len(self.backoff):
                self.sleep(self.backoff[n])
        return res

    def readonly_check(self) -> str:
        """The cockpit's own view after a submit (-ReadOnly prints it; nothing is changed)."""
        try:
            rc, out = self.runner(self._cmd("-ReadOnly"), 120)
            return "rc=%s %s" % (rc, (out or "")[-400:].replace("\n", " | "))
        except Exception as e:  # never let a diagnostic stop the batch
            return "readonly check failed: %s" % e

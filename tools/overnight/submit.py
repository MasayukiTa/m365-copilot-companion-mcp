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


@dataclass
class SubmitResult:
    ok: bool = False
    duplicate: bool = False
    landed_anyway: bool = False
    attempts: int = 0
    job_id: str = ""
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
            # A near-duplicate reply is no longer a special case: the fleet door does not refuse
            # on content resemblance (2026-10-26/27 owner instruction), so there is nothing to
            # detect here. A non-zero rc is a real submit failure and is retried/reported as one.
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


_QUEUED_RE = re.compile(r"^queued\s+([0-9a-f]{6,32})\b")


class QueueSubmitter:
    """Night route: write the goal into the fleet's pending queue through fleet_submit().

    Same interface as Submitter. Nothing here needs the screen: a locked desktop, which stops
    the GUI route, does not matter. fleet_submit's own refusals (duplicate, too long, queue
    full) come back as text and are mapped here: a similar-goal refusal is a duplicate (the
    runner rewords once), everything else is a failure with the door's message kept.
    """

    def __init__(self, fn=None, log=None, source="overnight batch"):
        self._fn = fn
        self.log = log or (lambda m: None)
        self.source = source

    def _submit_fn(self):
        if self._fn is None:
            from tools.fleet_intake import fleet_submit  # heavy import: only when used
            self._fn = fleet_submit
        return self._fn

    def submit(self, goal: str, probe=None) -> SubmitResult:
        check_goal(goal)
        res = SubmitResult(attempts=1)
        try:
            text = str(self._submit_fn()(goal, note="overnight batch", source=self.source))
        except Exception as e:  # the door must never stop the batch
            res.output = "fleet_submit raised %s: %s" % (type(e).__name__, e)
            return res
        res.output = text[-2000:]
        m = _QUEUED_RE.match(text.strip())
        if m:
            res.ok, res.job_id = True, m.group(1)
        else:
            # The door no longer refuses a goal because it resembles one in flight
            # (2026-10-26/27 owner instruction), so there is no similar-goal reply to map to a
            # duplicate here. Anything that is not a queued-ok line is a real failure (too long,
            # queue full, raised) and keeps the door's own message.
            self.log("queue refused: %s" % text[:300])
        return res

    def readonly_check(self) -> str:
        return "queue route (no cockpit call)"


# fleet_similarity() was removed: it exposed the door's content-resemblance rule so the runner
# could hold a request back until a similarly-worded one finished. The door no longer refuses on
# content (2026-10-26/27 owner instruction), so there is nothing to mirror and nothing to hold
# back for -- every request is submitted as written.

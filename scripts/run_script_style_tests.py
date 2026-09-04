"""Run the test files pytest collects NOTHING from, and hold their results to a baseline.

WHY THIS EXISTS

21 of the 166 files listed in CI's pytest step defined no `test_*` function. They are
script-style suites -- they build their own `results` list, print `=== N/M checks passed ===`
and exit non-zero on failure -- and pytest imports them, collects zero items, and moves on.
CI was green. It had never executed them.

That was not a small gap. `relay/test_acceptance.py` alone runs 20 checks; the twenty files
together run several hundred, covering the relay loop, the planner, the watchdog, transient
retry, and the unlock injection path. And four of them were RED, with seven failing checks
nobody had seen -- including a kill-switch that never engaged, so a loop asked to abort ran
to completion and finished STUCK, and a per-worker retry budget that bounded nothing while
the evolution loop was free to tune it. All seven are fixed and every file is green.

WHY THE BASELINE MECHANISM SURVIVES ANYWAY

Turning CI red on seven pre-existing failures would have made it stop being read, which is
how this happened in the first place; turning them off would have repeated the original
mistake with extra steps. So each file carries the number of checks it passes:

  * a file expected to pass completely must exit 0. No baseline, no leniency.
  * a file with known failures must produce EXACTLY its recorded count. Fewer is a
    regression. More is someone having fixed something, which is good news and still fails
    -- because a baseline that silently shrinks stops being a record of anything.

Every entry is currently None, and the machinery is kept rather than deleted: the next
suite added here may not be green, and a recorded number is how it says so out loud instead
of being quietly excluded. A number is a debt, never a decision.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: path -> expected passing checks, or None for "must pass completely".
#:
#: EVERY ENTRY IS None AS OF 2026-08-18, and that is the point: when CI first ran
#: these files, four of them were red with seven failing checks. All seven are now
#: fixed, and the mechanism is kept rather than deleted because the next file added
#: here may not be green, and a number is how it says so out loud.
#:
#: What the seven were:
#:   relay/test_relay_loop.py     the kill-switch went through an HTTP authorisation
#:                                predicate that denies in-process callers, so it
#:                                never engaged
#:   relay/test_transient.py      the per-worker retry budget bounded nothing; it
#:                                survived only inside "retry %d/%d"
#:   relay/test_fleet_verify.py   same cause -- "retries exhausted" was expressed as
#:                                max_transient=0, which stopped meaning anything
#:                                when the transport budget became a time window
#:   relay/test_unlock_inject.py  asserted a redaction marker no code has ever
#:                                emitted. The password itself WAS removed, so a
#:                                stale expectation rather than a leak
SUITES = {
    "relay/test_acceptance.py": None,
    "relay/test_autoscale.py": None,
    "relay/test_code_task.py": None,
    "relay/test_continue_escalation.py": None,
    "relay/test_feedback.py": None,
    "relay/test_fleet_refute.py": None,
    "relay/test_fleet_runner_fixes.py": None,
    "relay/test_fleet_verify.py": None,
    "relay/test_folder_verify.py": None,
    "relay/test_forge.py": None,
    "relay/test_planner.py": None,
    "relay/test_project_memory.py": None,
    "relay/test_recycle.py": None,
    "relay/test_refuter.py": None,
    "relay/test_refuter_memory.py": None,
    "relay/test_relay_loop.py": None,
    "relay/test_repo_map.py": None,
    "relay/test_transient.py": None,
    "relay/test_unlock_inject.py": None,
    "relay/test_watchdog.py": None,
}

#: Per-suite budget. relay/test_fleet_refute.py is the slowest at a few seconds; the
#: headroom is for a loaded machine, not for a suite that hangs.
TIMEOUT_S = 600

_COUNT = re.compile(r"===\s*(\d+)\s*/\s*(\d+)[^=]*passed\s*===")


def run_one(rel: str, expected, timeout: int = None):
    # A TIMEOUT IS A RESULT, NOT A CRASH. subprocess.run raises TimeoutExpired, and with
    # nothing catching it the whole gate died on a traceback -- so every suite after the slow
    # one never ran at all, and the output said nothing about which suite was slow or that
    # the rest had been skipped. Observed on relay/test_fleet_refute.py, which passes 12/12
    # when given more than 600s: the gate reported a stack trace from subprocess and the
    # suites below it were quietly not tested.
    #
    # The encoding is explicit for the reason set out in scripts/check_integration_evidence
    # ._git: text=True alone decodes the child with the locale codec, and these suites print
    # Japanese.
    try:
        proc = subprocess.run([sys.executable, str(ROOT / rel)], cwd=str(ROOT),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=TIMEOUT_S if timeout is None else timeout)
    except subprocess.TimeoutExpired:
        return False, ("%s TIMED OUT after %ds. It was not run to completion, so nothing it "
                       "covers has been checked."
                       % (rel, TIMEOUT_S if timeout is None else timeout))
    out = (proc.stdout or "") + (proc.stderr or "")
    hit = _COUNT.search(out)
    passed, total = (int(hit.group(1)), int(hit.group(2))) if hit else (None, None)

    if expected is None:
        if proc.returncode == 0:
            return True, "%s ok (%s)" % (rel, "%d/%d" % (passed, total) if hit else "exit 0")
        tail = "\n      ".join(l for l in out.splitlines() if l.startswith("[FAIL]"))
        return False, ("%s FAILED (exit %d)\n      %s"
                       % (rel, proc.returncode, tail or out.strip()[-300:]))

    if passed is None:
        return False, ("%s has a recorded baseline of %d but printed no count line -- the "
                       "suite changed shape, so the baseline no longer means anything"
                       % (rel, expected))
    if passed == expected:
        return True, "%s at its recorded baseline (%d/%d; %d known failures)" % (
            rel, passed, total, total - passed)
    if passed < expected:
        return False, ("%s REGRESSED: %d/%d, was %d. Something that used to pass no longer "
                       "does" % (rel, passed, total, expected))
    return False, ("%s IMPROVED: %d/%d, baseline says %d. Good news -- update the baseline "
                   "in this file so it keeps being a record of what is actually broken"
                   % (rel, passed, total, expected))


def main() -> int:
    bad = []
    for rel, expected in SUITES.items():
        if not (ROOT / rel).is_file():
            bad.append("%s is listed here but does not exist" % rel)
            continue
        ok, message = run_one(rel, expected)
        print(("  " if ok else "  ! ") + message)
        if not ok:
            bad.append(message)

    known = sum(1 for v in SUITES.values() if v is not None)
    print()
    if bad:
        print("script-style suites: %d failing" % len(bad))
        return 1
    print("script-style suites OK: %d run, %d carrying recorded failures" % (len(SUITES), known))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

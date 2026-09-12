"""Trivial, Docker-FREE acceptance gate for the DECOUPLED solve phase.

In the decoupled architecture (solve all 300 locally, then batch-grade the diffs on the
the eval host eval host), the fleet must NOT run the local swebench Docker eval during solving --
that is what filled C: on this 16 GB box (the hardware wall). So the solve-phase goals use
THIS gate instead of swe_check.py: it accepts the agent's DONE iff it actually edited source
(a non-empty `git diff`), and otherwise tells it no patch exists yet. Correctness is the
agent's own job here -- the strong-scaffold red->green self-test (SWE_STRONG_SELFTEST=1) is
what verifies the fix; the hidden tests are applied later, once, on the eval host.

    python bench/swe_diffgate.py <worktree_dir>
Exit 0 = a non-empty diff exists (DONE accepted). Exit 1 = no edits yet (keep working).
"""
import subprocess
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: swe_diffgate.py <worktree_dir>", file=sys.stderr)
        return 1
    wt = sys.argv[1]
    try:
        # UTF-8 FIRST, THEN THE CODE PAGE, NEVER RAISING. `text=True` here decoded a PATCH
        # with cp932 and one byte deleted the whole diff -- the incident that cost 60 solved
        # instances (4ef0d31). Routed through the shared policy so this file cannot drift
        # from the rest of the sweep.
        from tools.childproc import run as _run_child
        diff = _run_child(["git", "-C", wt, "diff"], timeout=60).stdout
    except Exception as e:
        print("DIFFGATE_ERROR: could not read git diff at %s: %s" % (wt, e), file=sys.stderr)
        return 1
    if diff.strip():
        print("PATCH_PRESENT: your edits are captured. (Final correctness is checked later by the "
              "hidden tests -- make sure your own red->green reproducer passes before DONE.)")
        return 0
    print("NO_PATCH_YET: you have not edited any source files at %s. Read the relevant source, "
          "write a reproducer that FAILS on the bug, fix the source until it passes, then DONE." % wt)
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""Trivial, Docker-FREE acceptance gate for the DECOUPLED solve phase.

In the decoupled architecture (solve all 300 locally, then batch-grade the diffs on the
kiyus eval host), the fleet must NOT run the local swebench Docker eval during solving --
that is what filled C: on this 16 GB box (the hardware wall). So the solve-phase goals use
THIS gate instead of swe_check.py: it accepts the agent's DONE iff it actually edited source
(a non-empty `git diff`), and otherwise tells it no patch exists yet. Correctness is the
agent's own job here -- the strong-scaffold red->green self-test (SWE_STRONG_SELFTEST=1) is
what verifies the fix; the hidden tests are applied later, once, on kiyus.

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
        diff = subprocess.run(["git", "-C", wt, "diff"], capture_output=True, text=True,
                              timeout=60).stdout
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

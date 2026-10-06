# -*- coding: utf-8 -*-
"""The tree the guards scan must be the tree that gets pushed.

WHAT THIS IS FOR. A dozen guards in this repository sweep the repository and hold a ratchet
against what they find -- the locale-decode inventory, the backslash-literal check, the
windowless-launch inventory, the unreached-function burndown, the UI build list, the hermetic
test manifest. Every one of them filters against `git ls-files`, which is the INDEX, and every
one of them does so for a good reason: CI only ever sees tracked files, so sweeping a
local-only file would make a guard fail for something CI cannot reproduce.

The cost of that correct decision is that **a file which has not been `git add`ed is invisible
to all of them at once**. Write a new module, run the suite, get green, `git add`, commit,
push -- and the guards meet the file for the first time on the runner.

MEASURED, TWICE, THE SAME WEEK.

  * 2026-09-14: a new test file was written, `preflight` reported all gates passed, and CI
    failed on "test files missing from CI and EXCLUDED" naming that file. The manifest audit
    gained `--strict-untracked` for exactly this, and preflight passes it.
  * 2026-09-22: `scripts/test_a_crash_is_not_a_verdict.py` was written with
    `subprocess.run(..., text=True)`, the suite was green here, and CI's decode ratchet failed
    on it. `--strict-untracked` did not help: it is the MANIFEST's flag, and the manifest is
    one guard out of a dozen.

So the first fix was right and too narrow. The hazard is not "an untracked test file"; it is
"an untracked file under the roots the guards sweep", and it is one `git ls-files --others`
call away from being visible.

WHAT IT DOES NOT DO. It does not run the guards, and it does not want the file added -- an
untracked file is a perfectly normal state while something is being written. It refuses to let
a PUSH happen with one outstanding, which is the single moment where "the guards saw a
different tree" turns into a red main.
"""
from __future__ import annotations

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

#: The directories the sweeping guards walk. Kept in step with tools/ and relay/'s own ROOTS
#: tuples; a root that is swept by a guard and missing here is a blind spot in this check.
ROOTS = ("relay", "bench", "tools", "scripts", "bridge", "ui", "tests")

#: Extensions a guard actually parses. A new .md or .json is not something any ratchet reads,
#: and failing on one would be this check crying wolf -- which is how a signal stops being read.
SUFFIXES = (".py", ".cs", ".ps1", ".bat", ".cmd")


def _git(*args):
    """Through tools.childproc, like every other guard here.

    Two ratchets have something to say about a bare `subprocess.run`: it decodes the child with
    the local code page (cp932 here, so one bad byte takes the whole of stdout), and it is a
    new launch site with no decision recorded about the console it inherits. Both fired on this
    file the moment it was staged -- which is, in the smallest possible way, the thing this
    file exists to say.
    """
    from tools.childproc import run as _run_child
    try:
        r = _run_child(["git"] + list(args), cwd=REPO, timeout=120)
    except OSError as exc:
        # git missing from PATH entirely is a plain OSError from CreateProcess, with nothing
        # between it and the caller unless caught here -- the same shape as the sibling guard
        # (check_no_identifying_names.py), and it used to reach the console as a bare
        # traceback instead of the readable "git ... failed" message the line below already
        # gives for a git that runs and fails.
        raise SystemExit("check_nothing_new_is_invisible: could not run git: %s" % exc) from exc
    if r.returncode != 0:
        raise SystemExit("check_nothing_new_is_invisible: git %s failed: %s"
                         % (" ".join(args), (r.stderr or "").strip()))
    return [p for p in (r.stdout or "").split("\n") if p.strip()]


def invisible():
    """Files that exist, are under a swept root, and are not in the index.

    `--exclude-standard` keeps gitignored files out: those are invisible to CI too, on purpose,
    and the repository's rule is that what lives under an ignore is never transcribed into the
    tracked tree anyway.
    """
    out = []
    for rel in _git("ls-files", "--others", "--exclude-standard"):
        head = rel.split("/", 1)[0]
        if head in ROOTS and rel.endswith(SUFFIXES):
            out.append(rel)
    return sorted(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero when anything is invisible. For the pre-push path; run "
                         "by hand while writing code, a NOTE is the right volume.")
    a = ap.parse_args(argv)

    found = invisible()
    if not found:
        print("nothing new is invisible: every source file under %s is in the index."
              % ", ".join(ROOTS))
        return 0

    print("these files exist but are NOT in the git index, so every guard that filters against")
    print("`git ls-files` -- the decode inventory, the backslash check, the launch-site")
    print("inventory, the UI build list, the hermetic test manifest -- is sweeping a tree that")
    print("does not contain them. They will be swept for the first time on CI:")
    for rel in found:
        print("  - %s" % rel)
    if not a.strict:
        print("")
        print("NOTE only: this is normal while a file is being written. `git add` them and run")
        print("the suite again before pushing.")
        return 0
    print("")
    print("`git add` them, then re-run. (Deliberately not adding them? Then they are not part")
    print("of this push, and moving them out of %s keeps them out of the guards' way.)"
          % ", ".join(r + "/" for r in ROOTS))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

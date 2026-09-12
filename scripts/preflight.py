# -*- coding: utf-8 -*-
"""Everything CI runs, run here, in one command. `python scripts/preflight.py`

WHY THIS EXISTS. CI's `test` job has SIX gates and only one of them is pytest. I ran the pytest
one over six directories, called it a full suite, pushed, and CI failed on
`scripts/run_script_style_tests.py` -- a runner for twenty files that pytest collects nothing
from, because they define no `test_*` functions. I had even enumerated those twenty files
earlier that day, concluded "pytest collects nothing from them", and stopped there without
asking what else runs them. Something does.

The failure was a stale assertion in `relay/test_acceptance.py` requiring the old
silently-dropping behaviour of `normalize_checks`. Trivial to fix and impossible to see
locally, because the command that sees it is not the command I was running.

The repository already has the rule -- "local green is not CI green; when you push, do not stop
until you have seen CI" -- and following it caught this. What it could not do is make the local
check complete. This can: one command, the same gates, in CI's order.

WHAT IS DELIBERATELY NOT HERE, named rather than left as a silent gap:

  Install shipped dependencies   CI does `pip install -r requirements.txt` into a fresh runner.
                                 preflight runs under whatever interpreter invoked it, so it
                                 cannot tell whether requirements.txt still describes that
                                 environment. A stale local venv passes here and fails there.
  Secret scan / CodeQL           runner-only.
  Linux-only behaviour           the hermetic suite runs on ubuntu; a Windows-only pass proves
                                 nothing about a codec or path assumption that differs there.
                                 This is not hypothetical: cp932 is a Python codec, not a
                                 Windows code page, so a LookupError-based skip did not fire on
                                 Linux and a test that passed here went red on the runner.
  windows-install-smoke          `--windows` runs the two pytest steps of that job. The import
                                 smoke needs the server's own deps and the two Pester suites
                                 need Pester 3.4.0 and PowerShell; they are NOT run, and they
                                 are listed here so their absence is a decision rather than an
                                 oversight. An earlier version of this docstring said that job
                                 "cannot be" run locally, which was simply false -- this IS the
                                 Windows box.

So a green preflight still does not mean a green CI. It means the gap is now the four things
above instead of an unknown number of them, and the rule stands: after pushing, watch the run.

Exit code is the number of failing gates, so `python scripts/preflight.py && git push` is safe.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI = os.path.join(REPO, ".github", "workflows", "ci.yml")
PY = sys.executable


def _pytest_files():
    """The exact file list CI's `Run hermetic unit tests` step passes to pytest.

    READ FROM ci.yml, NOT RESTATED. A second copy of that list would go stale the first time
    someone adds a test, and a preflight that runs a different set than CI is worse than none:
    it would report green for a set nobody is gating on.
    """
    with open(CI, encoding="utf-8") as fh:
        text = fh.read()
    i = text.index("Run hermetic unit tests")
    block = text[i:text.index("\n      - name:", i)]
    return re.findall(r"^\s+([\w./-]+\.py)\s*\\?$", block, re.M)


def _run(label, argv, cwd=REPO):
    print("\n=== %s ===" % label, flush=True)
    t0 = time.time()
    rc = subprocess.call(argv, cwd=cwd)
    print("--- %s: %s in %.0fs" % (label, "OK" if rc == 0 else "FAILED (exit %d)" % rc,
                                   time.time() - t0), flush=True)
    return rc


def main(argv=None):
    argv = list(argv or sys.argv[1:])
    quick = "--quick" in argv          # skip the long pytest run; gates only
    windows = "--windows" in argv      # also run the windows-install-smoke job's pytest steps

    files = _pytest_files()
    if len(files) < 50:
        # A parse that silently returned a short list would make this pass by running almost
        # nothing, which is the failure mode the whole file is about.
        print("preflight: only %d test files parsed out of ci.yml -- the format changed and "
              "this script would be checking a subset. Fix _pytest_files() before trusting "
              "a green result." % len(files))
        return 1

    gates = [
        ("Audit hermetic test manifest", [PY, "scripts/check_ci_test_manifest.py"]),
        ("Integration evidence for new definitions", [PY, "scripts/check_integration_evidence.py"]),
        ("No identifying names in tracked files", [PY, "scripts/check_no_identifying_names.py", "."]),
        ("Run script-style tracer tests", [PY, "tools/test_trace.py"]),
        ("Run script-style suites", [PY, "scripts/run_script_style_tests.py"]),
    ]
    skipped = []
    if quick:
        skipped.append("Run hermetic unit tests (%d files) -- --quick" % len(files))
    else:
        gates.insert(3, ("Run hermetic unit tests (%d files)" % len(files),
                         [PY, "-m", "pytest", "-q"] + files))
    # THE WINDOWS JOB IS A SEPARATE CI JOB, NOT A SKIPPED GATE OF THIS ONE -- so its absence is
    # mentioned, not counted as an incomplete run. Listing it in `skipped` made every default
    # invocation print "NOT a green preflight", which is crying wolf: a signal that is never
    # clean stops being read, and then the one that matters (--quick dropping the pytest gate)
    # is lost in it.
    if windows:
        gates.append(("windows-install-smoke: path tests",
                      [PY, "-m", "pytest", "-q", "tools/test_file_ops.py"]))
        gates.append(("windows-install-smoke: DPAPI bootstrap",
                      [PY, "-m", "pytest", "-q", "scripts/test_bootstrap.py",
                       "scripts/test_bootstrap_p2c.py"]))

    failed = [name for name, cmd in gates if _run(name, cmd) != 0]

    print("\n" + "=" * 70)
    if failed:
        print("preflight: %d of %d gate(s) FAILED -- %s"
              % (len(failed), len(gates), ", ".join(failed)))
        print("CI runs these same gates; pushing now means watching it fail.")
    elif skipped:
        # NEVER "all gates passed" WHEN A GATE WAS SKIPPED. An audit of this file found exactly
        # that: `--quick` dropped the only gate that runs code and then printed a smaller,
        # unlabelled count that reads identically to a complete run. A preflight reporting
        # green for a subset manufactures the confidence it exists to replace.
        print("preflight: %d gate(s) passed, %d NOT RUN:" % (len(gates), len(skipped)))
        for s_ in skipped:
            print("   skipped: %s" % s_)
        print("This is NOT a green preflight. The skipped gates still run in CI.")
    else:
        print("preflight: all %d gates passed." % len(gates))
    print("A green preflight is not a green CI: `pip install -r requirements.txt` into a clean")
    print("environment, the secret scan, CodeQL and the ubuntu-only behaviour of the hermetic")
    print("suite run only on the runner. After pushing, watch the run.")
    if not windows:
        print("Not run here (separate CI job): windows-install-smoke. `--windows` adds its two")
        print("pytest steps; its import smoke and Pester suites still only run on the runner.")
    return len(failed)


if __name__ == "__main__":
    raise SystemExit(main())

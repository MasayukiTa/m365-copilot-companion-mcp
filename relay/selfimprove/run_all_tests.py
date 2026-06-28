"""Regression gate -- run EVERY self-improvement + strengths + hardening test suite in one command.

The "never silently regress" discipline made runnable: `python -m relay.selfimprove.run_all_tests`
runs each suite the way it is meant to be run -- as its own `python -m <module>` subprocess -- and
reports a single PASS/FAIL summary, exiting non-zero if any suite fails. Use it before committing any
change that could touch these modules (and post-measurement, before adopting anything into the fleet).

Why subprocess, not in-process import: some suites are invocation-coupled (e.g. test_guards asserts
the process command line contains its own name) or use pytest fixtures in their function signatures.
Running each as `python -m <module>` reproduces the exact standalone invocation under which every
suite is written to pass, so the gate measures the real thing rather than an in-process artefact.
A suite passes iff its subprocess exits 0 AND prints an "ALL ... PASSED" line.
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = r"C:\Users\USER\companion-mcp"

_SUITES = [
    # controller core (relay.selfimprove)
    "relay.selfimprove.test_guards",
    "relay.selfimprove.test_archive",
    "relay.selfimprove.test_frozen",
    "relay.selfimprove.test_sentinel",
    "relay.selfimprove.test_propose",
    "relay.selfimprove.test_l2",
    "relay.selfimprove.test_policy",
    "relay.selfimprove.test_apply",
    "relay.selfimprove.test_calibration",
    "relay.selfimprove.test_targeting",
    "relay.selfimprove.test_dashboard",
    "relay.selfimprove.test_status",
    "relay.selfimprove.test_diversify",
    "relay.selfimprove.test_l2_cron",
    # general-use quality gate + closed loop (#1/#2)
    "relay.selfimprove.test_quality",
    "relay.selfimprove.test_usage",
    "relay.selfimprove.test_quality_loop",
    # strengths (relay)
    "relay.test_bestofn",
    "relay.test_confidence",
    "relay.test_bestofn_run",
    "relay.test_solve_policy",
    # hardening (relay)
    "relay.test_edge_auth",
    "relay.test_soak",
]


def _run_suite(modname: str) -> tuple[bool, str]:
    """Run one suite as `python -m <module>`; pass iff exit 0 AND output contains 'PASSED'."""
    try:
        r = subprocess.run([sys.executable, "-m", modname], cwd=REPO,
                           capture_output=True, text=True, timeout=300)
    except Exception as e:
        return False, "launch error: %s" % e
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    ok = r.returncode == 0 and "PASSED" in out
    if ok:
        return True, "exit 0"
    # surface the last few non-empty lines of the failure for a quick diagnosis
    tail = [ln for ln in out.splitlines() if ln.strip()][-6:]
    return False, "exit %s | %s" % (r.returncode, " / ".join(tail))


def main() -> int:
    results = []
    for modname in _SUITES:
        ok, detail = _run_suite(modname)
        results.append((modname, ok, detail))
        print("%-4s %-40s %s" % ("OK" if ok else "FAIL", modname, "" if ok else detail))

    failed = [m for m, ok, _ in results if not ok]
    print("\n" + ("=" * 60))
    print("REGRESSION GATE: %d/%d suites passed" % (len(results) - len(failed), len(results)))
    if failed:
        print("FAILED:")
        for m, ok, detail in results:
            if not ok:
                print("  - %s : %s" % (m, detail))
        return 1
    print("ALL SUITES GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Run the frozen-guard tests against an Evolution Lab candidate (slice F5).

WHAT DECISION THIS SERVES. Whether a candidate still honours every frozen guard (the guard table,
statuses and reasons: relay/org/lab_security_regression.py). It runs the repository's existing guard
tests with the candidate directory as working directory and import root, and prints pass/fail per
guard. It writes no file of its own, never runs anything in the production tree (a candidate that is
the production root, inside it or containing it is refused), never imports candidate code into this
process, and no runtime module imports it. Each guard runs as one child process with a timeout.

Usage:
  python scripts/org_lab_security_regression.py --candidate DIR --production-root PROD_ROOT
         [--production-state PROD_STATE_DIR] [--guard ID ...] [--timeout SECONDS]
         [--python PATH] [--format text|json]
  python scripts/org_lab_security_regression.py --list
--list prints the guard table. The python used for the child processes defaults to this one.

Exit code: 0 all guards pass, 1 a guard failed / timed out / lost a test file, 2 refused (bad input).
"""
from __future__ import annotations

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

TAIL_LINES = 8


def _run_guard(cmd, candidate, timeout):
    """Run one guard command in the candidate; returns the result object summarize() expects."""
    from tools import childproc
    env = dict(os.environ)
    env["PYTHONPATH"] = candidate
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = childproc.run(cmd, cwd=candidate, env=env, timeout=timeout,
                             creationflags=childproc.headless_creationflags())
    except Exception as exc:  # subprocess.TimeoutExpired or a launch error
        name = type(exc).__name__
        if name == "TimeoutExpired":
            return {"returncode": -1, "timed_out": True, "tail": "timed out after %ss" % timeout}
        return {"returncode": -1, "timed_out": False, "tail": "launch error: %s" % exc}
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lines = [ln for ln in out.splitlines() if ln.strip()][-TAIL_LINES:]
    return {"returncode": proc.returncode, "timed_out": False, "tail": "\n".join(lines)}


def main(argv=None):
    from relay.org import lab_isolation as L
    from relay.org import lab_security_regression as S
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true", help="print the guard table and exit")
    ap.add_argument("--candidate")
    ap.add_argument("--production-root")
    ap.add_argument("--production-state")
    ap.add_argument("--guard", action="append", default=None, help="run only this guard id")
    ap.add_argument("--timeout", type=int, default=900, help="seconds per guard (default 900)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    try:
        if args.list:
            lines = []
            for gid, files, tests in S.GUARDS:
                lines.append("%s: %d frozen file(s), %d test file(s)" % (gid, len(files), len(tests)))
            sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
            return 0
        if not (args.candidate and args.production_root):
            raise S.LabSecurityError("--candidate and --production-root are required")
        if args.timeout < 1:
            raise S.LabSecurityError("--timeout must be at least 1")
        cand = os.path.abspath(args.candidate)
        if not os.path.isdir(cand):
            raise S.LabSecurityError("candidate is not a directory: %s" % cand)
        pstate = args.production_state or os.path.join(args.production_root, L.DEFAULT_STATE_NAME)
        layout = L.check_lab_layout(cand, args.production_root, pstate)
        bad = [x for x in layout["violations"] if not x["code"].startswith("marker_")]
        if bad:
            raise S.LabSecurityError("candidate is not separate from production: %s"
                                     % ", ".join(x["code"] for x in bad))
        frozen_path = os.path.join(cand, "relay", "selfimprove", "frozen.py")
        with open(frozen_path, encoding="utf-8-sig") as fh:
            manifest = S.manifest_from_source(fh.read())
        plan = S.guard_plan(lambda rel: os.path.isfile(os.path.join(cand, *rel.split("/"))),
                            only=args.guard)
        results = {}
        for entry in plan:
            if entry["missing"] or not entry["tests"]:
                continue
            cmd = S.build_command(args.python, entry["tests"])
            results[entry["id"]] = _run_guard(cmd, cand, args.timeout)
        report = S.summarize(plan, results, coverage=S.coverage_problems(manifest))
    except (OSError, ValueError) as exc:
        sys.stderr.write("refused: %s\n" % exc)
        return 2
    if args.format == "json":
        text = S.to_json(report)
    else:
        lines = ["lab security regression: %s" % ("ok" if report["ok"] else "NOT OK")]
        for g in report["guards"]:
            lines.append("  %-24s %s" % (g["id"], g["status"]))
            for m in g["missing"]:
                lines.append("      missing test file: %s" % m)
            if g["status"] in S.FAILING and g["tail"]:
                lines.extend("      | " + ln for ln in g["tail"].splitlines())
        cov = report["coverage"]
        if cov["uncovered"]:
            lines.append("  frozen files with no guard: %s" % ", ".join(cov["uncovered"]))
        if cov["not_frozen"]:
            lines.append("  guard files not in the candidate manifest: %s"
                         % ", ".join(cov["not_frozen"]))
        text = "\n".join(lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

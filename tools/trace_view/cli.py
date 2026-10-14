# -*- coding: utf-8 -*-
"""Command line: ``python -m tools.trace_view [report|list] --since ... --until ...``."""
from __future__ import annotations

import argparse
import sys
import time

from . import report as _report
from .render_md import render_markdown  # noqa: F401  (kept importable for callers)
from .timeutil import DEFAULT_TZ, fmt_duration, fmt_time, parse_tz, parse_time


def _parser():
    p = argparse.ArgumentParser(
        prog="python -m tools.trace_view",
        description="Read-only per-session trace viewer for the server's tool ledger.")
    p.add_argument("command", nargs="?", choices=("report", "list"), default="report")
    p.add_argument("--since", help="start, 'YYYY-MM-DD HH:MM[:SS]' in --tz (default: one hour before --until)")
    p.add_argument("--until", help="end, same format (default: now)")
    p.add_argument("--session", default="", help="session id prefix")
    p.add_argument("--state-dir", default=None, help="state directory (default: the server's own)")
    p.add_argument("--tz", default=DEFAULT_TZ, help="time zone for input and output (default JST)")
    p.add_argument("--gap", type=float, default=120.0, help="gap threshold in seconds (default 120)")
    p.add_argument("--lang", choices=("ja", "en"), default="ja")
    p.add_argument("--out", default=None, help="output directory (report mode)")
    p.add_argument("--format", default="md", help="comma list of md,html,xlsx (default md)")
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = _parser().parse_args(argv)
    try:
        tz = parse_tz(args.tz)
        until = parse_time(args.until, tz) if args.until else time.time()
        since = parse_time(args.since, tz) if args.since else until - 3600.0
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if since > until:
        print("error: --since is after --until", file=sys.stderr)
        return 2
    report = _report.make_report(args.state_dir, since, until, args.session or None, args.gap)
    if args.command == "list":
        for s in report["sessions"]:
            st = s["stats"]["status"]
            kinds = sorted({f["kind"] for f in s["findings"]})
            print("%s  %s-%s  calls=%d ok=%d err=%d refused=%d gaps=%d %s  %s" % (
                (s["id"] or "(none)").ljust(16), fmt_time(s["first"], tz), fmt_time(s["last"], tz),
                len(s["steps"]), st.get("ok", 0), st.get("error", 0), st.get("refused", 0),
                len(s["gaps"]), s["concurrency"]["mode"], ",".join(kinds)))
        if not report["sessions"]:
            print("(no sessions in this window)")
        return 0
    formats = [f.strip() for f in args.format.split(",") if f.strip()]
    if not args.out:
        if formats != ["md"]:
            print("error: --out is required for html/xlsx output", file=sys.stderr)
            return 2
        print(_report.render_markdown(report, tz, args.lang))
        return 0
    try:
        paths = _report.write_outputs(report, args.out, formats, tz, args.lang)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    for path in paths:
        print(path)
    print("sessions=%d calls=%d gaps=%d (threshold %s)" % (
        report["meta"]["n_sessions"], report["meta"]["n_steps"],
        sum(len(s["gaps"]) for s in report["sessions"]), fmt_duration(args.gap, args.lang)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

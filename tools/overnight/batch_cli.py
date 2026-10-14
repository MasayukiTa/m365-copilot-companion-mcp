"""python -m tools.overnight run|resume|feedback  (see README.md)"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tools.overnight import env, feedback, reconcile, report, state as S
from tools.overnight.common import Clock, Log, fmt_dt, to_epoch
from tools.overnight.detect import FleetReader
from tools.overnight.items import DEFAULT_MAX_ITEMS, parse_items
from tools.overnight.runner import (DEFAULT_MAX_INFLIGHT, DEFAULT_MAX_MINUTES, DEFAULT_POLL_S,
                                    Runner)
from tools.overnight.submit import (DEFAULT_SCRIPT, REPO, QueueSubmitter, Submitter,
                                    fleet_similarity)


def default_out_dir(clock) -> Path:
    return Path.home() / "Documents" / "朝レポート" / clock.now().strftime("%Y%m%d_%H%M")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tools.overnight",
                                description="Overnight investigation batch -> one morning report.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--fleet-dir", default=str(REPO / ".fleet"),
                        help="fleet state directory (read only); default <repo>/.fleet")
        sp.add_argument("--submit-script", default=str(DEFAULT_SCRIPT))
        sp.add_argument("--submit-route", choices=("queue", "gui"), default=None,
                        help="queue (default): write to the fleet's pending queue, works with a "
                             "locked screen; gui: type into the cockpit like a person (needs an "
                             "unlocked desktop)")
        sp.add_argument("--poll-seconds", type=float, default=None)
        sp.add_argument("--max-inflight", type=int, default=None,
                        help="items queued but unfinished at once (default %d)" % DEFAULT_MAX_INFLIGHT)

    r = sub.add_parser("run", help="queue the items and build the report")
    r.add_argument("--items", required=True, help="items.txt or items.json")
    r.add_argument("--out", default=None, help="output folder (default Documents/朝レポート/<date_time>)")
    r.add_argument("--start-at", default=None, help='wait until this time, e.g. "02:00" (JST)')
    r.add_argument("--deadline", default=None, help='stop queuing and report at this time, e.g. "07:00"')
    r.add_argument("--max-minutes-per-item", type=float, default=DEFAULT_MAX_MINUTES)
    r.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    common(r)

    s = sub.add_parser("resume", help="continue a run from its state.json")
    s.add_argument("--out", required=True)
    s.add_argument("--deadline", default=None)
    s.add_argument("--retry-failed", action="store_true",
                   help="queue failed / timed-out / skipped items again")
    common(s)

    f = sub.add_parser("feedback", help="read the OK/NG marks of a report into feedback.jsonl")
    f.add_argument("--report", required=True)
    return p


def _make_runner(st: S.State, args, clock, log) -> Runner:
    cfg = st.config
    if args.fleet_dir:
        cfg["fleet_dir"] = args.fleet_dir
    if args.submit_script:
        cfg["submit_script"] = args.submit_script
    if args.poll_seconds is not None:
        cfg["poll_s"] = args.poll_seconds
    if args.max_inflight is not None:
        cfg["max_inflight"] = args.max_inflight
    cfg.setdefault("poll_s", DEFAULT_POLL_S)
    cfg.setdefault("max_inflight", DEFAULT_MAX_INFLIGHT)
    out = Path(cfg["out_dir"])
    reader = FleetReader(cfg["fleet_dir"])
    route = args.submit_route or cfg.get("submit_route") or "queue"
    cfg["submit_route"] = route
    gui = Submitter(script=cfg["submit_script"], sleep=clock.sleep, log=log)
    submitter = QueueSubmitter(log=log) if route == "queue" else gui

    def finish(state):
        try:
            state.config["fleet_summary"] = reader.fleet_summary()
            state.save()
        except Exception:
            pass
        try:  # morning consistency check: always attempted, always recorded
            state.config["reconcile"] = reconcile.run_check(
                state, reader, gui.readonly_check, to_epoch(clock.now()))
            state.save()
        except Exception as e:
            state.config["reconcile"] = {"error": "整合検査に失敗しました: %r" % (e,)}
        report.finish(state, out, log=log, toast=env.toast, clock=clock)

    return Runner(st, clock=clock, reader=reader, submitter=submitter, log=log,
                  free_gb=lambda: env.free_disk_gb(out), floor_gb=env.disk_floor_gb,
                  awake=env.keep_awake, finish=finish,
                  similar=fleet_similarity())


def run_batch(args, clock=None) -> int:
    clock = clock or Clock()
    parsed = parse_items(args.items, args.max_items)
    out = Path(args.out) if args.out else default_out_dir(clock)
    if (out / "state.json").exists():
        print("state.json already exists in %s: use 'resume' to continue it" % out, file=sys.stderr)
        return 2
    if not parsed.items:
        print("no runnable items in %s" % args.items, file=sys.stderr)
        for w in parsed.warnings:
            print("  " + w, file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    log = Log(out / "overnight.log", clock)
    now = clock.now()
    start = env.parse_clock(args.start_at, now) if args.start_at else None
    deadline = env.parse_clock(args.deadline, now, after=start or now) if args.deadline else None
    st = S.State(out / "state.json")
    st.config.update({
        "out_dir": str(out), "items_path": str(args.items), "preamble": parsed.preamble,
        "start_at": start.isoformat() if start else "", "deadline": deadline.isoformat() if deadline else "",
        "max_minutes": args.max_minutes_per_item, "warnings": parsed.warnings,
        "created": fmt_dt(now),
    })
    for it in parsed.items:
        st.add_item(it.id, it.ask, it.tags, it.max_minutes)
    for iid, why, ask in parsed.skipped:
        key = iid
        while key in st.data["items"]:
            key += "_dup"
        rec = st.add_item(key, ask)
        rec["status"], rec["error"] = S.SKIPPED, "not run: " + why
    st.save()
    for w in parsed.warnings:
        log("input warning: " + w)
    log("%d item(s); start=%s deadline=%s out=%s" % (
        len(parsed.items), fmt_dt(start) if start else "now", fmt_dt(deadline) if deadline else "none", out))
    n = feedback.ingest_siblings(out, log)
    if n:
        log("feedback: %d new mark(s) from earlier reports" % n)
    _make_runner(st, args, clock, log).run()
    return 0


def resume_batch(args, clock=None) -> int:
    clock = clock or Clock()
    out = Path(args.out)
    path = out / "state.json"
    if not path.exists():
        print("no state.json in %s" % out, file=sys.stderr)
        return 2
    st = S.State.load(path)
    log = Log(out / "overnight.log", clock)
    cfg = st.config
    now = clock.now()
    cfg["start_at"] = ""
    if args.deadline:
        cfg["deadline"] = env.parse_clock(args.deadline, now).isoformat()
    elif cfg.get("deadline") and cfg["deadline"] <= now.isoformat():
        log("the earlier deadline has passed; resuming without one (per-item budgets still apply)")
        cfg["deadline"] = ""
    cfg.pop("run_ended", None)
    cfg.pop("stop_reason", None)
    if args.retry_failed:
        for iid in st.ids():
            st.reset_for_retry(iid)
    log("resume: %s" % ", ".join("%s=%d" % (k, len(st.by_status(k))) for k in sorted(S.ALL_STATES)
                                 if st.by_status(k)))
    _make_runner(st, args, clock, log).run()
    return 0


def record_feedback(args) -> int:
    try:
        n = feedback.ingest(args.report)
    except Exception as e:
        print("could not read %s: %r" % (args.report, e), file=sys.stderr)
        return 2
    print("%d mark(s) added to %s" % (n, Path(args.report).parent.parent / feedback.FILENAME))
    return 0


def batch_main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "run":
        return run_batch(args)
    if args.cmd == "resume":
        return resume_batch(args)
    return record_feedback(args)

"""fleet_runner.py -- launch N autonomous relays in parallel and stream live status.

This is the LAUNCHER for relay_fleet: give it several goals and it drives that many
Copilot conversations at once, each pursued to DONE by its own deterministic relay
loop, advanced from one thread in a non-blocking round-robin (relay_fleet.py).

Where the official Cowork gives you one autonomous track, this gives you N -- and
because the slow part (the agent's turn) happens server-side, N turns overlap while
the client only does cheap polls. That's the parallelism edge over Cowork.

It writes a live snapshot to <state_dir>/status.json after every round-robin sweep
(atomic temp-then-rename, so a reader never sees a half-written file) and prints a
compact live table to stdout. The WPF cockpit (ui/FleetCockpit.exe) tails that JSON.

  # goals inline
  python -m relay.fleet_runner --agent-url <URL> -g "ゴールA" -g "ゴールB"
  # goals from a file (one per line, blank lines and # comments ignored)
  python -m relay.fleet_runner --agent-url <URL> --goals-file goals.txt

The agent URL embeds a tenant GUID, so it is NOT hardcoded: pass --agent-url or set
MCP_IMPL_AGENT_URL / MCP_FLEET_AGENT_URL in .env (gitignored).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# allow running both as `python -m relay.fleet_runner` and `python relay/fleet_runner.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay.relay_fleet import (  # noqa: E402
    TERMINAL, auto_concurrency, avail_phys_mb, run_relay_fleet,
)
from relay.copilot_autopilot_relay import default_notify  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# A worker's status -> (pill label, design-language colour key). The cockpit maps the
# key to a brush; we keep the vocabulary aligned with the WPF ShuttleScope palette.
STATUS_PILL = {
    "pending":   ("待機列", "muted"),    # queued -- no tab open yet (memory discipline)
    "ready":     ("準備",   "muted"),
    "waiting":   ("実行中", "good"),     # A_GOOD blue -- a turn is streaming server-side
    "done":      ("完了",   "done"),     # finished cleanly
    "stuck":     ("停滞",   "bad"),       # B_BAD red
    "maxturns":  ("上限",   "bad"),
    "error":     ("エラー", "bad"),
    "cancelled": ("停止",   "muted"),    # user released it from the cockpit
}

DEFAULT_MAX_CONCURRENT = 3


def settings_maxtabs(default=DEFAULT_MAX_CONCURRENT):
    """Read the user's chosen max-concurrent-tabs from the shared settings.txt
    (the cockpit writes `maxtabs=N`). Falls back to `default`."""
    try:
        p = os.path.join(os.environ.get("APPDATA", ""), "copilot-bridge", "settings.txt")
        if os.path.isfile(p):
            for ln in open(p, encoding="utf-8-sig").read().splitlines():
                if ln.startswith("maxtabs="):
                    return max(1, int(ln.split("=", 1)[1].strip()))
    except Exception:
        pass
    return default


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_goals(args):
    goals = list(args.goal or [])
    if args.goals_file:
        with open(args.goals_file, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    goals.append(s)
    return goals


def _snapshot(workers, started, total, max_concurrent=0):
    total = len(workers)        # dynamic: goals can be added mid-run (native chat queue)
    done = sum(1 for w in workers if w.status in TERMINAL)
    open_tabs = sum(1 for w in workers if getattr(w, "page", None) is not None)
    return {
        "started": started,
        "updated": time.time(),
        "total": total,
        "done_count": done,
        "running": done < total,
        "max_concurrent": max_concurrent,
        "open_tabs": open_tabs,
        "avail_mb": round(avail_phys_mb()),
        "workers": [{
            "name": w.name,
            "goal": w.goal,
            "status": w.status,
            "pill": STATUS_PILL.get(w.status, (w.status, "muted"))[0],
            "color": STATUS_PILL.get(w.status, (w.status, "muted"))[1],
            "outcome": w.outcome,
            "turn": w.turn,
            "max_turns": w.max_turns,
            "reason": w.reason,
            "closed": getattr(w, "closed", False),
            "conv_url": getattr(w, "conv_url", ""),
            "last": (w.last_response or "")[:600],
        } for w in workers],
    }


def _write_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)   # atomic on Windows + POSIX


def _print_table(workers, total):
    done = sum(1 for w in workers if w.status in TERMINAL)
    line = "  ".join(
        "%s[%s t%d/%d]" % (w.name, STATUS_PILL.get(w.status, (w.status,))[0],
                           w.turn, w.max_turns)
        for w in workers)
    sys.stdout.write("\r\033[K[fleet %d/%d] %s" % (done, total, line))
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser(
        description="Launch N autonomous Copilot relays in parallel with live status.")
    ap.add_argument("--cdp-url", default=os.environ.get("MCP_CDP_URL", "http://localhost:9222"))
    ap.add_argument("--agent-url", default=(os.environ.get("MCP_FLEET_AGENT_URL")
                                            or os.environ.get("MCP_IMPL_AGENT_URL", "")))
    ap.add_argument("-g", "--goal", action="append", help="a goal (repeatable)")
    ap.add_argument("--goals-file", help="file with one goal per line (# comments ok)")
    ap.add_argument("--max-turns", type=int, default=1000,
                    help="hard cap on turns per goal (default 1000 ~ unlimited)")
    ap.add_argument("--max-concurrent", type=int, default=-1,
                    help="max tabs open at once. -1 = use the cockpit's setting "
                         "(maxtabs, default 3); 0 = auto from free RAM; N = exactly N")
    ap.add_argument("--poll-s", type=float, default=1.0)
    ap.add_argument("--stall-s", type=int, default=150,
                    help="if status.json stops updating this long while running, the "
                         "watchdog hard-resets the wedged Edge (0 = disable watchdog)")
    ap.add_argument("--max-recover", type=int, default=3,
                    help="max auto-recovery reconnect attempts after a wedged Edge")
    ap.add_argument("--no-auto-recover", action="store_true",
                    help="disable auto-recovery (single connection, no reconnect)")
    ap.add_argument("--state-dir", default=os.path.join(_repo_root(), ".fleet"),
                    help="where to write the live status.json the cockpit reads")
    args = ap.parse_args()

    goals = _read_goals(args)
    if not goals:
        ap.error("no goals -- pass -g/--goal (repeatable) or --goals-file")
    if not args.agent_url:
        ap.error("no agent URL -- pass --agent-url or set MCP_FLEET_AGENT_URL in .env")

    os.makedirs(args.state_dir, exist_ok=True)
    status_path = os.path.join(args.state_dir, "status.json")
    started = time.time()

    if args.max_concurrent > 0:
        max_conc = args.max_concurrent
    elif args.max_concurrent == 0:
        max_conc = auto_concurrency(len(goals))           # 0 = auto from free RAM
    else:
        max_conc = min(settings_maxtabs(), len(goals))    # -1 = the cockpit's setting (default 3)
    commands_path = os.path.join(args.state_dir, "commands.json")

    # write an initial 'launching' snapshot so the cockpit shows something at once
    _write_atomic(status_path, {"started": started, "updated": started,
                                "total": len(goals), "done_count": 0, "running": True,
                                "max_concurrent": max_conc, "open_tabs": 0,
                                "avail_mb": round(avail_phys_mb()),
                                "workers": [{"name": "w%d" % i, "goal": g,
                                             "status": "pending", "pill": "待機列",
                                             "color": "muted", "outcome": None,
                                             "turn": 0, "max_turns": args.max_turns,
                                             "reason": "", "closed": False, "last": ""}
                                            for i, g in enumerate(goals)]})

    print("fleet: %d goal(s) -> %s" % (len(goals), args.agent_url))
    print("       live status: %s" % status_path)
    print("       max %d tab(s) open at once (close-on-done frees each); free RAM now %d MB"
          % (max_conc, round(avail_phys_mb())))

    mc_box = [max_conc]                # live concurrency cap (cockpit can change it)
    add_box = []                       # goals queued mid-run (native chat / cockpit)

    def _drain_commands(workers):
        # cockpit -> fleet control channel. {"close":["w2"], "set_maxtabs":5}. Consume.
        try:
            if not os.path.isfile(commands_path):
                return
            with open(commands_path, encoding="utf-8") as f:
                cmd = json.load(f)
            os.remove(commands_path)
            by_name = {w.name: w for w in workers}
            for nm in cmd.get("close", []):
                w = by_name.get(nm)
                if w is not None and w.status not in TERMINAL:
                    w.cancel()
            if "set_maxtabs" in cmd:
                try:
                    mc_box[0] = max(1, int(cmd["set_maxtabs"]))
                except Exception:
                    pass
            # steering: {"steer": {"worker":"w0","text":"..."}} or a list of such
            steer = cmd.get("steer")
            if steer is not None:
                items = steer if isinstance(steer, list) else [steer]
                for it in items:
                    try:
                        w = by_name.get(it.get("worker"))
                        if w is not None and w.status not in TERMINAL:
                            w.steer(it.get("text", ""))
                    except Exception:
                        pass
            # native chat / cockpit queued a new goal into the running fleet
            add = cmd.get("add_goal")
            if add is not None:
                items = add if isinstance(add, list) else [add]
                for it in items:
                    try:
                        if isinstance(it, dict) and it.get("text"):
                            add_box.append({"text": it["text"], "priority": bool(it.get("priority"))})
                        elif isinstance(it, str) and it:
                            add_box.append({"text": it, "priority": False})
                    except Exception:
                        pass
        except Exception:
            pass

    def on_tick(workers):
        _drain_commands(workers)
        try:
            _write_atomic(status_path, _snapshot(workers, started, len(goals), mc_box[0]))
        except Exception:
            pass
        _print_table(workers, len(goals))

    from playwright.sync_api import sync_playwright
    from relay.relay_fleet import FleetContextLost
    from relay.edge_recover import cdp_alive, hard_reset

    try:
        port = int(args.cdp_url.rsplit(":", 1)[-1].split("/")[0])
    except Exception:
        port = 9222

    # Watchdog (separate thread, NO Playwright): if status.json stops advancing while a
    # run is live, the dedicated Edge is wedged -> hard-reset it. Killing it unblocks the
    # main thread's synchronous attach(), whose context then probes dead -> the run loop
    # raises FleetContextLost and we reconnect + resume below.
    import threading
    stop_wd = threading.Event()

    def _watchdog():
        last_seen, last_change = None, time.time()
        while not stop_wd.is_set():
            stop_wd.wait(5)
            if stop_wd.is_set() or args.stall_s <= 0:
                continue
            try:
                d = json.load(open(status_path, encoding="utf-8"))
                if not d.get("running") or d.get("idle"):
                    last_change = time.time(); continue
                u = d.get("updated")
                if u != last_seen:
                    last_seen, last_change = u, time.time()
                elif time.time() - last_change > args.stall_s:
                    print("\n[watchdog] fleet stalled %ds -> hard-resetting the Edge" % args.stall_s)
                    hard_reset(port)
                    last_change = time.time()
            except Exception:
                pass

    if args.stall_s > 0 and not args.no_auto_recover:
        threading.Thread(target=_watchdog, daemon=True).start()

    results_by_goal = {}
    pending = list(goals)
    attempt = 0
    while pending:
        if not cdp_alive(args.cdp_url):
            print("[recover] Edge unreachable -> hard reset before (re)connecting")
            hard_reset(port)
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(args.cdp_url, timeout=20000)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                res = run_relay_fleet(context, pending, args.agent_url,
                                      max_turns=args.max_turns, poll_s=args.poll_s,
                                      notify=default_notify, on_tick=on_tick,
                                      max_concurrent=max_conc, mc_box=mc_box, add_box=add_box)
            for r in res:
                results_by_goal[r["goal"]] = r
            pending = []                                   # finished cleanly
        except FleetContextLost as e:
            attempt += 1
            pending = e.unfinished
            print("\n[recover] Edge context lost; resuming %d goal(s) (attempt %d/%d)"
                  % (len(pending), attempt, args.max_recover))
            if args.no_auto_recover or attempt > args.max_recover:
                print("[recover] giving up (auto-recover off or attempts exhausted)")
                break
            if not cdp_alive(args.cdp_url):
                hard_reset(port)
        except Exception as e:
            attempt += 1
            print("\n[recover] %s while connecting; hard reset + retry (attempt %d/%d)"
                  % (type(e).__name__, attempt, args.max_recover))
            if args.no_auto_recover or attempt > args.max_recover:
                break
            hard_reset(port)

    stop_wd.set()
    results = [results_by_goal[g] for g in goals if g in results_by_goal]

    # final snapshot + summary
    elapsed = round(time.time() - started, 1)
    final = {"started": started, "updated": time.time(), "total": len(goals),
             "done_count": len(goals), "running": False, "elapsed_s": elapsed,
             "workers": [{"name": r["name"], "goal": r["goal"], "status": "done",
                          "pill": (r["outcome"] or "?"), "color":
                          "done" if r["outcome"] == "DONE" else "bad",
                          "outcome": r["outcome"], "turn": r["turns"],
                          "max_turns": args.max_turns, "reason": r["reason"],
                          "last": ""} for r in results]}
    _write_atomic(status_path, final)
    print("\n\n=== fleet complete in %ss ===" % elapsed)
    for r in results:
        print("  %-4s %-8s turns=%d  %s" % (r["name"], r["outcome"], r["turns"],
                                            (r["goal"][:60] + "...") if len(r["goal"]) > 60 else r["goal"]))
        if r["reason"]:
            print("       reason: %s" % r["reason"])


if __name__ == "__main__":
    main()

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
    EVAL_STALL_CEILING_S, TERMINAL, VERIFY_STATUSES, auto_concurrency, avail_phys_mb,
    goal_fields, run_relay_fleet,
)
from relay.copilot_autopilot_relay import default_notify  # noqa: E402
from relay.refuter import PANEL_LENSES  # noqa: E402

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
    "awaiting":  ("承認待ち", "muted"),  # plan proposed, paused for the user to approve/edit
    "verifying": ("検証中", "good"),     # spec 3-3: running the acceptance check locally
    "refuting":  ("反証中", "good"),     # spec 4B: an independent reviewer is checking it
    "researching": ("外部調査中", "good"),  # non-blocking deep-research side-agent is running
    "done":      ("完了",   "done"),     # finished cleanly
    "stuck":     ("停滞",   "bad"),       # B_BAD red
    "maxturns":  ("上限",   "bad"),
    "error":     ("エラー", "bad"),
    "cancelled": ("停止",   "muted"),    # user released it from the cockpit
}

DEFAULT_MAX_CONCURRENT = 3


def _settings_path():
    return os.path.join(os.environ.get("APPDATA", ""), "copilot-bridge", "settings.txt")


def _settings_int(key, default):
    """Read an int `key=N` from the shared settings.txt (cockpit-written). Falls back."""
    try:
        p = _settings_path()
        if os.path.isfile(p):
            for ln in open(p, encoding="utf-8-sig").read().splitlines():
                if ln.startswith(key + "="):
                    return int(ln.split("=", 1)[1].strip())
    except Exception:
        pass
    return default


def settings_maxtabs(default=DEFAULT_MAX_CONCURRENT):
    """The user's chosen concurrency from settings.txt (`maxtabs=N`). Under autoscale this is
    the DEFAULT/start cap; with autoscale off it's the fixed cap. Falls back to `default`."""
    return max(1, _settings_int("maxtabs", default))


def settings_autoscale():
    """Read the cockpit's autoscale config: (on, ceiling).
      autoscale=1        -> RAM-aware dynamic concurrency enabled
      autoscale_max=N    -> the ceiling tabs may grow to (defaults to maxtabs if unset)"""
    on = _settings_int("autoscale", 0) == 1
    ceiling = _settings_int("autoscale_max", 0)          # 0 = unset -> caller defaults it
    return on, ceiling


def settings_effort(default="auto"):
    """The cockpit's chosen effort mode (`effort=min|max|ultra|auto` in settings.txt). This is the
    UI selector the user picks for BOTH fleet and single runs; the CLI --effort overrides it when
    given explicitly. Invalid/unset -> `default` (auto)."""
    try:
        p = _settings_path()
        if p and os.path.isfile(p):
            for ln in open(p, encoding="utf-8"):
                ln = ln.strip()
                if ln.startswith("effort="):
                    v = ln.split("=", 1)[1].strip()
                    if v in ("min", "max", "ultra", "auto"):
                        return v
    except Exception:
        pass
    return default


def _settings_float(key, default):
    """Read a float `key=N` from the shared settings.txt (cockpit-written). Falls back."""
    try:
        p = _settings_path()
        if os.path.isfile(p):
            for ln in open(p, encoding="utf-8-sig").read().splitlines():
                if ln.startswith(key + "="):
                    return float(ln.split("=", 1)[1].strip())
    except Exception:
        pass
    return default


def settings_disk_floor(default=None):
    """The user's reserved C: free-space floor in GB (`disk_floor_gb=N` in settings.txt).
    This is the 'always keep N GB free on C:' admission reserve -- a new eval-bearing tab is
    not opened if it would push C: under this. Falls back to env SWE_DISK_FLOOR_GB (default 6)
    via relay_fleet.DEFAULT_DISK_FLOOR_GB when unset, so the cockpit/env/CLI form one chain."""
    if default is None:
        from relay.relay_fleet import DEFAULT_DISK_FLOOR_GB
        default = DEFAULT_DISK_FLOOR_GB
    return _settings_float("disk_floor_gb", default)


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_goals(args):
    """Goals come from -g flags and/or a goals file. A goals-file line is either:
      * plain text                -> a goal with no acceptance check (back-compat), or
      * a JSON object starting '{' -> {"goal"/"text": str, "check"/"checks": ..., "cwd": ...}
        carrying a machine-checkable acceptance gate (spec 3-3). folder_coder --verify
        emits these. Bad JSON falls back to treating the line as plain text."""
    goals = list(args.goal or [])
    if args.goals_file:
        with open(args.goals_file, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if s.startswith("{"):
                    try:
                        goals.append(json.loads(s))
                        continue
                    except Exception:
                        pass            # not valid JSON -> treat the raw line as a goal
                goals.append(s)
    return goals


def _snapshot(workers, started, total, max_concurrent=0, disk_floor_gb=0.0, paused=False):
    from relay.relay_fleet import free_disk_gb
    total = len(workers)        # dynamic: goals can be added mid-run (native chat queue)
    done = sum(1 for w in workers if w.status in TERMINAL)
    # open_tabs = ACTUAL browser tabs across the fleet: main agent tabs PLUS open sub-agent
    # side-pages (research / refuter). Counts real tabs (not just workers) so the cockpit's
    # tab/RAM display matches what the tab-budget admission gates on -- an auto worker mid-fan-out
    # shows as up to 3 tabs. Falls back to the main-tab count if tab_load isn't available.
    open_tabs = sum((w.tab_load() if hasattr(w, "tab_load") else
                     (1 if getattr(w, "page", None) is not None else 0)) for w in workers)
    return {
        "started": started,
        "updated": time.time(),
        "total": total,
        "done_count": done,
        "running": done < total,
        "paused": bool(paused),        # fleet frozen by the cockpit (pause toggle)
        "max_concurrent": max_concurrent,
        "open_tabs": open_tabs,
        "avail_mb": round(avail_phys_mb()),
        # disk admission reserve + current C: free, so the cockpit can show the disk gate.
        "disk_floor_gb": round(disk_floor_gb, 1),
        "free_disk_gb": round(free_disk_gb(), 1),
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
            "conv_title": getattr(w, "conv_title", ""),
            "verified": getattr(w, "verified", None),
            "verify_attempts": getattr(w, "verify_attempts", 0),
            # epoch by which an in-progress BLOCKING acceptance eval must finish (0 = idle).
            # The watchdog reads this from a frozen status.json: a future value means the main
            # thread is legitimately busy in a bounded eval, NOT a wedged Edge -> don't reset.
            "eval_busy_until": getattr(w, "eval_busy_until", 0.0),
            "plan": getattr(w, "plan_steps", []),     # surfaced so the cockpit can show/pick
            "last": (w.last_response or "")[:600],
            # full-text transcript file (all turns, untruncated) for the chat viewer to
            # render the whole conversation -- vs `last`, which is only the latest 600 chars.
            "transcript": getattr(w, "transcript", "") or "",
            # carried so the cockpit can RETRY a stopped goal with its full acceptance gate
            # intact (re-queue via add_goal). Small per goal; safe to include for 100+ workers.
            "checks": getattr(w, "checks", []),
            "cwd": getattr(w, "cwd", None),
        } for w in workers],
    }


def _write_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)   # atomic on Windows + POSIX


def _watchdog_should_reset(status, stalled_s, now=None):
    """Pure decision: given a (possibly frozen) status.json dict and how long its `updated`
    field has been unchanged, decide whether the dedicated Edge is genuinely WEDGED and must
    be hard-reset -- vs. the main thread merely being busy in a bounded acceptance eval.

    Returns (should_reset: bool, why: str). Keep this side-effect free so it can be unit-tested.

    Rule:
      * not running / idle / no stall yet      -> never reset (caller resets its stall clock)
      * a worker is in a VERIFY status, or its eval_busy_until is still in the future
        -> the main thread is legitimately blocked in a BOUNDED eval, NOT a wedged Edge.
           Do NOT reset, UNLESS the freeze has run past EVAL_STALL_CEILING_S / the worker's
           own eval deadline (failsafe: a real wedge that merely happened to be mid-verify is
           still eventually recovered).
      * otherwise (no verify in flight, purely no progress past stall_s) -> wedged -> reset.
    """
    now = time.time() if now is None else now
    if not status or not status.get("running") or status.get("idle"):
        return (False, "not running / idle")
    if stalled_s <= 0:
        return (False, "no stall")
    workers = status.get("workers") or []
    verifying = []          # names of workers legitimately busy in a bounded eval
    deadline_in_future = False
    for w in workers:
        st = w.get("status")
        try:
            busy_until = float(w.get("eval_busy_until") or 0.0)
        except (TypeError, ValueError):
            busy_until = 0.0
        if busy_until > now:
            verifying.append(w.get("name"))
            deadline_in_future = True
        elif st in VERIFY_STATUSES:
            # in a verify status but no recorded busy deadline (old snapshot, or the
            # non-blocking gate which keeps status.json fresh anyway) -- still treat as a
            # legitimate eval, bounded by the global ceiling from the freeze duration.
            verifying.append(w.get("name"))
    if verifying:
        # A worker carrying a busy deadline that is still in the future is, by definition,
        # within its declared eval budget -> WAIT (the deadline is the bound). Only when no
        # such future deadline exists do we fall back to the global ceiling on freeze time,
        # so a real wedge that merely happened to be mid-verify is still eventually recovered.
        if deadline_in_future:
            return (False, "verifying %s (within eval deadline)" % verifying)
        if stalled_s <= EVAL_STALL_CEILING_S:
            return (False, "verifying %s (within %ds eval ceiling)" % (
                verifying, EVAL_STALL_CEILING_S))
        return (True, "verifying %s but frozen %ds past %ds eval ceiling -> wedged" % (
            verifying, stalled_s, EVAL_STALL_CEILING_S))
    return (True, "stalled %ds with no eval in flight -> wedged" % stalled_s)


def _print_table(workers, total):
    done = sum(1 for w in workers if w.status in TERMINAL)
    def _turn_str(w):
        # max_turns=0 means unlimited; show "t10/∞" to avoid "t10/0" confusion.
        cap = ("∞" if not w.max_turns else str(w.max_turns))
        return "%s[%s t%d/%s]" % (w.name, STATUS_PILL.get(w.status, (w.status,))[0],
                                   w.turn, cap)
    line = "  ".join(_turn_str(w) for w in workers)
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
                         "(maxtabs, default 3); 0 = auto from free RAM; N = exactly N. "
                         "PRECEDENCE: an EXPLICIT --max-concurrent N (>=0) is the hard launch "
                         "cap and DISABLES autoscale (CLI wins over settings.txt autoscale=1); "
                         "use --autoscale to opt back in. With --max-concurrent left at -1, "
                         "the cockpit's autoscale=1 / --autoscale governs the live cap.")
    ap.add_argument("--autoscale", action="store_true",
                    help="RAM-aware dynamic concurrency: grow tabs while free RAM allows, "
                         "drain when it gets tight (ramps up 1 tab/loop, never past the cap). "
                         "Re-enables autoscale even when --max-concurrent is given explicitly.")
    ap.add_argument("--autoscale-default", type=int, default=-1,
                    help="autoscale START/default tabs. -1 = the cockpit's maxtabs setting")
    ap.add_argument("--autoscale-max", type=int, default=-1,
                    help="autoscale ceiling (上限, max tabs). -1 = cockpit's autoscale_max")
    ap.add_argument("--autoscale-headroom-mb", type=int, default=1400,
                    help="free RAM (MB) to keep for the user's other work while autoscaling")
    ap.add_argument("--autoscale-per-tab-mb", type=int, default=700,
                    help="RAM budget (MB) assumed per Copilot tab when autoscaling")
    ap.add_argument("--autoscale-up-margin-mb", type=int, default=700,
                    help="anti-thrash dead-band (MB): extra free RAM required ON TOP of the "
                         "per-tab budget before autoscale ramps UP one more tab. Stops the "
                         "1<->3 oscillation -- once settled at a water level, small RAM jitter "
                         "no longer re-grows the cap. 0 = legacy (no dead-band)")
    ap.add_argument("--disk-floor-gb", type=float, default=-1.0,
                    help="reserved C: free space (GB) to always keep: a new eval-bearing tab "
                         "is admitted only if C: free stays >= this floor after the job's eval. "
                         "-1 = use the cockpit's disk_floor_gb / env SWE_DISK_FLOOR_GB "
                         "(default 6). 0 = disable the disk gate (normal, non-bench use).")
    ap.add_argument("--eval-disk-gb", type=float, default=-1.0,
                    help="disk (GB) a single not-yet-started eval is assumed it might consume; "
                         "subtracted when looking ahead so a tab is never opened that would "
                         "itself push C: under the floor. -1 = env SWE_EVAL_DISK_GB (default 0).")
    ap.add_argument("--poll-s", type=float, default=1.0)
    ap.add_argument("--stall-s", type=int, default=150,
                    help="if status.json stops updating this long while running, the "
                         "watchdog hard-resets the wedged Edge (0 = disable watchdog)")
    ap.add_argument("--max-recover", type=int, default=3,
                    help="max auto-recovery reconnect attempts after a wedged Edge")
    ap.add_argument("--no-auto-recover", action="store_true",
                    help="disable auto-recovery (single connection, no reconnect)")
    ap.add_argument("--no-recycle", action="store_true",
                    help="disable the pre-run auto-recycle of a bloated/low-RAM Edge")
    ap.add_argument("--max-transient", type=int, default=10,
                    help="per-goal retries for TRANSIENT failures (send/timeout/likely-"
                         "transient STUCK) before giving up, with backoff (default 10, "
                         "like Claude Code retrying a failed network request)")
    ap.add_argument("--refuter", action="store_true",
                    help="operator B: after a candidate DONE, an INDEPENDENT reviewer "
                         "(non-blocking side chat) tries to refute it before accepting. "
                         "Off by default; doubles oracle cost.")
    ap.add_argument("--max-refute", type=int, default=2,
                    help="max refuter rounds per goal (default 2)")
    ap.add_argument("--panel", action="store_true",
                    help="review with a perspective-diverse PANEL (correctness / edge / "
                         "security), one independent reviewer per lens, majority vote. "
                         "Implies --refuter; ~3x the review cost.")
    ap.add_argument("--plan", action="store_true",
                    help="plan-first: each goal proposes a numbered plan and pauses for "
                         "approval (status 'awaiting'); approve or edit it with a steer to "
                         "start execution. The plan is in status.json (workers[].plan).")
    ap.add_argument("--max-research", type=int, default=3,
                    help="max deep-research delegations a worker may make per goal (RESEARCH: "
                         "-> Researcher side-agent). Default 3; 0 disables research.")
    ap.add_argument("--accuracy", action="store_true",
                    help="alias for --effort ultra (kept for back-compat).")
    ap.add_argument("--effort", choices=["min", "max", "ultra", "auto"], default=None,
                    help="how much effort the scaffold spends per task (default: the cockpit's "
                         "settings.txt effort=, else auto). "
                         "min: single-shot, minimal-diff, no review/research. "
                         "max: + on-demand research + one correctness refuter. "
                         "ultra: full 3-lens panel + refute-until-clean + liberal research + "
                         "self-test (ignore time). "
                         "auto: RIGHT-SIZE per task -- solve minimally, then ONE minimality+"
                         "correctness refuter; accept if upheld (cheap, no over-engineering), "
                         "escalate to research+panel only when it refutes. Beats a uniform ultra "
                         "by not over-engineering the easy tasks (ultra's observed failure mode).")
    ap.add_argument("--state-dir", default=os.path.join(_repo_root(), ".fleet"),
                    help="where to write the live status.json the cockpit reads")
    args = ap.parse_args()

    # ULTRA ACCURACY preset: maximise CLEAN correctness, ignore time. Wires the verified accuracy
    # levers -- the session's failure analysis pinned the bottleneck on edit PRECISION (right file,
    # wrong edit), not localization, so adversarial review + self-test target it directly. The gain
    # is clean: review/self-test/research never touch the hidden grading tests. Unattended-safe
    # (deliberately NOT --plan, which would pause for approval and stall a headless run).
    if args.effort is None:               # CLI not given -> follow the cockpit's settings.txt selector
        args.effort = settings_effort()
    if args.accuracy:
        args.effort = "ultra"
    # Effort -> worker levers. A UNIFORM ultra over-engineers easy tasks (observed: 44-47 line
    # diffs for 2-7 line gold fixes), so 'auto' right-sizes: solve minimally, gate on ONE
    # minimality+correctness refuter, and escalate (research + the refute-fix loop) only when it
    # refutes. _lenses is the refuter lens list (None = single general refuter; >1 = a panel).
    _eff = args.effort
    args._lenses = None
    if _eff == "min":
        args.refuter = False
        args.max_refute = 0
        args.max_research = 0
    elif _eff == "max":
        args.refuter = True
        args.max_refute = max(args.max_refute, 1)
        args.max_research = max(args.max_research, 3)
    elif _eff == "ultra":
        args.refuter = True
        args._lenses = list(PANEL_LENSES)               # correctness / edge / security
        args.max_refute = max(args.max_refute, 4)
        args.max_research = max(args.max_research, 6)
        os.environ["SWE_STRONG_SELFTEST"] = "1"
    elif _eff == "auto":
        args.refuter = True
        args._lenses = ["rootcause"]                    # right-size gate: minimal AND complete AND
                                                        # symptom-gone (not just "not over-engineered")
        args.max_refute = max(args.max_refute, 3)
        args.max_research = max(args.max_research, 3)
        os.environ["SWE_STRONG_SELFTEST"] = "1"
    if args.panel and args._lenses is None:             # explicit --panel still forces the 3 lenses
        args._lenses = list(PANEL_LENSES)
        args.refuter = True
    print("[effort] %s  (refuter=%s lenses=%s refute<=%d research<=%d)"
          % (_eff, args.refuter, args._lenses, args.max_refute, args.max_research))

    goals = _read_goals(args)
    if not goals:
        ap.error("no goals -- pass -g/--goal (repeatable) or --goals-file")
    if not args.agent_url:
        ap.error("no agent URL -- pass --agent-url or set MCP_FLEET_AGENT_URL in .env")
    # a goal may be a plain string or a dict carrying acceptance checks; gtexts is the
    # display/keying text for each, so dict goals don't break snapshots or result lookup.
    gtexts = [goal_fields(g)[0] for g in goals]
    nverify = sum(1 for g in goals if goal_fields(g)[1])

    os.makedirs(args.state_dir, exist_ok=True)
    status_path = os.path.join(args.state_dir, "status.json")
    started = time.time()
    # full-text conversation transcripts (one jsonl per worker, all turns untruncated).
    # The cockpit/chat viewer reads these to show whole conversations without disturbing
    # the live companion Edge. Keyed per-run so reused worker names never interleave.
    transcripts_dir = os.path.join(args.state_dir, "transcripts")
    try:
        os.makedirs(transcripts_dir, exist_ok=True)
    except Exception:
        pass

    # an EXPLICIT --max-concurrent (>=0) was given on the CLI (not the -1 "ask the cockpit"
    # sentinel). Used for the precedence rule below: CLI wins over settings.txt autoscale.
    explicit_mc = args.max_concurrent >= 0
    if args.max_concurrent > 0:
        max_conc = args.max_concurrent
    elif args.max_concurrent == 0:
        max_conc = auto_concurrency(len(goals))           # 0 = auto from free RAM
    else:
        max_conc = min(settings_maxtabs(), len(goals))    # -1 = the cockpit's setting (default 3)

    # ── autoscale: the user picks a DEFAULT (start) and a CEILING (上限). Start at the
    # default, shrink when RAM is tight, grow toward the ceiling when RAM is free.
    #
    # PRECEDENCE (clarified 2026-06-14): an explicit --max-concurrent N is a HARD launch cap
    # and the CLI wins -- it DISABLES settings.txt autoscale=1, so `--max-concurrent 2` always
    # means exactly 2 even if the cockpit left autoscale on. Passing --autoscale re-enables it
    # (explicit opt-in beats the disable). With --max-concurrent left at -1, the cockpit's
    # autoscale=1 / --autoscale governs the live cap (backward-compatible). `maxtabs` is the
    # default/start (and, with autoscale off, the fixed cap as before).
    set_on, set_ceiling = settings_autoscale()
    autoscale = args.autoscale or (set_on and not explicit_mc)
    asc_default = args.autoscale_default if args.autoscale_default > 0 else settings_maxtabs()
    if args.autoscale_max > 0:
        asc_ceiling = args.autoscale_max
    elif set_ceiling > 0:
        asc_ceiling = set_ceiling
    else:
        asc_ceiling = max(asc_default, settings_maxtabs())
    asc_ceiling = max(1, min(asc_ceiling, len(goals)))
    asc_default = max(1, min(asc_default, asc_ceiling))      # default never exceeds the ceiling
    autoscale_max = asc_ceiling
    if autoscale:
        max_conc = asc_default                               # START at the user's default
    asc_box = [1 if autoscale else 0, asc_ceiling]           # live [on, ceiling] for the cockpit

    # ── disk-floor admission reserve: keep this many GB free on C: at all times. Resolution
    # chain (most explicit wins): CLI --disk-floor-gb >= 0 -> cockpit settings.txt
    # disk_floor_gb -> env SWE_DISK_FLOOR_GB (default 6). A 0 floor disables the disk gate.
    if args.disk_floor_gb >= 0:
        disk_floor = args.disk_floor_gb
    else:
        disk_floor = settings_disk_floor()
    disk_box = [disk_floor]                                   # live disk floor (cockpit-settable)
    eval_disk = None if args.eval_disk_gb < 0 else args.eval_disk_gb
    commands_path = os.path.join(args.state_dir, "commands.json")

    # write an initial 'launching' snapshot so the cockpit shows something at once
    _write_atomic(status_path, {"started": started, "updated": started,
                                "total": len(goals), "done_count": 0, "running": True,
                                "max_concurrent": max_conc, "open_tabs": 0,
                                "avail_mb": round(avail_phys_mb()),
                                "workers": [{"name": "w%d" % i, "goal": gtexts[i],
                                             "status": "pending", "pill": "待機列",
                                             "color": "muted", "outcome": None,
                                             "turn": 0, "max_turns": args.max_turns,
                                             "reason": "", "closed": False, "last": ""}
                                            for i in range(len(goals))]})

    print("fleet: %d goal(s) (%d with acceptance check) -> %s"
          % (len(goals), nverify, args.agent_url))
    print("       live status: %s" % status_path)
    if autoscale:
        print("       autoscale ON: start %d, RAM-adjust 1..%d tab(s); free RAM now %d MB"
              % (asc_default, asc_ceiling, round(avail_phys_mb())))
    else:
        print("       max %d tab(s) open at once (close-on-done frees each); free RAM now %d MB"
              % (max_conc, round(avail_phys_mb())))
    if disk_floor > 0:
        from relay.relay_fleet import free_disk_gb
        print("       disk floor: keep >= %.1f GB free on C: (free now %.1f GB); "
              "admission gated on disk+RAM, continuous (no batch barrier)"
              % (disk_floor, free_disk_gb()))

    mc_box = [max_conc]                # live concurrency cap (cockpit can change it)
    add_box = []                       # goals queued mid-run (native chat / cockpit)
    pause_box = [False]                # cockpit pause toggle: freeze the fleet without losing
                                       # state (e.g. across a network switch); resume to continue
    stop_box = [False]                 # cockpit graceful-stop: cancel all workers and end the run

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
                # under autoscale this knob is the CEILING (上限); otherwise the fixed cap.
                try:
                    n = max(1, int(cmd["set_maxtabs"]))
                    if asc_box[0]:
                        asc_box[1] = n
                    else:
                        mc_box[0] = n
                except Exception:
                    pass
            # live disk-floor control: {"set_disk_floor_gb": 8} -- the reserved C: free space
            # the admission gate keeps. 0 disables the disk gate. Takes effect next sweep.
            if "set_disk_floor_gb" in cmd:
                try:
                    disk_box[0] = max(0.0, float(cmd["set_disk_floor_gb"]))
                except Exception:
                    pass
            # live autoscale control from the cockpit: {"set_autoscale": {"on":1,"max":4,
            # "default":2}}. on/max take effect each loop; default (if given) re-seats the
            # live cap now so turning autoscale on starts from the user's default.
            asc = cmd.get("set_autoscale")
            if isinstance(asc, dict):
                try:
                    if "on" in asc:
                        asc_box[0] = 1 if asc["on"] else 0
                    if asc.get("max"):
                        asc_box[1] = max(1, int(asc["max"]))
                    if asc.get("default"):
                        mc_box[0] = max(1, min(int(asc["default"]), asc_box[1] or 999))
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
                            # carry checks/cwd through so a RETRY re-runs WITH its acceptance
                            # gate (not just the bare prompt). goal_fields reads them downstream.
                            g = {"text": it["text"], "priority": bool(it.get("priority"))}
                            if it.get("checks"):
                                g["checks"] = it["checks"]
                            if it.get("cwd"):
                                g["cwd"] = it["cwd"]
                            add_box.append(g)
                        elif isinstance(it, str) and it:
                            add_box.append({"text": it, "priority": False})
                    except Exception:
                        pass
            # pause / resume the whole fleet: {"pause": true} freezes it in place (no new
            # turns, no new tabs), {"pause": false} resumes. Handy right before a network
            # switch so in-flight work isn't lost. Takes effect on the next sweep.
            if "pause" in cmd:
                pause_box[0] = bool(cmd["pause"])
            # graceful stop: {"stop": true} cancels every worker and ends the run.
            if cmd.get("stop"):
                stop_box[0] = True
        except Exception:
            pass

    convs_path = os.path.join(args.state_dir, "conversations.json")

    def _register_convs(workers):
        # session-shared conversation registry: every fleet conversation is added so the
        # native chat can list/read/delete it too (and vice versa). Dedup by url.
        try:
            existing = []
            if os.path.isfile(convs_path):
                try:
                    existing = json.load(open(convs_path, encoding="utf-8"))
                except Exception:
                    existing = []
            urls = set(e.get("url") for e in existing if isinstance(e, dict))
            changed = False
            for w in workers:
                u = getattr(w, "conv_url", "")
                if u and u not in urls:
                    # prefer Copilot's auto-generated chat title for the registry entry;
                    # fall back to the goal text when it hasn't been captured yet.
                    title = (getattr(w, "conv_title", "") or w.goal or "")[:60]
                    existing.append({"url": u, "title": title,
                                     "source": "fleet", "ts": time.time()})
                    urls.add(u); changed = True
            if changed:
                _write_atomic(convs_path, existing)
        except Exception:
            pass

    def on_tick(workers):
        _drain_commands(workers)
        _register_convs(workers)
        try:
            _write_atomic(status_path, _snapshot(workers, started, len(goals), mc_box[0],
                                                 disk_floor_gb=disk_box[0], paused=pause_box[0]))
        except Exception:
            pass
        _print_table(workers, len(goals))

    from playwright.sync_api import sync_playwright
    from relay.relay_fleet import FleetContextLost
    from relay.edge_recover import cdp_alive, companion_edge_mb, hard_reset, should_recycle

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
                    continue
                stalled = time.time() - last_change
                if stalled <= args.stall_s:
                    continue
                # status.json has been frozen past --stall-s. Distinguish a genuinely WEDGED
                # Edge from the main thread being legitimately blocked in a BOUNDED acceptance
                # eval (SWE-bench docker verify): in the latter case the frozen snapshot carries
                # a worker in a verify status / with eval_busy_until in the future -- DON'T
                # hard-reset that (it would discard the eval and resume every goal at attempt 1).
                should, why = _watchdog_should_reset(d, stalled)
                if should:
                    print("\n[watchdog] fleet stalled %ds -> hard-resetting the Edge (%s)"
                          % (args.stall_s, why))
                    hard_reset(port)
                    last_change = time.time()
                # else: eval in flight -> wait. Re-checked every 5s; last_change is left intact
                # so the failsafe ceiling keeps counting from the original freeze.
            except Exception:
                pass

    if args.stall_s > 0 and not args.no_auto_recover:
        threading.Thread(target=_watchdog, daemon=True).start()

    # pre-run auto-recycle: the dedicated Edge accumulates memory across runs and the
    # heavy M365 SPA gets flaky under pressure. If it has bloated or free RAM is low,
    # hard-reset it now for a lean, reliable start (only touches the dedicated profile).
    if not args.no_auto_recover and not args.no_recycle:
        try:
            emb = companion_edge_mb()
            recycle, why = should_recycle(emb, avail_phys_mb())
            if recycle:
                print("[recycle] %s -> hard-resetting the companion Edge for a clean start" % why)
                hard_reset(port)
        except Exception:
            pass

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
                                      max_concurrent=max_conc, mc_box=mc_box, add_box=add_box,
                                      refuter=args.refuter,
                                      max_refute=args.max_refute, plan_mode=args.plan,
                                      max_research=args.max_research,
                                      review_lenses=args._lenses,
                                      max_transient=args.max_transient,
                                      autoscale=autoscale, autoscale_max=autoscale_max,
                                      asc_box=asc_box,
                                      autoscale_per_tab_mb=args.autoscale_per_tab_mb,
                                      autoscale_headroom_mb=args.autoscale_headroom_mb,
                                      autoscale_up_margin_mb=args.autoscale_up_margin_mb,
                                      disk_floor_gb=disk_floor, eval_disk_gb=eval_disk,
                                      disk_box=disk_box, pause_box=pause_box, stop_box=stop_box,
                                      transcript_dir=transcripts_dir,
                                      run_id="r%x_a%d" % (int(started), attempt))
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
    results = [results_by_goal[t] for t in gtexts if t in results_by_goal]

    # final snapshot + summary -- reflect the REAL outcome of each goal, not a blanket
    # "done" (which made failed/stuck goals show as green 完了).
    def _ostatus(o):
        if o == "DONE": return "done"
        if o == "CANCELLED": return "cancelled"
        if o == "MAXTURNS": return "maxturns"
        if o in ("STUCK", "VERIFY_FAILED"): return "stuck"
        return "error"

    elapsed = round(time.time() - started, 1)
    done_count = sum(1 for r in results if r["outcome"] == "DONE")
    final = {"started": started, "updated": time.time(), "total": len(goals),
             "done_count": done_count, "running": False, "elapsed_s": elapsed,
             "workers": [{"name": r["name"], "goal": r["goal"],
                          "status": _ostatus(r["outcome"]),
                          "outcome": r["outcome"], "turn": r["turns"],
                          "max_turns": args.max_turns, "reason": r["reason"],
                          "verified": r.get("verified"),
                          "verify_attempts": r.get("verify_attempts", 0),
                          "conv_url": r.get("conv_url", ""),
                          "conv_title": r.get("conv_title", ""),
                          "transcript": r.get("transcript", ""),
                          "cwd": r.get("cwd", ""),
                          "closed": True, "last": ""} for r in results]}
    _write_atomic(status_path, final)
    print("\n\n=== fleet complete in %ss ===" % elapsed)
    for r in results:
        print("  %-4s %-8s turns=%d  %s" % (r["name"], r["outcome"], r["turns"],
                                            (r["goal"][:60] + "...") if len(r["goal"]) > 60 else r["goal"]))
        if r["reason"]:
            print("       reason: %s" % r["reason"])


if __name__ == "__main__":
    main()

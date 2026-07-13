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
  # RESUME the unfinished portion of the last run after a crash / reboot / kill
  # (re-queues only goals that did NOT finish DONE, from the durable ledger):
  python -m relay.fleet_runner --agent-url <URL> --resume
  # --resume may be combined with -g/--goals-file: the resume set PLUS the new goals
  python -m relay.fleet_runner --agent-url <URL> --resume -g "追加ゴール"

Every run writes a durable goals ledger next to status.json so --resume can relaunch
just the unfinished goals:
  <state_dir>/last_run_goals.json  -- {started, goals:[{text,checks,cwd,priority,key}]}
  <state_dir>/last_run_done.json   -- {goal_key: outcome} for goals that reached DONE

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
# key to a brush; we keep the vocabulary aligned with the WPF the sibling app palette.
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
            for ln in open(p, encoding="utf-8-sig"):   # tolerate a BOM (the C# cockpit may write one)
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


def settings_ram_floor(default=2048.0):
    """The user's reserved free-RAM floor in MB (`ram_floor_mb=N` in settings.txt). The RAM analog
    of disk_floor: the autoscale keeps this much physical RAM free for the user's other work, so a
    higher floor shrinks fleet concurrency. Cockpit-settable; default 2048 (2 GB)."""
    return _settings_float("ram_floor_mb", default)


def settings_per_tab(default=700.0):
    """Calibrated free-RAM cost per Copilot tab in MB (`autoscale_per_tab_mb=N` in settings.txt),
    measured live by bench/ram_calib.py on THIS machine and written back -- the per-user self-tuning
    of concurrency. Replaces the flat 700 MB assumption with the observed value so the autoscale
    packs accurately (the calibrator writes a deliberately CONSERVATIVE estimate, and backs off on
    swap pressure)."""
    return _settings_float("autoscale_per_tab_mb", default)


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))



# ── fragmented-goals-file guard ─────────────────────────────────────────────────
# Incident (see project notes): a single coherent multi-line PROMPT (an intro
# sentence, a "target repo:" line, a bare path, ~25 "tools/x.py" bullet lines, a
# few numbered criteria, and a "<<<FINDINGS>>> [ ] <<<END_FINDINGS>>>" output-format
# block) was passed to --goals-file instead of a real one-goal-per-line /
# one-JSON-object-per-line file. _read_goals() naively split it into 53 nonsense
# "goals" -- one per source line -- and almost every lane went STUCK immediately.
#
# The correct path for building a goals-file is bench/review_build_goals.py's
# write_goals_jsonl() (used by bench/review_run.py / bench/review_fix.py): one
# JSON object per line, e.g. {"text": "...", "cwd": "..."}.
#
# These tokens are the review PROMPT's own OUTPUT-FORMAT delimiters (see
# bench/review_build_goals.py FINDINGS_BEGIN/FINDINGS_END and the JSON-array
# example lines around them). They never legitimately appear as a goal's full
# text in a real goals file, so any ONE of them alone on a line is a high-precision
# signal that the file is a shredded prompt, not a goals list.
_FRAGMENT_DELIMITER_TOKENS = frozenset([
    "<<<FINDINGS>>>", "<<<END_FINDINGS>>>", "[", "]", "{", "}",
])

# Soft aggregate heuristic thresholds: a real goals-file file can have short lines
# (a quick one-word-ish goal), but a shredded PROMPT typically produces MANY short
# non-JSON fragments (file paths, a truncated intro, bullet lines). Require a
# minimum sample size so a small legit file of a few short goals is never flagged.
_FRAGMENT_MIN_LINES = 8
_FRAGMENT_SHORT_LEN = 40


def _fragment_guard_error(path, reason):
    """Build the actionable error text for a rejected goals-file. `reason` is a
    short, specific description of what tripped the guard (which line/token, or
    the aggregate short-line ratio) -- always names the file and points at the
    correct tool to build a real goals file instead of a raw prompt."""
    return (
        "goals-file '%s' looks like a single multi-line PROMPT that was split "
        "into one (nonsense) goal per line, not a real per-goal list: %s. "
        "Do not pass a raw multi-line prompt to --goals-file. Build a proper "
        "goals file instead -- bench/review_build_goals.py's write_goals_jsonl() "
        "writes one JSON object per line (e.g. {\"text\": \"...\", \"cwd\": \"...\"}), "
        "the same way bench/review_run.py and bench/review_fix.py already do."
        % (path, reason)
    )


def _read_goals_file(path):
    """Parse one goals-file into a list of goals (plain strings, or dicts carrying
    an acceptance gate), in the SAME format _read_goals() has always produced --
    plus a defensive guard that fails fast when the file looks like a fragmented
    single prompt (see module notes above) rather than a real per-goal list.

    A line is either:
      * plain text                -> a goal with no acceptance check (back-compat), or
      * a JSON object starting '{' -> {"goal"/"text": str, "check"/"checks": ..., "cwd": ...}
        carrying a machine-checkable acceptance gate (spec 3-3). folder_coder --verify
        emits these. Bad JSON falls back to treating the line as plain text.

    Guard rules (any ONE hard rule = immediate reject; the soft rule needs a
    large-enough sample):
      * a plain-text line that is EXACTLY a findings/output delimiter token
        (<<<FINDINGS>>>, <<<END_FINDINGS>>>, or a lone [ ] {{ }} bracket);
      * a JSON-object line whose resolved goal text (the 'text'/'goal' key) is
        empty/whitespace-only -- a goal must be non-empty, never silently
        turned into an empty lane;
      * (soft) if there are >= _FRAGMENT_MIN_LINES non-blank lines and more than
        half of them are short (< _FRAGMENT_SHORT_LEN chars) plain-text lines,
        the file is very likely a shredded prompt.

    Raises SystemExit(<actionable message>) on detection (mirrors argparse's
    ap.error() fatal-bad-input style elsewhere in this module) so the run aborts
    cleanly instead of spawning nonsense lanes. Never raises any OTHER exception
    itself: unexpected per-line failures are treated as "not fragmentary" for
    that line so the guard can only be more permissive than intended, never
    crash a legitimate run.
    """
    goals = []
    total_lines = 0
    short_nonjson = 0
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            try:
                s = raw_line.strip()
                if not s or s.startswith("#"):
                    continue
                total_lines += 1

                if s.startswith("{"):
                    try:
                        d = json.loads(s)
                    except Exception:
                        d = None
                    if d is not None:
                        if not isinstance(d, dict):
                            raise SystemExit(_fragment_guard_error(
                                path, "line %d is a JSON value that is not an "
                                "object (%s)" % (total_lines, type(d).__name__)))
                        text = d.get("text") or d.get("goal") or ""
                        if not str(text).strip():
                            raise SystemExit(_fragment_guard_error(
                                path, "line %d is a JSON object with no usable "
                                "'text'/'goal' key, so it resolves to an EMPTY "
                                "goal" % total_lines))
                        goals.append(d)
                        continue
                    # not valid JSON -> fall through, treat the raw line as plain
                    # text (existing back-compat), still subject to the checks below.

                if s in _FRAGMENT_DELIMITER_TOKENS:
                    raise SystemExit(_fragment_guard_error(
                        path, "line %d is exactly the delimiter/marker token %r "
                        "-- that only appears in a review-prompt's OUTPUT FORMAT "
                        "block, never as a real goal" % (total_lines, s)))

                if len(s) < _FRAGMENT_SHORT_LEN:
                    short_nonjson += 1
                goals.append(s)
            except SystemExit:
                raise
            except Exception:
                # be permissive on any unexpected per-line hiccup -- never let the
                # guard itself crash a legitimate run.
                continue

    if total_lines >= _FRAGMENT_MIN_LINES and short_nonjson > total_lines / 2:
        raise SystemExit(_fragment_guard_error(
            path, "%d of %d non-blank lines are short (<%d chars) plain-text "
            "fragments (file paths, truncated sentences, ...) -- consistent "
            "with one prompt shredded line-by-line, not %d distinct goals"
            % (short_nonjson, total_lines, _FRAGMENT_SHORT_LEN, total_lines)))

    return goals


def _read_goals(args):
    """Goals come from -g flags and/or a goals file. See _read_goals_file() for
    the goals-file line format and the fragmented-prompt guard it applies."""
    goals = list(args.goal or [])
    if args.goals_file:
        goals.extend(_read_goals_file(args.goals_file))
    return goals


def _pending_gates(started=0.0):
    """Scan .companion_gates/ for unanswered HITL gates and return a list of dicts.

    Each dict has: {"token", "question", "context", "ts", "path"}.
    These are surfaced in status.json so the WPF cockpit can display them and write answers.

    `path` is the ABSOLUTE path to this gate's JSON file (forward-slashed so it
    JSON-serializes cleanly and the cockpit can open it directly WITHOUT having to
    resolve MCP_ALLOWED_BASE itself).

    Cockpit writes an answer by updating the gate file at `path`:
      set:  {"answered": true, "answer": "approved"}   OR   {"answer": "denied"}
    Write atomically (temp-then-rename) to avoid partial reads.

    `started` is the epoch timestamp when the current Fleet run started.  Only gates
    with asked_at >= started are included so stale gates from a previous run (or leftover
    test gates) never bleed into a new run's pending-gates list.  Gates that lack an
    asked_at field (malformed) are silently skipped rather than crashing the snapshot.
    """
    try:
        import json as _json
        from tools.file_ops import ALLOWED_BASE
        gate_dir = ALLOWED_BASE / ".companion_gates"
        if not gate_dir.is_dir():
            return []
        result = []
        for p in gate_dir.glob("gate_*.json"):
            try:
                d = _json.loads(p.read_text(encoding="utf-8"))
                if not d.get("answered"):
                    # FIX 1 (P0): scope to the CURRENT run.  Gates without a valid asked_at
                    # (malformed or pre-dating this contract) are excluded defensively.
                    asked_at = d.get("asked_at")
                    if not isinstance(asked_at, (int, float)):
                        continue          # malformed gate -- skip rather than crash
                    if asked_at < started:
                        continue          # stale gate from a previous run -- ignore
                    result.append({
                        "token": d.get("token", p.stem),
                        "question": d.get("question", ""),
                        "context": d.get("context", ""),
                        "ts": asked_at,
                        # absolute path to THIS gate file, forward-slashed so the cockpit
                        # can open it directly (no MCP_ALLOWED_BASE resolution needed).
                        "path": p.resolve().as_posix(),
                    })
            except Exception:
                continue
        result.sort(key=lambda x: x["ts"])
        return result
    except Exception:
        return []


def _clean_final_text(text, max_len=600):
    """Strip terminal markers and collapse whitespace from a worker's final assistant text.

    Removes trailing lone tokens like "DONE", "<promptend>", agent control preamble, and
    agent control-word tokens (mirroring CleanAgentResultForUi's _resultPreambleTokens list),
    then collapses runs of whitespace and truncates to `max_len` chars.  Returns "" if
    `text` is falsy.

    Agent control words mirrored from FleetCockpit.cs CleanAgentResultForUi()
    (_resultPreambleTokens array, case-insensitive exact-match per line, and also stripped
    as leading space-delimited prefixes once the text is on a single line):
        desktopfile操作, browser操作, computeruse, Copilot, エージェント
    """
    import re
    if not text:
        return ""

    # --- Agent control-word list (mirrors CleanAgentResultForUi._resultPreambleTokens) ---
    _CTRL_TOKENS = [
        "desktopfile操作",
        "browser操作",
        "computeruse",
        "Copilot",
        "エージェント",
    ]

    # Build a regex that matches any control token as a complete word/token.
    # re.escape handles the Japanese characters safely.
    _ctrl_pattern = re.compile(
        r'(?:' + '|'.join(re.escape(tok) for tok in _CTRL_TOKENS) + r')',
        re.IGNORECASE,
    )

    t = text

    # Phase 1 (multi-line): drop lines that consist solely of a control token.
    # This matches the C# CleanAgentResultForUi per-line exact-match logic.
    if '\n' in t or '\r' in t:
        lines = re.split(r'\r\n|\r|\n', t)
        kept = []
        for line in lines:
            stripped = line.strip()
            if stripped and _ctrl_pattern.fullmatch(stripped):
                continue   # drop lines that are entirely a control token
            kept.append(line)
        t = '\n'.join(kept)

    # Phase 2: strip trailing terminal / control tokens (case-insensitive, allow surrounding ws)
    t = re.sub(r'\s*<promptend>\s*$', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s*\bDONE\b\s*$', '', t)

    # Phase 3: strip agent-control preambles at the very start (tool-call notation)
    t = re.sub(r'^\s*\[?(?:TOOL[_\-]CALL|FUNCTION[_\-]CALL|tool_call)[^\n]*\n?', '', t,
               flags=re.IGNORECASE)

    # Phase 4: collapse interior whitespace to a single space and trim.
    t = re.sub(r'\s+', ' ', t).strip()

    # Phase 5: strip leading control-word tokens (space-separated prefixes on the single
    # collapsed line).  Handles the common pattern "desktopfile操作 Fleet review C DONE"
    # -> "Fleet review C DONE".  Loop in case multiple tokens stack.
    changed = True
    while changed:
        m = _ctrl_pattern.match(t)
        if m and (len(t) == m.end() or t[m.end()] == ' '):
            t = t[m.end():].lstrip()
            changed = True
        else:
            changed = False

    return t[:max_len]


def _snapshot(workers, started, total, max_concurrent=0, disk_floor_gb=0.0, paused=False,
              ram_floor_mb=0.0, directive="", run_label="", goal_count=0):
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
        # RAM admission reserve (free RAM kept for the user) so the cockpit can show the RAM gate.
        "ram_floor_mb": round(ram_floor_mb),
        # Fleet-level directive (Bucket B): the single authoritative goal text when this run
        # was started from exactly one goal; "" when there are multiple independent goals (the
        # UI already handles multi-goal honestly and should NOT fabricate a summary). Only
        # populated when there is a genuinely single directive -- never fabricated for multi-goal.
        "directive": directive,
        # FIX 3 (P2): human-readable run label (verbatim first line of first goal, <=60 chars)
        # and total goal count for the UI header.  run_label is NEVER synthesised -- verbatim only.
        "run_label": run_label,
        "goal_count": goal_count,
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
            # Structured per-worker phase timeline (Bucket B): list of
            # {"ts": <epoch float>, "event": "<status-key>", "label": "<short English label>"}
            # appended on every status TRANSITION (never duplicated). The UI renders these as
            # a real (non-fabricated) phase spine -- no agent cooperation or inference needed.
            "phase_events": list(getattr(w, "phase_events", [])),
            # NEXT + CONFIDENCE turn markers (Bucket C, informational only -- no gate/pause):
            # the agent MAY write "NEXT: <one-line>" and/or "CONFIDENCE: low|medium|high"
            # before its terminal marker; the relay parses them and stores them here.
            "next_step": getattr(w, "next_step", "") or "",
            "self_confidence": getattr(w, "self_confidence", "") or "",
        } for w in workers],
        # Pending HITL gates from the autonomy contract gate (contract_gate.py).
        # Each entry: {"token": str, "question": str, "context": str, "ts": float, "path": str}
        # `path` is the ABSOLUTE forward-slashed path to that gate's JSON file, so the
        # cockpit can open it directly without resolving MCP_ALLOWED_BASE itself.
        # Cockpit writes the answer by patching the file at `path`:
        # Set {"answered": true, "answer": "approved"}  to approve
        # Set {"answered": true, "answer": "denied"}    to deny
        "pending_gates": _pending_gates(started=started),
    }


def _write_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)   # atomic on Windows + POSIX


# ── RUN-RESUME ledger ──────────────────────────────────────────────────────────
# When a fleet run dies midway (process killed, PC reboot, crash), the unfinished
# goals would be lost from the runner's perspective. We persist two small sidecar
# files next to status.json so `--resume` can relaunch just the unfinished portion:
#
#   last_run_goals.json  -- the DURABLE goals ledger for the last run, written ONCE
#     at run start:
#       {"started": <epoch float>,
#        "goals": [{"text": str, "checks": list, "cwd": str|None,
#                   "priority": bool, "key": "<stable hash of text>"}, ...]}
#
#   last_run_done.json   -- a parallel completion map, updated on each snapshot:
#       {"<goal_key>": "<outcome>", ...}   # only for goals that reached a
#                                          # successful terminal outcome (DONE)
#
# goal_key = a stable hash of the NORMALIZED goal text (same text -> same key across
# process restarts), so the done-map can be joined back onto the ledger after a crash.
# Both are written atomically (tmp+replace) and read tolerantly (utf-8-sig, missing/
# corrupt -> empty). A ledger write failure is logged once to stderr but NEVER crashes
# the run (spec: skip nothing silently, but never take the run down for a sidecar).
LAST_RUN_GOALS = "last_run_goals.json"
LAST_RUN_DONE = "last_run_done.json"
# outcome strings that count as a goal being genuinely finished (don't re-queue on resume)
_RESUME_SUCCESS_OUTCOMES = ("DONE",)


def _goal_key(text):
    """Stable key for a goal from its NORMALIZED text. Same text -> same key across
    process restarts (unlike Python's per-process hash()). Used to join the done-map
    onto the goals ledger when resuming."""
    import hashlib
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]


def _normalize_goal_for_ledger(goal):
    """A goal (plain string OR dict) -> the normalized ledger dict form, reusing
    goal_fields so the ledger carries the SAME text/checks/cwd the run used, plus the
    priority flag (goal_fields drops it) and the stable key."""
    text, checks, cwd = goal_fields(goal)
    priority = bool(goal.get("priority")) if isinstance(goal, dict) else False
    return {"text": text, "checks": checks, "cwd": cwd,
            "priority": priority, "key": _goal_key(text)}


def _ledger_to_goal(entry):
    """Turn a ledger entry back into the goal form the normal pipeline expects: a dict
    with text/checks/cwd/priority (checks/cwd only when present so a plain goal stays
    minimal). goal_fields reads text/checks/cwd downstream; priority is honoured by the
    add-goal queue if the goal is later re-queued."""
    g = {"text": entry.get("text", "")}
    if entry.get("checks"):
        g["checks"] = entry["checks"]
    if entry.get("cwd"):
        g["cwd"] = entry["cwd"]
    if entry.get("priority"):
        g["priority"] = True
    return g


def _write_goals_ledger(state_dir, goals, started):
    """Write the durable goals ledger ONCE at run start. Best-effort: on any failure,
    log once to stderr and return -- never raise (a sidecar must not take down the run)."""
    try:
        payload = {"started": started,
                   "goals": [_normalize_goal_for_ledger(g) for g in goals]}
        _write_atomic(os.path.join(state_dir, LAST_RUN_GOALS), payload)
    except Exception as e:
        sys.stderr.write("[resume] WARN: could not write goals ledger: %s\n" % e)


def _read_goals_ledger(state_dir):
    """Read last_run_goals.json tolerantly. Returns (started, [ledger_entry,...]).
    Missing/corrupt/malformed -> (None, []) with no crash (utf-8-sig tolerates a BOM)."""
    path = os.path.join(state_dir, LAST_RUN_GOALS)
    try:
        if not os.path.isfile(path):
            return None, []
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
        goals = d.get("goals")
        if not isinstance(goals, list):
            return None, []
        clean = [e for e in goals if isinstance(e, dict) and e.get("text")]
        return d.get("started"), clean
    except Exception:
        return None, []


def _read_done_map(state_dir):
    """Read last_run_done.json tolerantly: {goal_key: outcome}. Missing/corrupt -> {}."""
    path = os.path.join(state_dir, LAST_RUN_DONE)
    try:
        if not os.path.isfile(path):
            return {}
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _update_done_map(state_dir, workers):
    """Rewrite last_run_done.json from the live workers: map goal_key -> outcome for
    every worker that reached a successful terminal outcome (DONE). Best-effort: a
    failure logs once to stderr and is swallowed (never crashes the snapshot hook).

    Cheap: called on the snapshot tick, iterates the in-memory workers, atomic write."""
    try:
        done = {}
        for w in workers:
            outcome = getattr(w, "outcome", None)
            if outcome in _RESUME_SUCCESS_OUTCOMES:
                done[_goal_key(getattr(w, "goal", "") or "")] = outcome
        _write_atomic(os.path.join(state_dir, LAST_RUN_DONE), done)
    except Exception as e:
        # log ONCE per process (not once per tick) to avoid stderr spam every sweep.
        if not getattr(_update_done_map, "_warned", False):
            sys.stderr.write("[resume] WARN: could not write done map: %s\n" % e)
            _update_done_map._warned = True


def _resume_goals(state_dir):
    """Build the resume goal set from the sidecar ledger + done-map. Returns
    (remainder_goals, n_unfinished, m_total). A corrupt/absent ledger yields ([], 0, 0)
    -- the caller prints a clear 'nothing to resume' message rather than crashing."""
    _started, ledger = _read_goals_ledger(state_dir)
    if not ledger:
        return [], 0, 0
    done_map = _read_done_map(state_dir)
    remainder = []
    for entry in ledger:
        key = entry.get("key") or _goal_key(entry.get("text", ""))
        if done_map.get(key) in _RESUME_SUCCESS_OUTCOMES:
            continue                       # already finished successfully -- skip
        remainder.append(_ledger_to_goal(entry))
    return remainder, len(remainder), len(ledger)


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
    # cp932 console: goal/reason text can contain chars the legacy codepage cannot
    # encode (a worker once died printing U+26A0); degrade to '?' instead of crashing.
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(
        description="Launch N autonomous Copilot relays in parallel with live status.")
    ap.add_argument("--cdp-url", default=os.environ.get("MCP_CDP_URL", "http://localhost:9222"))
    ap.add_argument("--agent-url", default=(os.environ.get("MCP_FLEET_AGENT_URL")
                                            or os.environ.get("MCP_IMPL_AGENT_URL", "")))
    ap.add_argument("-g", "--goal", action="append", help="a goal (repeatable)")
    ap.add_argument("--goals-file", help="file with one goal per line (# comments ok)")
    ap.add_argument("--resume", action="store_true",
                    help="relaunch the UNFINISHED portion of the last run. Loads the "
                         "durable goals ledger (.fleet/last_run_goals.json) written at the "
                         "last run's start, drops goals that reached a successful terminal "
                         "outcome (DONE, per .fleet/last_run_done.json), and re-queues the "
                         "rest through the normal pipeline. Use after a crash / reboot / "
                         "kill so unfinished goals aren't lost. Combined with -g/--goals-file "
                         "the resume set is ADDED to the new goals. If everything finished (or "
                         "there is no ledger), prints a one-line summary and exits 0.")
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
    ap.add_argument("--ram-floor-mb", type=float, default=-1.0,
                    help="reserved free RAM (MB) to always keep for the user's other work (the RAM "
                         "analog of --disk-floor-gb): the autoscale keeps this much physical RAM "
                         "free, so a higher floor shrinks concurrency. -1 = use the cockpit's "
                         "ram_floor_mb / else --autoscale-headroom-mb. Cockpit-settable live.")
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
        # DOMAIN-AWARE gate: effort modes are general (orthogonal to task type), so the auto lens is
        # domain-agnostic ('rootcause') for a general task (research/summarize/M365/...) and swaps to
        # the code-specific 'rootcause_code' only for CODING tasks. Coding is signalled by SWE_MINIMALITY
        # (set by the SWE goal builder) or MCP_TASK_DOMAIN=coding (set by code_task). A non-coding task
        # is never reviewed with code criteria (reproduce-the-bug, producer/consumer, hunks).
        _coding = bool(os.environ.get("SWE_MINIMALITY")) or \
            os.environ.get("MCP_TASK_DOMAIN", "").lower() == "coding"
        args._lenses = ["rootcause_code" if _coding else "rootcause"]
        args.max_refute = max(args.max_refute, 3)
        args.max_research = max(args.max_research, 3)
        if _coding:
            os.environ["SWE_STRONG_SELFTEST"] = "1"     # red->green self-test is a coding discipline
    if args.panel and args._lenses is None:             # explicit --panel still forces the 3 lenses
        args._lenses = list(PANEL_LENSES)
        args.refuter = True
    print("[effort] %s  (refuter=%s lenses=%s refute<=%d research<=%d)"
          % (_eff, args.refuter, args._lenses, args.max_refute, args.max_research))

    goals = _read_goals(args)
    # RUN-RESUME: prepend the unfinished portion of the last run (from the durable
    # ledger) when --resume is passed. --resume + -g/--goals-file = resume set PLUS the
    # new goals; --resume alone with an empty/all-done ledger prints a summary and exits 0.
    if args.resume:
        resume_goals, n_unfinished, m_total = _resume_goals(args.state_dir)
        if m_total == 0:
            print("RESUME: 0 of 0 goals unfinished -- requeueing. "
                  "(no last-run ledger found -- nothing to resume)")
        else:
            print("RESUME: %d of %d goals unfinished -- requeueing." % (n_unfinished, m_total))
        # resume set goes first so it keeps its original order ahead of any new goals.
        goals = resume_goals + goals
        if not goals:
            # everything finished (and no new -g/--goals-file goals) -> nothing to launch.
            sys.exit(0)
    if not goals:
        if args.resume:
            ap.error("no goals -- --resume found an empty ledger and no -g/--goals-file given")
        ap.error("no goals -- pass -g/--goal (repeatable), --goals-file, or --resume")
    if not args.agent_url:
        ap.error("no agent URL -- pass --agent-url or set MCP_FLEET_AGENT_URL in .env")
    # a goal may be a plain string or a dict carrying acceptance checks; gtexts is the
    # display/keying text for each, so dict goals don't break snapshots or result lookup.
    gtexts = [goal_fields(g)[0] for g in goals]
    nverify = sum(1 for g in goals if goal_fields(g)[1])
    # Fleet-level directive (Bucket B): the single authoritative task description when this
    # run was started from exactly ONE goal. With multiple independent goals there is no single
    # directive, so we set it to "" -- the UI handles multi-goal runs honestly and we never
    # fabricate a summary. Only one goal -> directive = that goal's text.
    directive = gtexts[0] if len(gtexts) == 1 else ""
    # FIX 3 (P2): run_label = verbatim first line of the first goal, truncated to 60 chars,
    # with leading list markers / whitespace stripped.  NEVER synthesised.
    import re as _re
    _first_goal_text = gtexts[0] if gtexts else ""
    _first_line = _first_goal_text.splitlines()[0] if _first_goal_text else ""
    _first_line = _re.sub(r'^[\s\-*#\d.>]+', '', _first_line).strip()
    run_label = _first_line[:60]
    goal_count = len(gtexts)

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

    # RUN-RESUME: write the durable goals ledger ONCE, now, so a crash mid-run leaves a
    # record `--resume` can relaunch from. Reset the done-map to empty for this run so a
    # previous run's completions never mask this run's goals. Best-effort (never crashes).
    _write_goals_ledger(args.state_dir, goals, started)
    try:
        _write_atomic(os.path.join(args.state_dir, LAST_RUN_DONE), {})
    except Exception as e:
        sys.stderr.write("[resume] WARN: could not reset done map: %s\n" % e)

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
    # ── RAM-floor admission reserve: keep this many MB free for the user. CLI --ram-floor-mb >= 0
    # -> cockpit settings.txt ram_floor_mb -> --autoscale-headroom-mb (default 1400).
    if args.ram_floor_mb >= 0:
        ram_floor = args.ram_floor_mb
    else:
        ram_floor = settings_ram_floor(default=float(args.autoscale_headroom_mb))
    ram_box = [ram_floor]                                     # live RAM floor (cockpit-settable)
    eval_disk = None if args.eval_disk_gb < 0 else args.eval_disk_gb
    commands_path = os.path.join(args.state_dir, "commands.json")

    # write an initial 'launching' snapshot so the cockpit shows something at once
    _write_atomic(status_path, {"started": started, "updated": started,
                                "total": len(goals), "done_count": 0, "running": True,
                                "max_concurrent": max_conc, "open_tabs": 0,
                                "avail_mb": round(avail_phys_mb()),
                                "directive": directive,
                                "run_label": run_label, "goal_count": goal_count,
                                "workers": [{"name": "w%d" % i, "goal": gtexts[i],
                                             "status": "pending", "pill": "待機列",
                                             "color": "muted", "outcome": None,
                                             "turn": 0, "max_turns": args.max_turns,
                                             "reason": "", "closed": False, "last": "",
                                             # initial snapshot: worker is not yet a RelayWorker
                                             # (no phase_events attribute), so we provide the
                                             # synthetic "Queued" event manually here.
                                             "phase_events": [{"ts": started,
                                                               "event": "pending",
                                                               "label": "Queued"}]}
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
            with open(commands_path, encoding="utf-8-sig") as f:   # tolerate a BOM from the C# cockpit
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
            # live RAM-floor control: {"set_ram_floor_mb": 3072} -- the reserved free RAM the
            # autoscale keeps for the user. Higher floor -> fewer concurrent tabs. Next sweep.
            if "set_ram_floor_mb" in cmd:
                try:
                    ram_box[0] = max(0.0, float(cmd["set_ram_floor_mb"]))
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
                    existing = json.load(open(convs_path, encoding="utf-8-sig"))  # tolerate C# BOM
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
                    existing.append({"url": u, "title": title, "source": "fleet",
                                     # carry the disk transcript path + worker name so the chat
                                     # opens this conversation straight from the .jsonl -- no live
                                     # re-scrape (which fails for any conv whose agent the bridge
                                     # is not currently connected to).
                                     "transcript": getattr(w, "transcript", "") or "",
                                     "name": getattr(w, "name", ""), "ts": time.time()})
                    urls.add(u); changed = True
            if changed:
                _write_atomic(convs_path, existing)
        except Exception:
            pass

    def on_tick(workers):
        _drain_commands(workers)
        _register_convs(workers)
        # RUN-RESUME: refresh the completion map so a crash after this sweep can resume
        # only the still-unfinished goals. Cheap (in-memory scan + one atomic write).
        _update_done_map(args.state_dir, workers)
        try:
            _write_atomic(status_path, _snapshot(workers, started, len(goals), mc_box[0],
                                                 disk_floor_gb=disk_box[0], paused=pause_box[0],
                                                 ram_floor_mb=ram_box[0], directive=directive,
                                                 run_label=run_label, goal_count=goal_count))
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
                                      # per-machine calibrated RAM/tab (bench/ram_calib.py writes
                                      # autoscale_per_tab_mb to settings.txt); used only when the CLI
                                      # arg is still the 700 default, so an explicit --flag still wins.
                                      autoscale_per_tab_mb=(settings_per_tab(700.0)
                                                            if args.autoscale_per_tab_mb == 700
                                                            else args.autoscale_per_tab_mb),
                                      autoscale_headroom_mb=args.autoscale_headroom_mb,
                                      autoscale_up_margin_mb=args.autoscale_up_margin_mb,
                                      disk_floor_gb=disk_floor, eval_disk_gb=eval_disk,
                                      disk_box=disk_box, ram_box=ram_box,
                                      pause_box=pause_box, stop_box=stop_box,
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

    # Deactivate the autonomy contract so the gate goes INERT after the run.
    try:
        from tools.contract_gate import deactivate_contract
        deactivate_contract()
    except Exception:
        pass

    results = [results_by_goal[t] for t in gtexts if t in results_by_goal]

    # final snapshot + summary -- reflect the REAL outcome of each goal, not a blanket
    # "done" (which made failed/stuck goals show as green 完了).
    def _ostatus(o):
        if o == "DONE": return "done"
        if o == "CANCELLED": return "cancelled"
        if o == "MAXTURNS": return "maxturns"
        if o in ("STUCK", "VERIFY_FAILED"): return "stuck"
        return "error"

    def _final_worker_entry(r, max_turns):
        # FIX 2 (P0): recover the cleaned final assistant text and write it to BOTH
        # display_result (new contract field) and last (stop blanking it).
        # If no text is available, last keeps "" rather than being forcibly overwritten.
        raw_last = r.get("last_response", "") or ""
        cleaned = _clean_final_text(raw_last)
        return {
            "name": r["name"], "goal": r["goal"],
            "status": _ostatus(r["outcome"]),
            "outcome": r["outcome"], "turn": r["turns"],
            "max_turns": max_turns, "reason": r["reason"],
            "verified": r.get("verified"),
            "verify_attempts": r.get("verify_attempts", 0),
            "conv_url": r.get("conv_url", ""),
            "conv_title": r.get("conv_title", ""),
            "transcript": r.get("transcript", ""),
            "cwd": r.get("cwd", ""),
            "closed": True,
            # FIX 2: last is the cleaned final text, not "".  Readers expecting a non-blank
            # last after completion now see the real answer instead of a bare 完了 label.
            "last": cleaned,
            # New contract field: the same cleaned final text, explicitly named so the UI
            # can distinguish "display result" from the mid-run live tail.
            "display_result": cleaned,
            "phase_events": r.get("phase_events", []),
        }

    elapsed = round(time.time() - started, 1)
    done_count = sum(1 for r in results if r["outcome"] == "DONE")
    final = {"started": started, "updated": time.time(), "total": len(goals),
             "done_count": done_count, "running": False, "elapsed_s": elapsed,
             "directive": directive,
             # FIX 3 (P2): also carry run_label / goal_count into the final snapshot.
             "run_label": run_label, "goal_count": goal_count,
             "workers": [_final_worker_entry(r, args.max_turns) for r in results]}
    _write_atomic(status_path, final)
    # RUN-RESUME: write the FINAL completion map from the true per-goal outcomes (the
    # on_tick map may miss a worker that reached DONE on the very last sweep). A later
    # --resume then re-queues exactly the goals that did NOT finish successfully.
    try:
        final_done = {_goal_key(r["goal"]): r["outcome"]
                      for r in results if r["outcome"] in _RESUME_SUCCESS_OUTCOMES}
        _write_atomic(os.path.join(args.state_dir, LAST_RUN_DONE), final_done)
    except Exception as e:
        sys.stderr.write("[resume] WARN: could not write final done map: %s\n" % e)
    print("\n\n=== fleet complete in %ss ===" % elapsed)
    for r in results:
        print("  %-4s %-8s turns=%d  %s" % (r["name"], r["outcome"], r["turns"],
                                            (r["goal"][:60] + "...") if len(r["goal"]) > 60 else r["goal"]))
        if r["reason"]:
            print("       reason: %s" % r["reason"])


if __name__ == "__main__":
    main()

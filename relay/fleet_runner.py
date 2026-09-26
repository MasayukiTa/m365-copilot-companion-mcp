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
import io
import json
import math as _math
import os
import re as _re
import sys

from relay import outcomes as _outcomes
import time

# allow running both as `python -m relay.fleet_runner` and `python relay/fleet_runner.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# BEFORE THE IMPORTS BELOW, AND THAT ORDER IS THE WHOLE POINT. This call used to sit under
# them, and a module-level `X = os.environ.get("MCP_...")` runs at import -- so every such
# constant in relay_fleet and everything it pulls in was evaluated against the process
# environment with .env not yet loaded. The key was read; the file had simply not been.
#
# MEASURED 2026-08-28 rather than reasoned about: with MCP_FLEET_SOCKET_RETRIES=0 in .env,
# relay_fleet.DEFAULT_SOCKET_RETRIES imported through this module read 2. Call-time reads
# were unaffected -- MCP_SOCKET_FORCE_FAIL in the same file took effect in the same run --
# which is why this hid for so long: the settings that appeared to work and the settings
# that silently did nothing were separated by nothing a reader of .env could see.
#
# Nothing in the current .env was affected: of its 26 keys, exactly one is read at module
# level and differs from its default, and that one was a temporary test line. The cost was
# entirely in front of us -- the next person to set one of those keys and watch it do
# nothing.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from relay.acceptance import MalformedCheck  # noqa: E402
from relay.relay_fleet import (  # noqa: E402
    EVAL_STALL_CEILING_S, TERMINAL, VERIFY_STATUSES, auto_concurrency, avail_phys_mb,
    goal_fields, run_relay_fleet,
)
from relay.copilot_autopilot_relay import default_notify  # noqa: E402
from relay.refuter import PANEL_LENSES  # noqa: E402
from relay.fanout import fanout_family_view  # noqa: E402
from relay.control_markers import CLOSING_INSTRUCTION  # noqa: E402


# ── COORDINATOR OUTPUT CAPTURE (TEE) ────────────────────────────────────────────
# The coordinator's own stdout/stderr were never captured by any launcher (bench/
# review_run.py's bare subprocess.Popen, the WPF cockpit's SpawnFleet, or a manual
# `python -m relay.fleet_runner` run all just inherit the console) -- so an overnight
# crash/reboot left NO record of what the coordinator was doing or how it was invoked.
# Fixed once here, at the fleet_runner side, so it covers every launcher: duplicate
# stdout+stderr writes to a timestamped log under <state_dir>/ in addition to the real
# console. Best-effort -- a log-setup failure (unwritable .fleet/, permissions, full
# disk) is swallowed and the console behaves exactly as before.
class _Tee:
    """A minimal stream wrapper that writes to the REAL stream first (so console output
    is never lost, reordered, or delayed by the log) and then best-effort mirrors the
    same bytes to a log file. Any attribute this class doesn't define (isatty, encoding,
    errors, fileno, reconfigure, ...) is delegated to the real stream via __getattr__."""

    def __init__(self, real, logfile):
        self._real = real
        self._log = logfile

    def write(self, s):
        n = 0
        try:
            n = self._real.write(s)
        except Exception:
            pass
        try:
            self._log.write(s)
            self._log.flush()
        except Exception:
            pass
        return n if n else len(s)

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass
        try:
            self._log.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._real, name)


def _setup_coordinator_log(state_dir):
    """TEE sys.stdout/sys.stderr to a timestamped log under state_dir so a future
    incident (crash, reboot, kill) leaves a record of the coordinator's own output,
    regardless of which launcher started it. Call once, early in main() (after argparse,
    before the run loop). Best-effort: never raises -- on any failure the console is left
    completely untouched and this returns None. Returns the log path on success."""
    try:
        os.makedirs(state_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(state_dir, "coordinator_%s_p%d.log" % (ts, os.getpid()))
        f = open(log_path, "a", encoding="utf-8", errors="replace")
        f.write("=== fleet_runner started %s  pid=%d  argv=%r ===\n"
                % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), sys.argv))
        f.flush()
        sys.stdout = _Tee(sys.stdout, f)
        sys.stderr = _Tee(sys.stderr, f)
        return log_path
    except Exception:
        return None


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
    # operator E, wired in: relay_fleet.py raised a HITL gate (converged STUCK / unlock
    # exhausted / retry budget -- see GATE_AFTER_STUCK_RETRIES) instead of settling STUCK.
    # Non-blocking like 'researching' -- the sweep keeps stepping every other worker -- and
    # the gate itself (question + answer path) is already surfaced via status.json's
    # pending_gates / the cockpit's existing Bucket C banner; this pill is just this worker's
    # own card saying the same thing.
    "awaiting_gate": ("人間の判断待ち", "muted"),
    "done":      ("完了",   "done"),     # finished cleanly
    "stuck":     ("停滞",   "bad"),       # B_BAD red
    "maxturns":  ("上限",   "bad"),
    "error":     ("エラー", "bad"),
    "cancelled": ("停止",   "muted"),    # user released it from the cockpit
    "fresh_replay": ("新規会話", "good"),
    "content_refused": ("内容拒否", "bad"),
}

def _run_id_of(worker, started) -> str:
    """The run this worker belongs to: `r<hex>_a<attempt>`, as the transcripts spell it.

    READ, NEVER INVENTED. The transcript file is named `<run_id>_<name>.jsonl` and that name is
    the only run identity in this system that survives a restart and is shared by every worker
    of the same run. Deriving it from the path the worker already carries keeps one source of
    truth; minting a second one here would add a fifth notion of "run" to the four that already
    fail to join.

    Falls back to `r<hex of started>` when there is no transcript yet (a worker that has not
    written a turn). That is the same shape and the same run, minus the attempt number, so it
    still groups the run's rows together rather than leaving them anonymous.
    """
    path = str(getattr(worker, "transcript", "") or "")
    if path:
        # NOT os.path.basename: it splits on the HOST's separator. These paths are recorded on
        # Windows and read wherever the code runs -- a Linux CI runner sees no separator in
        # `C:\x\...\r6a9f9ad6_a0_w0.jsonl` and hands back the whole string as the run id.
        # The id is data that travels; it must not depend on who is parsing it.
        base = path.replace("\\", "/").rsplit("/", 1)[-1]
        for suffix in (".jsonl.gz", ".jsonl", ".json"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        # `<run_id>_<name>` -- the worker name is the last segment, so the rest is the run id.
        head = base.rsplit("_", 1)[0]
        if head and head != base:
            return head
    try:
        return "r%x" % int(started or 0)
    except Exception:
        return ""


def merge_conv_rows(existing, entries, now=None):
    """Merge fleet conversation rows into the shared registry. PURE: list in, list out.

    Lives at module level, not inside _register_convs, because the version that lived in the
    closure could not be tested and was wrong in two ways for an unknown length of time:

      (1) It registered a worker ONLY once `conv_url` was known (`if u and ...`), so a worker
          whose url had not been captured yet produced no row at all -- while its transcript
          was already on disk and growing. Measured 2026-09-08: two runs writing 71KB and 86KB
          of transcript at 13:47-13:48, and the newest fleet row in the file was from 10:19.
      (2) When a run reused a conversation url already present, `u not in urls` skipped it, so
          the row kept the PREVIOUS run's transcript path. Transcripts are keyed
          `<run_id>_<name>` exactly because w0 is reused across runs, so the stale pointer was
          not an older version of the same conversation -- it was a different one. Opening the
          row in the chat showed an empty conversation while the work was running.

    `existing` -- rows read from conversations.json; non-dict items are dropped, foreign rows
    (other sources, e.g. the bridge's "chat" rows) are preserved untouched.
    `entries`  -- desired rows in the registry's shape. Matched to an existing row by "url"
    first, then by "transcript"; a match is UPDATED IN PLACE, otherwise the row is appended.

    On update only the pointer fields move (transcript / name / url-once-known). The title is
    deliberately NOT recomputed: it is how the owner recognises the row in the sidebar, and
    rewriting it on every tick would rename rows under the cursor. "ts" is stamped only when
    something actually changed, for the same reason -- it orders the sidebar.

    Returns (rows, changed) so the caller can skip the write when nothing moved."""
    rows = [e for e in (existing or []) if isinstance(e, dict)]
    changed = len(rows) != len(existing or [])   # dropping a corrupt row is itself a change
    by_url, by_tr = {}, {}
    for idx, e in enumerate(rows):
        u0 = e.get("url") or ""
        if u0:
            by_url[u0] = idx
        t0 = e.get("transcript") or ""
        if t0:
            by_tr[t0] = idx
    for entry in (entries or []):
        if not isinstance(entry, dict):
            continue
        u = entry.get("url") or ""
        tr = entry.get("transcript") or ""
        if not u and not tr:
            continue   # nothing to point at yet; a later tick will carry one
        hit = by_url.get(u) if u else None
        if hit is None and tr:
            hit = by_tr.get(tr)
        if hit is not None:
            row = rows[hit]
            fresh = {}
            if tr and row.get("transcript") != tr:
                fresh["transcript"] = tr
            nm = entry.get("name") or ""
            if nm and row.get("name") != nm:
                fresh["name"] = nm
            if u and not row.get("url"):
                fresh["url"] = u     # the url arrived after the row was made from a transcript
            # Backfill only -- a row written before "goal" existed, or one whose goal write
            # raced the read, must not stay permanently unaddressable. Never overwrites a goal
            # already recorded (it cannot change for a given worker/attempt lineage anyway).
            g = entry.get("goal") or ""
            if g and not row.get("goal"):
                fresh["goal"] = g
            if fresh:
                row.update(fresh)
                row["ts"] = time.time() if now is None else now
                if tr:
                    by_tr[tr] = hit
                if u:
                    by_url[u] = hit
                changed = True
            continue
        row = dict(entry)
        row.setdefault("ts", time.time() if now is None else now)
        rows.append(row)
        if u:
            by_url[u] = len(rows) - 1
        if tr:
            by_tr[tr] = len(rows) - 1
        changed = True
    return rows, changed


def report_unused_steers(workers, reported=None, log=None):
    """Name every steering message a worker took to its grave.

    A STEER TAKES EFFECT ON THE NEXT TURN, and a worker that finishes on the turn it was
    sent never has one. Measured: a message was delivered to w2 while it was refuting, w2
    completed at turn 1, and the message was simply never used -- correctly queued,
    correctly reported as queued, and silently discarded when the worker went terminal.

    That silence is the defect, not the timing. 'Interrupt' promises mid-turn and cannot
    deliver it: a turn in flight is a request the model is already answering, and there is
    nowhere to insert anything until it replies. What can be delivered is the truth about
    what happened to the message.

    `reported` is a set the caller keeps, so each worker is named once rather than on every
    sweep -- a line per sweep at one-second polling is a log nobody reads, which is how the
    original delivery bug survived.
    """
    say = log or (lambda m: print(m, flush=True))
    seen = reported if reported is not None else set()
    named = 0
    for w in workers or []:
        try:
            pending = list(getattr(w, "steer_msgs", None) or [])
            # NOT FROM done, WHICH IS STILL DEFERRABLE. The first version reported a
            # message as never used the moment its worker went DONE, in the window before
            # the sweep revived it -- announcing a loss that had not happened. Only a
            # worker the deferral will not touch has genuinely lost the message.
            if (not pending or w.status not in TERMINAL or w.name in seen
                    or w.status == "done"):
                continue
            seen.add(w.name)
            named += 1
            say("[steer] NEVER USED by %s: it finished (%s) before another turn began. "
                "%d message(s), first: %s"
                % (w.name, w.status, len(pending), str(pending[0])[:80]))
        except Exception:
            continue
    return named

#: How a late steer is put to the worker that already answered. It is a follow-up, not a
#: fresh task, and saying so is what stops the model re-doing the whole goal: the
#: conversation it is resumed into already holds the work.
FOLLOW_UP_PROMPT = ("【ユーザーからの追加指示】%s\n"
                    "直前までの作業内容を踏まえ、この追加指示に対してだけ答えてください。"
                    "最初からやり直す必要はありません。"
                    + CLOSING_INSTRUCTION)

#: Same follow-up mechanics, but honest about authorship for the ONE case this channel also
#: carries that is not a human steer: relay_fleet._inject_unlock's own recovery payload,
#: redelivered here because the worker it was meant for had already gone TERMINAL (stuck) by
#: the time it was ready to resume. FOLLOW_UP_PROMPT's "【ユーザーからの追加指示】" ("additional
#: instruction FROM THE USER") is true for a real steer and false for that payload -- and a
#: false "from the user" label wrapped around text that hands over a password and directs a
#: tool call is exactly what a safety-aligned model should treat as an injection and refuse.
#: See relay_fleet.is_recovery_payload / SYSTEM_RECOVERY_PREFIX for the same distinction made
#: on the sibling channel (steer_msgs); this is the follow-up channel's copy of it.
SYSTEM_RECOVERY_FOLLOW_UP_PROMPT = (
    "【システムからの運用連絡(このマシン上の自動復旧機構が生成した内容。ユーザー発言ではありません)】%s\n"
    "直前までの作業内容を踏まえ、上記の運用上の指示にだけ従ってください。"
    "最初からやり直す必要はありません。"
    + CLOSING_INSTRUCTION)

def _follow_up(worker, text, enqueue, say):
    """Queue the message as a new goal continuing `worker`'s conversation. True if queued.

    The conversation is addressed by the FINISHED WORKER'S GOAL TEXT, because that is what
    identifies a conversation in socket_route's record -- `conversation_for_goal` matches on
    it. Handing back the goal is therefore the whole of what a resume needs.

    A worker with no goal text cannot be followed up, and that is said rather than guessed:
    a follow-up that silently became a fresh conversation is the failure this exists to
    avoid, and it answers plausibly either way.
    """
    if enqueue is None:
        return False
    goal = (getattr(worker, "goal", "") or "").strip()
    if not goal:
        say("[steer] cannot follow up %s: no goal text to identify its conversation"
            % worker.name)
        return False
    try:
        from relay.relay_fleet import is_recovery_payload as _is_recovery
    except Exception:
        _is_recovery = None
    template = FOLLOW_UP_PROMPT
    if _is_recovery is not None:
        try:
            if _is_recovery(text):
                template = SYSTEM_RECOVERY_FOLLOW_UP_PROMPT
        except Exception:
            pass
    try:
        enqueue({"text": template % text,
                 "follow_up_to": goal,
                 "priority": True,
                 "cwd": getattr(worker, "cwd", "") or ""})
    except Exception as exc:
        say("[steer] could not queue a follow-up for %s: %s: %s"
            % (worker.name, type(exc).__name__, str(exc)[:90]))
        return False
    say("[steer] %s had already finished, so the message goes to a NEW worker continuing "
        "the SAME conversation" % worker.name)
    return True

def deliver_steers(items, workers, log=None, enqueue=None):
    """Hand each steering message to the worker(s) it is for, and NAME EVERY REJECTION.

    AN EMPTY WORKER NAME MEANS EVERY LIVE WORKER, and it did not. The cockpit picks the
    first non-terminal worker it knows about and, finding none, sends "" -- its own comment
    says "relay broadcasts to all workers". Nothing broadcast: `by_name.get("")` is None,
    so the steer was dropped, and the cockpit said "queued for the next turn" regardless.
    Measured on a thirteen-worker run: the command file was consumed and not one of sixteen
    transcripts carried the message.

    A STEER THAT GOES NOWHERE IS WORSE THAN AN ERROR, because the person believes they
    redirected the work and then watches it continue in the old direction. Every path here
    either delivers or says why it did not.

    IT TAKES EFFECT ON THE WORKER'S NEXT TURN, not mid-turn. A turn already in flight is a
    request the model is answering; there is nowhere to insert anything until it replies.
    That is worth saying out loud, because 'interrupt' suggests otherwise.

    A STEER FOR A WORKER THAT HAS FINISHED IS NOT LATE, IT IS A FOLLOW-UP. Dropping it was
    the honest thing to do while there was nowhere to put it, and there is somewhere: a
    goal carrying `follow_up_to` resolves, in RelayWorker.__init__, to the conversation the
    named goal ran in, and attach() opens that conversation rather than a fresh one. So the
    message becomes a NEW worker continuing the SAME conversation -- no state machine runs
    backwards, no finished worker is revived, and the context the steer was about is still
    there.

    (Reviving the finished worker instead was tried and withdrawn. The sweep releases a
    worker's transport the instant it is terminal, so a revived one sent into a driver that
    was gone: AttributeError, retried 74 times against a budget of 10, on a live run. The
    reason no place in poll() or the sweep worked is that neither owns what a revival needs
    -- transport, a turn, a budget, and the run's accounting. Admission owns all four, and
    admission is reached by being a goal.)

    ADDRESSED STEERS ONLY. A broadcast that arrives after one worker of eight has finished
    was heard by the other seven; saying it again to the dead one is not a thing anybody
    wants. Those stay reported rather than resurrected.

    `enqueue(goal_dict)` is how a goal is added mid-run. Without it the terminal branch
    reports as before, so a caller that cannot enqueue loses nothing it had.

    Returns the number of workers that were given the message.
    """
    say = log or (lambda m: print(m, flush=True))
    by_name = {w.name: w for w in workers}
    delivered = 0
    for it in (items if isinstance(items, list) else [items]):
        try:
            # A DICT OR A STRING, AND NOTHING ELSE. str(None) is "None", which is not
            # empty, so a malformed entry became a real message BROADCAST to every live
            # worker -- caught by the test for "one bad item does not stop the others",
            # which found it doing rather more than not stopping them.
            if isinstance(it, dict):
                name = (it.get("worker") or "").strip()
                text = it.get("text") or ""
            elif isinstance(it, str):
                name, text = "", it
            else:
                say("[steer] DROPPED: not a message (%s)" % type(it).__name__)
                continue
            if not text.strip():
                say("[steer] refused: empty text")
                continue
            if name:
                w = by_name.get(name)
                if w is None:
                    say("[steer] DROPPED for %r: no such worker in this run (have %s)"
                        % (name, ",".join(sorted(by_name))[:120]))
                elif w.status in TERMINAL:
                    if _follow_up(w, text, enqueue, say):
                        continue
                    say("[steer] DROPPED for %s: already %s" % (name, w.status))
                else:
                    w.steer(text)
                    delivered += 1
                    say("[steer] queued for %s (%s) -- takes effect on its next turn"
                        % (name, w.status))
                continue
            live = [w for w in workers
                    if w.status not in TERMINAL and w.status != "pending"]
            if not live:
                say("[steer] DROPPED: no live worker to steer (%d workers, all terminal "
                    "or pending)" % len(workers))
                continue
            for w in live:
                w.steer(text)
            delivered += len(live)
            say("[steer] queued for all %d live workers (%s) -- takes effect on each "
                "one's next turn" % (len(live), ",".join(w.name for w in live)[:120]))
        except Exception as exc:
            say("[steer] DROPPED: %s: %s" % (type(exc).__name__, str(exc)[:120]))
    return delivered


#: How long a refusal has to sit unclaimed before the harness MAY act on it. Age alone is not
#: sufficient: while a candidate worker is still `waiting`, its reply has not landed yet and
#: _looks_locked has had literally no chance to classify/recover it. The old sweep ignored that
#: fact and raced normal recovery at 45s; measured 2026-09-26 unlock grants took median 68.3s
#: and up to 172.4s, so healthy in-flight turns were routinely given duplicate re-unlock steers.
#: We keep 45s as the post-settlement backstop, but NEVER intervene in an in-flight turn.
UNCLAIMED_REFUSAL_GRACE_S = 45.0

#: Refusals already acted on by the fallback sweep, keyed individually rather than by a single
#: monotonic timestamp. A later worker's refusal must never watermark away an older refusal that
#: was deliberately deferred while its own turn was still in flight. Pruned to the server's fresh
#: refusal window on every sweep, so this cannot grow without bound.
_HANDLED_UNCLAIMED = set()


def sweep_unclaimed_refusals(workers, now=None, log=None, deliver=None):
    """Re-unlock workers for a refusal that NOBODY CLASSIFIED. Replaces the fallback button.

    THERE USED TO BE A BUTTON. The panel carried a "re-unlock" control with a worker-name box,
    for the case its own tooltip described: a worker stopped by a lock that automatic recovery
    had not noticed. That is the harness handing its own failure to a person, and the person
    is the part of this system least able to know WHICH worker, if any, is the stuck one.

    THE HOLE IT COVERED IS REAL, AND MEASURED. Twice on 2026-09-15 a worker was refused for
    lock, no recovery fired, and the run carried on regardless -- once producing a deliverable
    that claimed to have verified content it had never been able to read. Automatic recovery
    is driven by the REPLY (_looks_locked on the text that comes back), so a refusal that never
    produces a recognisable reply is invisible to it.

    WHAT THIS ASKS INSTEAD, using only records the server already writes: the server logs every
    refusal; readers log every classification. A refusal with no classification consuming that exact row and no later grant for its
    session, past the grace period, was picked up by nobody. relay/turn_windows says which workers had a turn
    open at that instant, and those are the ones told to unlock.

    THE COST IS ASYMMETRIC AND THE BIAS FOLLOWS IT. Unlocking a worker that was not locked
    costs one turn. Not unlocking one that was costs a deliverable that is confidently wrong
    about work it never did. So an ambiguous window delivers to every candidate rather than
    guessing between them -- the same broadcast the button offered as an empty target, chosen
    for a reason rather than typed by someone who could not tell either.

    Returns the list of receipts it produced, newest last. Never raises: a recovery that fell
    over while recovering would be the failure it exists to prevent, wearing its own clothes.
    """
    say = log or (lambda m: print(m, flush=True))
    send = deliver or apply_reunlock
    t = float(now if now is not None else time.time())
    out = []
    try:
        from tools import lock_state as _ls
        from relay import turn_windows as _tw
        from relay.relay_fleet import NO_CONTEXT_REFUSAL, is_recovery_payload as _is_recovery_payload

        since = t - float(getattr(_ls, "DEFAULT_FRESH_SEC", 180.0))
        claims = _ls.classifications(since, now=t)
        grants = _ls.granted_records(since, now=t)
        refusals = [r for r in _ls.matching_records(since, now=t)
                    if not str(r.get("detail") or "").startswith(NO_CONTEXT_REFUSAL)]

        def _refusal_key(refusal):
            """Stable identity for one refusal row within the fresh window."""
            try:
                ts = float((refusal or {}).get("ts") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            return (
                ts,
                str((refusal or {}).get("session") or ""),
                str((refusal or {}).get("client_ip") or ""),
                str((refusal or {}).get("site") or ""),
                str((refusal or {}).get("detail") or "")[:160],
            )

        # Keep only keys that still exist in the same freshness window we are about to inspect.
        # This bounds memory while preserving deferred older refusals independently of newer ones.
        fresh_keys = {_refusal_key(r) for r in refusals}
        _HANDLED_UNCLAIMED.intersection_update(fresh_keys)

        live = {getattr(w, "name", ""): w for w in (workers or [])
                if getattr(w, "status", "") not in ("done", "stuck", "cancelled",
                                                      "content_refused", "maxturns", "error")}

        def _claim_consumed_refusal(claim, refusal):
            """True only when this classification names THIS refusal as its evidence.

            A classification timestamp is not a global acknowledgement: under concurrency one
            worker can classify its own refusal after a different worker's refusal. The ledger
            already stores `consumed`; use the join it was written to provide.
            """
            consumed = (claim or {}).get("consumed") or {}
            try:
                cts = float(consumed.get("ts") or 0.0)
                rts = float((refusal or {}).get("ts") or 0.0)
            except (TypeError, ValueError):
                return False
            if cts <= 0.0 or rts <= 0.0 or abs(cts - rts) > 1e-6:
                return False
            cs = str(consumed.get("session") or "")
            rs = str((refusal or {}).get("session") or "")
            return not (cs and rs and cs != rs)

        def _grant_resolved_refusal(grant, refusal):
            """A successful unlock resolves only an earlier refusal from the same MCP session."""
            rs = str((refusal or {}).get("session") or "")
            gs = str((grant or {}).get("session") or "")
            if not rs or gs != rs:
                return False
            try:
                return float(grant.get("ts") or 0.0) >= float(refusal.get("ts") or 0.0)
            except (TypeError, ValueError):
                return False

        for rec in refusals:
            ts = float(rec.get("ts") or 0.0)
            key = _refusal_key(rec)
            if key in _HANDLED_UNCLAIMED or (t - ts) < UNCLAIMED_REFUSAL_GRACE_S:
                continue
            # A classification only claims the refusal row it actually consumed. The old
            # timestamp-only rule let worker B's later classification hide worker A's unhandled
            # refusal. Conversely, a later grant for this exact session means recovery already
            # succeeded even if no classification row was written, so do not send another unlock.
            if any(_claim_consumed_refusal(c, rec) for c in claims):
                continue
            if any(_grant_resolved_refusal(g, rec) for g in grants):
                continue
            cands = []
            for n in _tw.candidates(ts):
                w = live.get(n)
                if w is None:
                    continue
                # A refusal occurring inside an in-flight turn is expected to be unclassified:
                # classification runs on the reply, and the reply does not exist yet. Queueing a
                # steer here races the worker's own recovery and was the main source of duplicate
                # unlock prompts in the 2026-09-25/26 logs. Wait for the turn to settle first.
                if getattr(w, "status", "") == "waiting":
                    continue
                if _is_recovery_payload(getattr(w, "job", "")):
                    continue
                if any(_is_recovery_payload(x) for x in (getattr(w, "steer_msgs", []) or [])):
                    continue
                cands.append(n)
            if not cands:
                # No worker had a turn open then: this refusal belongs to something else on
                # this machine. Delivering to everyone on no evidence is how a recovery starts
                # causing the noise it was built to quieten.
                continue
            # Mark only THIS refusal handled. Do it before delivery, matching the old one-shot
            # behaviour even if delivery itself reports a missing password or other terminal
            # inability; retrying that every tick would be a new spam loop.
            _HANDLED_UNCLAIMED.add(key)
            for name in cands:
                say("[reunlock] nobody classified the refusal at %.0f; %s had a turn open "
                    "then -- sending unlock" % (ts, name))
                out.append(send(name, workers, log=log))
    except Exception as exc:
        say("[reunlock] unclaimed-refusal sweep skipped: %s: %s"
            % (type(exc).__name__, exc))
    return out


def apply_reunlock(target, workers, enqueue=None, log=None):
    """THE FALLBACK BUTTON. Automatic recovery already exists: relay_fleet's
    `_inject_unlock` injects `UNLOCK_PREFIX % password` REACTIVELY, once a reply LOOKS
    like a lock refusal (see `_looks_locked`). (Before 2026-09-25, `_initial_job_with_unlock`
    also injected it proactively into a fresh worker's FIRST turn whenever a local password
    was found; that was removed because M365 Copilot's own safety/DLP filter refused that
    exact "call unlock with this password" turn-1 shape deterministically, so the proactive
    send could never succeed -- see `_initial_job_with_unlock`'s docstring.) The reactive
    heuristic can still miss -- it is deliberately loose and gated on a matching record, and
    it never runs at all for a refusal that arrives and never gets recognised as one.
    Measured twice in one day (2026-09-15): a worker refused for lock, no recovery fired,
    and the run continued regardless -- once producing a deliverable that claimed to have
    verified content it had never been able to read. There was no button for the operator
    to press.

    This is that button, and it is DELIBERATELY the same delivery path as a steer: the
    unlock instruction is a turn like any other, and `deliver_steers` already carries the
    hard-won rule that every rejection must be named rather than swallowed (see its
    docstring and tests/test_steer_delivery.py). Re-deriving that here would risk
    re-introducing the empty-name-drops-silently defect that file exists to prevent.

    THE PASSWORD IS READ HERE, ON THIS MACHINE, FROM THIS MACHINE'S .env -- and goes
    NOWHERE but into the one transient turn handed to `deliver_steers`. It is never
    written to `.fleet/commands.d/*.json` (plain text, read by several processes) and
    never appears in the dict this function returns: that dict is built only from the
    LOG LINES `deliver_steers` emits about names and statuses, which by construction
    never echo the turn text (see its own say() calls -- none of them format `text`).

    `target` is a worker name, or "" / "*" for every live worker (mirrors deliver_steers'
    own empty-name-means-broadcast rule -- "*" is accepted too because a command typed by
    a person reaches for the wildcard before the empty string).

    Returns a dict meant to be written straight into status.json so the operator can see
    what happened without guessing: {"ts", "target", "ok", "delivered", "reason"}. `ok`
    is False both when nothing was delivered AND when there was no password to try --
    "I pressed the button and nothing happened" is exactly the failure this exists to end,
    so a missing password is reported, not swallowed.
    """
    say = log or (lambda m: print(m, flush=True))
    name = (target or "").strip()
    if name == "*":
        name = ""

    from relay.relay_fleet import UNLOCK_PREFIX, _unlock_password
    pw = _unlock_password()
    if not pw:
        try:
            from tools.secret_store import (PROBLEM_UNDECRYPTABLE,
                                            unlock_password_problem)
            problem = unlock_password_problem()
        except Exception:
            problem = ""
        if problem == PROBLEM_UNDECRYPTABLE:
            reason = ("local unlock password is set but could not be decrypted on this "
                      "machine -- nothing delivered")
        else:
            reason = "no local unlock password configured (.env unset) -- nothing delivered"
        say("[reunlock] REFUSED for %r: %s" % (name or "*", reason))
        return {"ts": time.time(), "target": name or "*", "ok": False,
                "delivered": 0, "reason": reason}

    msgs = []

    def _capture(m):
        msgs.append(m)
        say(m)

    item = {"worker": name, "text": UNLOCK_PREFIX % pw}
    delivered = deliver_steers(item, workers, log=_capture, enqueue=enqueue)
    ok = delivered > 0
    reason = "; ".join(msgs)[-400:]
    if not reason:
        reason = "delivered" if ok else "not delivered (see fleet console log)"
    return {"ts": time.time(), "target": name or "*", "ok": ok,
            "delivered": delivered, "reason": reason}


def report_status(o):
    """The reported status for an outcome, from the closed set in relay/outcomes.py.

    THIS USED TO BE A CHAIN OF `if` ENDING IN `return "error"`, and that last line
    misreported healthy work twice -- INFRA_STUCK and REFUSED, which this same file
    already listed as retryable a thousand lines above, and FANOUT, so a run whose nine
    subtasks all completed and merged reported 0 done of 1. A catch-all cannot tell "a
    value that means failure" from "a value nobody has added yet", so every new outcome
    was born an error, silently.

    The set is closed now and an exhaustiveness test walks the AST for every literal
    assigned to `.outcome`, so an unlisted value fails CI on the commit that introduces
    it. If one reaches here anyway it is ANNOUNCED rather than flattened quietly: the run
    keeps its report -- eighty goals' results live only in this process -- but nobody has
    to read a total to discover the gap.
    """
    try:
        return _outcomes.status_of(o)
    except _outcomes.UnknownOutcome:
        print("!! UNKNOWN OUTCOME %r -- relay/outcomes.py does not list it; reporting as "
              "error. Add it there with the status it should mean." % (o,), flush=True)
        return "error"

from tools.settings_keys import default as _settings_default

#: THE default RAM floor, declared in tools/settings_keys.py and shared with the panel and the
#: admission gates. It had three owners until 2026-09-17; see that module.
RAM_FLOOR_DEFAULT_MB = float(_settings_default("ram_floor_mb"))

DEFAULT_MAX_CONCURRENT = 3

#: The autoscale ceiling used when the operator has never set one. MUST MATCH the cockpit's
#: `_autoMax` default (ui/FleetCockpit.cs), which is 100 and documented there as "high by
#: design": under autoscale the ceiling exists to get out of ram_target_cap's way, not to cap
#: anything itself. When the two disagreed the screen showed 100 and the fleet ran 3.
AUTOSCALE_CEILING_DEFAULT = 100


def _settings_path():
    # ONE RESOLVER, because this path had five copies that did not agree -- and because
    # the file is moving into the repository, where every context resolves it identically.
    # See tools/settings_path.py for the 2026-09-16 incident this closes.
    from tools.settings_path import settings_file
    return settings_file()


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


#: Outcomes worth another attempt: the run did not get an answer, as opposed to the task being
#: wrong. INFRA_STUCK means the connection or agent never established, REFUSED means the agent
#: answered but Copilot declined this prompt, and STUCK is the one the fleet actually emits --
#: "token-limit recycle: fresh conversation did not render" and its kin.
#:
#: THE FIRST VERSION OF THIS LIST NAMED OUTCOMES THAT DO NOT OCCUR. It was written by reading
#: the branches that set an outcome, not by counting what the fleet emits. Against the recorded
#: history the distribution is DONE 4, MAXTURNS 4, STUCK 2 -- and STUCK, the only failure that
#: actually happens here, was the one left out. A live run had w1 sitting at STUCK with a
#: token-limit recycle behind it, which is exactly the transient this exists for, and it would
#: never have been re-queued.
#:
#: Measured 2026-08-25 across 28 goals in six runs: 25% came back with Copilot's canned "I
#: couldn't respond to that". The failure moved to a different goal every run, was unaffected
#: by concurrency (25% at one worker, 25% at two), and the two goals that failed one run both
#: passed when re-queued unchanged. That is transient, and a bounded retry is what it needs.
RETRYABLE_OUTCOMES = _outcomes.RETRYABLE

#: NOT retried, and the distinction is the point. MAXTURNS means the worker ran its whole turn
#: budget and still had not finished: running it again spends the same budget on the same task
#: and ends the same way. CANCELLED was a human saying stop. Re-queueing either is not recovery,
#: it is repetition.
#: Now the exact complement, checked at import. It used to name three outcomes out of
#: eleven, so the other five were non-retryable only by omission -- which is the same
#: answer as "nobody considered them".
NON_RETRYABLE_OUTCOMES = _outcomes.NON_RETRYABLE


def settings_autoretry():
    """(enabled, cap) for re-queueing a stuck goal, from the same keys the cockpit writes.

    THE FLEET RETRIES ITSELF NOW, not only when a cockpit is watching. The re-queue used to
    live entirely in the cockpit's tick, which reads .fleet/status.json and injects add_goal --
    so a run started from the command line, from a scheduled task, or against any other state
    directory got no retry at all. "The run finishes with nothing refused" cannot depend on
    whether a window happens to be open.

    Same settings keys as the cockpit, deliberately: two retry policies that can disagree is
    worse than either one alone.
    """
    on = _settings_int("autoretry", 1) == 1
    cap = max(0, min(3, _settings_int("autoretry_max", 2)))
    return (on and cap > 0), cap


def build_settings_follower(disk_box, ram_box, mc_box, asc_box, path_fn=None, log=None):
    """Wire the settings file to the live boxes a running fleet reads.

    NAMED RATHER THAN INLINE so it can be exercised. It lived inside run(), which is 700 lines
    and cannot be called in a test, so the only available check was to read the source for a
    `.watch(` -- and source cannot catch a callback that writes into a box nothing reads.
    gpt-6-astra named that gap while reviewing tools/settings_keys.py. With this callable, the
    file-to-box half is a behavioural test; box-to-decision stays source-level, because
    relay_fleet indexes these same list objects at the moment it decides.

    The boxes are the live values themselves, shared with run_relay_fleet -- not copies. That
    is the whole mechanism: the sweep reads disk_box[0] each time it admits, so writing here
    changes the next decision without anything restarting.

    BOUNDED THE SAME WAY THE COMMAND CHANNEL IS, WHICH THIS DID NOT DO (gap left by e822fb6).
    That commit gave the cockpit -> running-fleet command channel a strict schema -- a
    set_disk_floor_gb outside [0, 100] GB, a set_ram_floor_mb outside [0, 65536] MB, or a
    set_maxtabs outside [1, 100] is refused (validate_command) -- but this follower reads the
    SAME THREE KEYS out of the SAME settings.txt by a completely different path (the cockpit's
    own live-push button vs. its "save the panel to disk, a running fleet notices next sweep"
    path) and applied only a floor, no ceiling: `max(0.0, float(v))` accepted a disk floor of
    1e9 GB or a NaN RAM floor from a hand-edited or foreign-written settings.txt. Two admission
    paths into the same running fleet that disagree about what a valid number is would have
    reopened exactly the hole SEC-08 closed for the other one.

    CLAMPED, NOT IGNORED, MATCHING THE PANEL'S OWN CHOICE. ui/FleetCockpit.cs never refuses a
    number outside range: SetDiskFloor/SetRamFloor/SetMaxTabs clamp with Math.Max/Math.Min
    before writing settings.txt, and LoadSettings clamps AGAIN on the way back in with the
    identical bounds -- so from the panel's own operator-facing behaviour, "this control does
    not go past its ends" rather than "an out-of-range value is refused" is the whole design.
    Silently dropping the update instead (as validate_command does for the command channel) would
    make this follower behave differently from the panel it exists to mirror, for a file the
    panel is the primary writer of. A value actually forced into range is worth one log line --
    it means something wrote settings.txt outside what the panel itself can produce.
    """
    from relay.settings_follow import Follower

    say = log or (lambda m: print(m, flush=True))

    def _clamp_and_log(key, v, bounds, whole=False):
        clamped = clamp_to_bounds(v, bounds, whole=whole)
        if clamped is None:
            say("[settings] %s=%r is not a finite number; ignored" % (key, v))
            return None
        if clamped != v:
            say("[settings] %s=%r is outside [%s, %s]; clamped to %s"
                % (key, v, bounds[0], bounds[1], clamped))
        return clamped

    def _set_disk_floor(v):
        clamped = _clamp_and_log("disk_floor_gb", v, DISK_FLOOR_GB_BOUNDS)
        if clamped is not None:
            disk_box[0] = clamped

    def _set_ram_floor(v):
        clamped = _clamp_and_log("ram_floor_mb", v, RAM_FLOOR_MB_BOUNDS)
        if clamped is not None:
            ram_box[0] = clamped

    def _set_maxtabs(v):
        clamped = _clamp_and_log("maxtabs", v, TABS_BOUNDS, whole=True)
        if clamped is None:
            return
        # Same split the cockpit's set_maxtabs command makes: under autoscale this knob
        # is the ceiling, otherwise it is the fixed cap.
        if asc_box[0]:
            asc_box[1] = clamped
        else:
            mc_box[0] = clamped

    return (Follower(path_fn or _settings_path)
            .watch("disk_floor_gb", _set_disk_floor)
            .watch("ram_floor_mb", _set_ram_floor)
            .watch("maxtabs", _set_maxtabs)
            .prime())


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


#: What the LAST _settings_float call actually did, per key. Not a second read -- the read
#: that decides is the read that records, because the cockpit rewrites this file in place and
#: a confirming read is an observation of a different moment.
SETTINGS_READ_TRACE = {}


def _settings_float(key, default):
    """Read a float `key=N` from the shared settings.txt (cockpit-written). Falls back.

    Records what it saw in SETTINGS_READ_TRACE[key] so a caller can say WHERE its number came
    from without opening the file again.
    """
    p = "?"
    try:
        p = _settings_path()
        if not os.path.isfile(p):
            SETTINGS_READ_TRACE[key] = "no file at %s" % p
            return default
        for ln in open(p, encoding="utf-8-sig").read().splitlines():
            if ln.startswith(key + "="):
                raw = ln.split("=", 1)[1].strip()
                try:
                    val = float(raw)
                except ValueError:
                    SETTINGS_READ_TRACE[key] = "unparsable line %r in %s" % (ln, p)
                    return default
                try:
                    _st = os.stat(p)
                    _id = " [size=%d mtime=%.0f]" % (_st.st_size, _st.st_mtime)
                except Exception:
                    _id = " [stat failed]"
                SETTINGS_READ_TRACE[key] = "read %r from %s%s" % (raw, p, _id)
                return val
        SETTINGS_READ_TRACE[key] = "no %s= line in %s" % (key, p)
    except Exception as exc:
        SETTINGS_READ_TRACE[key] = "%s reading %s: %s" % (type(exc).__name__, p, exc)
    return default


def _quota_snapshot():
    """The Copilot generative-message meter, for the cockpit's rate gauge. Never raises.

    Returns {} when the meter cannot be read, and the panel then says it has measured nothing
    rather than drawing a comfortable-looking zero -- an empty gauge and a quiet one look
    identical, and only one of them means there is headroom.
    """
    try:
        from relay.quota_meter import snapshot
        return snapshot()
    except Exception:
        return {}


def settings_fanout():
    """The operator's fan-out switch from settings.txt (`fanout=on|off`), or None if unset.

    None is a real answer and not a default: "the operator has not chosen" has to be
    distinguishable from "the operator chose off", or the caller cannot tell which of its own
    fallbacks to apply. The cockpit writes this key and honours it for launches from its own
    button; nothing on the autostart path read it, so the switch did nothing for goals that
    arrive from the tunnel -- which is most of them.
    """
    raw = _settings_text("fanout")
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "on", "true", "yes")


def _settings_text(key):
    """The raw string for `key`, or None when the file has no such line. Never raises."""
    try:
        p = _settings_path()
        if os.path.isfile(p):
            for ln in open(p, encoding="utf-8-sig").read().splitlines():
                if ln.startswith(key + "="):
                    return ln.split("=", 1)[1]
    except Exception:
        pass
    return None


def settings_disk_floor(default=None):
    """The user's reserved C: free-space floor in GB (`disk_floor_gb=N` in settings.txt).
    This is the 'always keep N GB free on C:' admission reserve -- a new eval-bearing tab is
    not opened if it would push C: under this. Falls back to env SWE_DISK_FLOOR_GB (default 6)
    via relay_fleet.DEFAULT_DISK_FLOOR_GB when unset, so the cockpit/env/CLI form one chain."""
    if default is None:
        from relay.relay_fleet import DEFAULT_DISK_FLOOR_GB
        default = DEFAULT_DISK_FLOOR_GB
    return _settings_float("disk_floor_gb", default)


def operator_set_a_disk_floor():
    """Has the operator chosen a disk floor, or is there nothing to respect?

    `settings_disk_floor` substitutes a default when the key is absent, so it cannot answer
    this on its own: a sentinel default is passed and a negative result means "no line in
    settings.txt". ANY real choice counts, including 0 -- the cockpit clamps that control to
    0..100 and therefore offers 0, so treating it as "unset" would substitute a number for one
    the operator picked.

    Callers use this to decide whether to pass --disk-floor-gb at all, because passing the
    flag BEATS the file (CLI > settings > env) and a run that ignores the panel while the panel
    keeps displaying its number is the defect class this repository has spent a week removing.

    Never raises. An unreadable settings file reads as "nothing chosen".
    """
    try:
        return float(settings_disk_floor(default=-1.0)) >= 0
    except Exception:
        return False


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


#: A CLI SUBMISSION USED TO LEAVE NO TRACE UNTIL IT HAD ALREADY SUCCEEDED.
#:
#: tools/fleet_submit writes .fleet/tasks/pending/<id>.json BEFORE anything runs, which is why
#: the cockpit can show a queued job the moment it is submitted. `fleet_runner.py -g "..."`
#: wrote nothing until the run was under way: no queue entry, no history row, and -- if it died
#: before argparse, on a bad path or the wrong interpreter -- not even a coordinator log. From
#: the screen and from every record on disk, a submission that failed early was indistinguish-
#: able from a command nobody typed.
#:
#: Reported 2026-09-18: a goal was submitted through the CLI, was not on the fleet, was not in
#: the history, and could not be found anywhere. The contract in docs/agent_contract.md says
#: work that cannot be confirmed in the GUI does not count as working -- and this route could
#: not be confirmed at all, by construction.
#:
#: So the goals are written into the SAME channel fleet_submit uses, at the first moment they
#: are known, and removed when the run actually starts and the goals ledger takes over. A run
#: that never starts leaves them behind, which is the point: an unclaimed entry on the screen
#: is the difference between "refused" and "never happened".
def _record_cli_submission(state_dir, goals, argv):
    """Write one visible queue entry per CLI goal. Returns their paths. Never raises."""
    import json as _j
    import time as _t

    out = []
    try:
        pend = os.path.join(state_dir, "tasks", "pending")
        os.makedirs(pend, exist_ok=True)
        stamp = int(_t.time())
        for n, g in enumerate(goals or []):
            text = " ".join(str(g if isinstance(g, str) else (g or {}).get("text", "")).split())
            if not text:
                continue
            jid = "cli%d_%d_%d" % (stamp, os.getpid(), n)
            rec = {
                "id": jid,
                "type": "fleet_goal",
                "payload": {"goal": text},
                "created": _t.time(),
                # SAME SHAPE AS fleet_submit's, so one reader serves both routes and the
                # difference in authority stays legible.
                "origin": {"via": "cli", "source": " ".join(str(a) for a in (argv or [])[:6])[:300]},
                # WHOSE ENTRY THIS STILL IS. These land in tasks/pending/, which is not a
                # display surface -- it is task_router's inbox, and dispatch_once claims every
                # .json in it. So writing one here to make the submission VISIBLE also offered
                # it for dispatch, and the window is everything between this line and
                # _clear_cli_submission: reading goals, bringing Edge up, the whole startup.
                # The router polls every couple of seconds. Measured artifact in the tree:
                # cli1789703602_14000_0.delivered.json, a CLI entry the router delivered.
                #
                # The distinction that was missing is "still mine" versus "abandoned", and
                # this pid is it. The router skips an entry whose owner is alive and takes one
                # whose owner is gone -- which is the recovery the comment above this function
                # actually wanted: a run that never starts must not leave the goal stranded.
                "owner_pid": os.getpid(),
            }
            p = os.path.join(pend, "%s.json" % jid)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                _j.dump(rec, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
            out.append(p)
    except Exception:
        pass
    return out


def _clear_cli_submission(paths):
    """Drop the queue entries once the run has really started. Never raises."""
    for p in paths or []:
        try:
            os.remove(p)
        except OSError:
            pass


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


#: Invariants that STOP a launch. Deliberately a short, named list rather than "everything the
#: checkpoint reports": the checkpoint also reports things that are worth a human's eye but not
#: worth refusing over -- memory near a ceiling, a route already closed -- and a gate that
#: refuses on those is a gate that gets bypassed by habit and then deleted.
#:
#: These two are the ones that make a run's results untrue rather than merely worse:
#:   a Copilot page nobody owns -> the browser carries ~350-560 MB into every measurement
#:   a browser with a window     -> a token capture will raise it onto the user's screen
BLOCKING_INVARIANTS = ("no idle Copilot page", "no browser window")


def _launch_blockers():
    """The unmet preconditions that should stop a launch, as (name, detail). Never raises.

    A gate that crashes is worse than no gate: it turns "the stack is dirty" into "no run can
    start for a reason nobody can see". Any failure to ASK is treated as nothing to report.
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "checkpoint", os.path.join(_repo_root(), "scripts", "win", "checkpoint.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # memory=False: the gate reads verdicts, never extras["memory"], and that figure is
        # a 4-5 s PowerShell query per browser -- 9.3 s of every launch, measured 2026-09-24.
        verdicts, _extras = mod.verdicts_now(memory=False)
        return [(name, detail) for name, ok, detail in verdicts
                if not ok and name in BLOCKING_INVARIANTS]
    except Exception as exc:
        print("[gate] could not evaluate preconditions (%s); starting anyway"
              % type(exc).__name__, flush=True)
        return []


def _pid_alive(pid: int) -> bool:
    """Is that process running? psutil is already a dependency and is cross-platform."""
    try:
        import psutil
        return bool(psutil.pid_exists(int(pid)))
    except Exception:
        return True          # cannot tell -> never treat somebody else's page as residue


def _unclaimed_pages(pages):
    """Of these Copilot pages, the ones no LIVING run has claimed.

    Reconciliation, not a guess: relay.ownership holds what each run says it owns, with a
    lease and a pid, and returns only the claims that survive both. A page whose owner died
    without releasing is residue; a page a live run is working in is not.
    """
    try:
        from relay import ownership
        from relay.relay_fleet import _page_target_id
        observed = {}
        by_id = {}
        for pg in pages:
            tid = _page_target_id(pg)
            if not tid:
                continue                     # cannot identify it -> do not touch it
            observed[("page", tid)] = pg.url
            by_id[("page", tid)] = pg
        result = ownership.reconcile(observed, _pid_alive)
        return [by_id[k] for k in result["orphaned"] if k in by_id]
    except Exception:
        return []                            # unsure -> close nothing


def _close_idle_copilot_pages(context) -> int:
    """Close Copilot pages nobody owns yet, and say how many. Never raises.

    Shares its reasoning with the end-of-run cleanup in relay_fleet: a blank page is opened
    first when the stale ones are all that is left, because Edge exits with its final page and
    taking the browser down would end the next run's SSO with it. If that replacement cannot
    be made, nothing is closed -- an untidy browser is better than no browser.
    """
    try:
        candidates = [p for p in context.pages if "m365.cloud.microsoft" in (p.url or "")]
        if not candidates:
            return 0
        # OWNERSHIP, NOT PRESENCE. This used to close every Copilot page on the context while
        # its docstring claimed they were unowned. Concurrent runs are ordinary here, so the
        # second run to start would close the first one's working page -- and the first would
        # see a TargetClosedError it could not explain. Only a page no LIVING run claims is
        # residue.
        stale = _unclaimed_pages(candidates)
        if not stale:
            return 0
        if len(stale) >= len(context.pages):
            try:
                context.new_page().goto("about:blank", timeout=10000)
            except Exception:
                return 0
        closed = 0
        for p in stale:
            try:
                p.close()
                closed += 1
            except Exception:
                pass
        return closed
    except Exception:
        return 0


def _snapshot(workers, started, total, max_concurrent=0, disk_floor_gb=0.0, paused=False,
              ram_floor_mb=0.0, directive="", run_label="", goal_count=0, queued=0,
              reunlock=None, command_rejections=None):
    from relay.relay_fleet import free_disk_gb
    total = len(workers)        # dynamic: goals can be added mid-run (native chat queue)
    done = sum(1 for w in workers if w.status in TERMINAL)
    # open_tabs = ACTUAL browser tabs across the fleet: main agent tabs PLUS open sub-agent
    # side-pages (research / refuter). Counts real tabs (not just workers) so the cockpit's
    # tab/RAM display matches what the tab-budget admission gates on -- an auto worker mid-fan-out
    # shows as up to 3 tabs. Falls back to the main-tab count if tab_load isn't available.
    open_tabs = sum((w.tab_load() if hasattr(w, "tab_load") else
                     (1 if getattr(w, "page", None) is not None else 0)) for w in workers)
    _snap = {
        "started": started,
        "updated": time.time(),
        "total": total,
        "done_count": done,
        "queued": int(queued or 0),
        # RUNNING MEANS THERE IS WORK, NOT THAT A WORKER EXISTS YET. `queued` counts goals
        # accepted but not yet turned into workers, and without it a fan-out run declares
        # itself finished the instant it splits: the parent goes terminal at turn 1 and its
        # children are still in the queue, so done==total and running goes False -- for the
        # whole hour the children then take. The watchdog reads this flag and skips a run
        # that is not running, so the wedge detector switched itself off at the exact moment
        # the run started doing its real work. A capture that blocked the main loop for ten
        # minutes on 2026-08-26 went unnoticed for precisely that reason.
        "running": (done < total) or int(queued or 0) > 0,
        "paused": bool(paused),        # fleet frozen by the cockpit (pause toggle)
        "max_concurrent": max_concurrent,
        "open_tabs": open_tabs,
        "avail_mb": round(avail_phys_mb()),
        # disk admission reserve + current C: free, so the cockpit can show the disk gate.
        "disk_floor_gb": round(disk_floor_gb, 1),
        "free_disk_gb": round(free_disk_gb(), 1),
        # RAM admission reserve (free RAM kept for the user) so the cockpit can show the RAM gate.
        "ram_floor_mb": round(ram_floor_mb),
        # THE THIRD GATE, and the one that was invisible. Disk and RAM have been on this panel
        # for months; the quota that actually stopped a run -- 217 turns refused out of 237 --
        # had no gauge at all, so a fleet grinding to a halt looked like a fleet being slow.
        # Turns per minute against Microsoft's published generative-orchestration limit, with
        # the refusals shown beside it: the published figure is scoped per Dataverse
        # environment and downstream services may impose lower ones, so refusals arriving under
        # the line mean the line is the wrong line, not that the gauge is broken.
        "quota": _quota_snapshot(),
        # THE FALLBACK BUTTON'S OWN RECEIPT. None until a {"reunlock":...} command has been
        # applied at least once this run; after that,
        # {"ts","target","ok","delivered","reason"} from apply_reunlock -- so "I pressed the
        # button and nothing happened" has an answer on this same screen instead of nowhere.
        # NEVER a password: apply_reunlock builds "reason" only from deliver_steers' own log
        # lines, which never format the turn text.
        "reunlock": reunlock,
        # COMMANDS THIS RUN REFUSED (SEC-08), newest last, at most MAX_REJECTIONS_KEPT:
        # {"ts","keys","errors"} from record_command_rejection. A command that fails the
        # channel's schema is not applied at all, and without this a refused pause or floor
        # change would look exactly like one the fleet ignored. Keys and reasons only.
        "command_rejections": list(command_rejections or []),
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
            # THE ADMISSION-TIME ID (codex-plan item 1, 2026-09-09) -- NOT run_id below.
            # run_id names the fleet SWEEP; jid names the ADMITTED GOAL, minted once by
            # task_router.py at submission and carried through add_goal_to_live_fleet /
            # goals_from_command / autostart_fleet into Worker.jid. This is what lets
            # .fleet/tasks/done/<jid>.json (admission), .fleet/acks/<jid>.ack (delivery --
            # written by read_commands below and read by task_router.fleet_landing_confirmed;
            # NOT .fleet/acked/, which this comment used to name and which nothing in the tree
            # writes: its 18 files are 32-hex names from before 2026-09-08, a different id
            # shape from jid's 12, and a reader sent there finds a dead end),
            # and this worker's own verified/verify_attempts above be joined on ONE id, which
            # is exactly the evidence bar the plan named: "同一run IDで受付・発火・実行・
            # 検証・終了を結ぶ". Empty for goals that never passed through admission.
            "jid": getattr(w, "jid", None) or "",
            # THE NAME OF THE RUN THIS WORKER BELONGS TO, stated rather than left implicit.
            #
            # Four notions of "run" exist in the ledgers and none of them join: ownership.jsonl
            # keys on a process id, mechanisms.jsonl on an epoch, history.json on
            # "<epoch>#<worker>", and the transcripts on "<run_id>_<name>" -- while judge.jsonl
            # (5,712 verdicts) and skill_use.jsonl carry no identity at all. Measured across
            # every ledger: no two share a value under any identity-shaped key, so not one of
            # those verdicts can be attached to the work that provoked it.
            #
            # The transcripts hold the only identity that names a run rather than a process or
            # a moment, and every worker already carries its transcript path -- so the id is
            # present, spelled into a filename, where nothing can join on it. Lift it out and
            # publish it under its own name; the file name stays the source of truth.
            "run_id": _run_id_of(w, started),
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
            "task_id": getattr(getattr(w, "task_envelope", None), "task_id", ""),
            "parent_task_id": getattr(getattr(w, "task_envelope", None), "parent_task_id", None),
            "campaign_id": getattr(getattr(w, "task_envelope", None), "campaign_id", ""),
            "role": getattr(getattr(w, "task_envelope", None), "role", ""),
            "depth": getattr(getattr(w, "task_envelope", None), "depth", 0),
            "subtask_index": getattr(w, "subtask_index", None),
            "goal_hash": getattr(w, "original_goal_hash", ""),
            "fresh_replay_count": getattr(w, "fresh_replay_count", 0),
            "refusal_count": getattr(w, "refusal_count", 0),
            "refusal_history": list(getattr(w, "refusal_history", [])),
            "recovery_cause": getattr(w, "recovery_cause", ""),
            "recovery_result": getattr(w, "recovery_result", ""),
            "recovery_state": getattr(w, "recovery_state", ""),
            "attempt_transcripts": list(getattr(w, "attempt_transcripts", [])),
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
    # Derived fan-out family markers (parent / child / aggregator / stalled) so the
    # cockpit can render the split-and-merge structure the lineage already implies.
    _fv = fanout_family_view(_snap["workers"])
    for _w in _snap["workers"]:
        _w["fanout"] = _fv.get(_w["name"], {"kind": "solo", "campaign_id": _w.get("campaign_id", ""), "label": ""})
    return _snap


#: How long to keep trying to replace a status file a reader is holding open. The cockpit
#: polls status.json about once a second and holds it for a few milliseconds; a second of
#: retries covers that by a wide margin without turning a real permission problem into a hang.
_REPLACE_DEADLINE_S = 1.0


def _write_atomic(path, payload):
    """Write `payload` as JSON, replacing `path` atomically.

    RETRIED, BECAUSE A READER CAN REFUSE THE REPLACEMENT. os.replace is atomic on Windows and
    POSIX both -- and on Windows it is also DENIED while another process holds the destination
    open without FILE_SHARE_DELETE. The cockpit reads this file about once a second, so the
    collision is not bad luck; it is a reader that is always there. Measured 2026-09-17: a
    coordinator died before its first turn on WinError 5 replacing status.json, and the queue
    recorded the run as started.

    The deadline is short and the exception is re-raised after it. A status file that truly
    cannot be written is a real failure -- a fleet with no status is a fleet the panel shows
    as dead -- so this turns a lost race into a delay, never into a silent skip.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    deadline = time.time() + _REPLACE_DEADLINE_S
    while True:
        try:
            os.replace(tmp, path)   # atomic on Windows + POSIX
            return
        except PermissionError:
            if time.time() >= deadline:
                raise
            time.sleep(0.05)


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
    if isinstance(goal, dict):
        out = dict(goal)
        out.update({"text": text, "checks": checks, "cwd": cwd,
                    "priority": priority, "key": _goal_key(text)})
        return out
    return {"text": text, "checks": checks, "cwd": cwd,
            "priority": priority, "key": _goal_key(text)}


def _ledger_to_goal(entry):
    """Turn a ledger entry back into the goal form the normal pipeline expects: a dict
    with text/checks/cwd/priority (checks/cwd only when present so a plain goal stays
    minimal). goal_fields reads text/checks/cwd downstream; priority is honoured by the
    add-goal queue if the goal is later re-queued."""
    g = dict(entry or {})
    g.pop("key", None)
    g["text"] = entry.get("text", "")
    return g


def _write_goals_ledger(state_dir, goals, started, raise_on_error=False):
    """Write the durable goals ledger once at run start.

    Normal launches keep the historical best-effort behaviour. ``raise_on_error=True`` is used
    when adopting a pending command, because that command must not be committed away until the
    ledger is known durable.
    """
    try:
        payload = {"started": started,
                   "goals": [_normalize_goal_for_ledger(g) for g in goals]}
        _write_atomic(os.path.join(state_dir, LAST_RUN_GOALS), payload)
        return True
    except Exception as e:
        if raise_on_error:
            raise
        sys.stderr.write("[resume] WARN: could not write goals ledger: %s\n" % e)
        return False


def _append_goals_ledger(state_dir, goals, started, raise_on_error=False):
    """Durably append live ``add_goal`` items to the current run ledger.

    The original ledger was written only once at launch, which meant every task accepted
    later through the live command channel vanished from ``--resume`` after a crash.  Keep
    the existing stable-key semantics: repeated delivery of the same goal text is idempotent.
    Returns the number of newly persisted entries. Best-effort, matching the run-start writer.
    """
    if not goals:
        return 0
    try:
        existing_started, existing = _read_goals_ledger(state_dir)
        out = list(existing or [])
        seen = set()
        for e in out:
            if isinstance(e, dict):
                seen.add(e.get("key") or _goal_key(e.get("text", "")))
        added = 0
        for goal in goals:
            e = _normalize_goal_for_ledger(goal)
            key = e.get("key")
            if key in seen:
                continue
            out.append(e)
            seen.add(key)
            added += 1
        if added:
            payload = {"started": existing_started if existing_started is not None else started,
                       "goals": out}
            _write_atomic(os.path.join(state_dir, LAST_RUN_GOALS), payload)
        return added
    except Exception as e:
        if raise_on_error:
            raise
        if not getattr(_append_goals_ledger, "_warned", False):
            sys.stderr.write("[resume] WARN: could not append live goal to ledger: %s\n" % e)
            _append_goals_ledger._warned = True
        return 0


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


def _merge_final_done_map(state_dir, results):
    """Merge successful outcomes from one final chunk into the durable done-map.

    ``run_relay_fleet`` may return only the workers from the last reconnect chunk, and a
    graceful stop can return just the workers active at stop time. Replacing the file from
    that partial ``results`` list erases DONE outcomes from earlier chunks and makes
    ``--resume`` replay work that already succeeded. Existing DONE keys are therefore
    monotonic for the life of the ledger.
    """
    done = _read_done_map(state_dir)
    for r in results or []:
        try:
            outcome = r.get("outcome")
            goal = r.get("goal") or ""
        except Exception:
            continue
        if outcome in _RESUME_SUCCESS_OUTCOMES and goal:
            done[_goal_key(goal)] = outcome
    _write_atomic(os.path.join(state_dir, LAST_RUN_DONE), done)
    return done


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


# ── RUN-ACTIVE marker ───────────────────────────────────────────────────────────
# A run that dies from a PC reboot / kill / crash leaves no trace that it was ever
# interrupted -- nothing relaunches it. ACTIVE_MARKER is a small sidecar written ONCE at
# run start and REMOVED on clean completion or an explicit user stop (KeyboardInterrupt,
# or the graceful `stop` command via commands.json, both of which reach the same normal
# end-of-main() completion path). Its PRESENCE with a DEAD pid is therefore the signal a
# boot-time supervisor (scripts/supervisor.ps1) uses to detect an interrupted run and
# relaunch it with --resume. Best-effort throughout: a marker read/write/remove failure
# is logged (write) or silently tolerated (read/clear) and never takes down the run.
ACTIVE_MARKER = "fleet_run_active.json"
RUN_LOCK_FILE = "fleet_runner.lock"


def _resume_argv(argv):
    """Strip goal-specifying flags (-g/--goal, --goals-file, --adopt-command) and any
    existing --resume from an argv list, returning the remainder suitable for relaunching with a
    single --resume appended. --resume alone reconstructs the goal set from the durable
    ledger (last_run_goals.json); replaying the ORIGINAL -g/--goals-file on top would
    duplicate goals (both the already-finished and the unfinished ones get re-added
    alongside the resume set -- see _resume_goals). Pure function, no I/O -- easy to
    unit test in isolation."""
    out = []
    skip_next = False
    for a in argv:
        if skip_next:
            skip_next = False
            continue
        if a in ("-g", "--goal", "--goals-file", "--adopt-command"):
            skip_next = True
            continue
        if a.startswith("--goal=") or a.startswith("--goals-file=") or a.startswith("--adopt-command="):
            continue
        if a == "--resume":
            continue
        out.append(a)
    return out


def _write_active_marker(state_dir, argv=None, pid=None, start_ts=None):
    """Best-effort: record this run as ACTIVE (pid, start_ts, argv, and a precomputed
    resume_argv) so a supervisor can detect an interrupted run later. Never raises -- a
    marker failure is logged once to stderr and the run continues untouched."""
    try:
        raw_argv = list(argv if argv is not None else sys.argv[1:])
        payload = {"pid": int(pid if pid is not None else os.getpid()),
                   "start_ts": float(start_ts if start_ts is not None else time.time()),
                   "argv": raw_argv,
                   "resume_argv": _resume_argv(raw_argv)}
        _write_atomic(os.path.join(state_dir, ACTIVE_MARKER), payload)
    except Exception as e:
        sys.stderr.write("[resume] WARN: could not write active-run marker: %s\n" % e)


def _read_active_marker(state_dir):
    """Read fleet_run_active.json tolerantly. Missing/corrupt -> None (utf-8-sig
    tolerates a BOM, matching the other sidecar readers in this module)."""
    path = os.path.join(state_dir, ACTIVE_MARKER)
    try:
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _active_run_conflict_pid(state_dir, self_pid=None):
    """Return the live pid that already owns this state dir, else 0.

    This is the migration guard for runners started before the OS lock existed, and also makes
    the refusal human-readable. Unknown pid state is treated as alive by `_pid_alive`, on purpose:
    overwriting another coordinator's ledger/status is worse than refusing one launch.
    """
    marker = _read_active_marker(state_dir)
    if not marker:
        return 0
    try:
        pid = int(marker.get("pid") or 0)
        me = int(os.getpid() if self_pid is None else self_pid)
    except Exception:
        return 0
    if pid <= 0 or pid == me:
        return 0
    return pid if _pid_alive(pid) else 0


def _acquire_run_lock(state_dir):
    """Non-blocking OS lock for one fleet coordinator per state directory.

    A marker file is evidence, not exclusion: two launchers can both inspect a missing/stale
    marker before either writes its own. The kernel byte-range lock closes that race and is
    automatically released when a killed process dies, so it cannot become a stale lock.
    Returns the open lock handle on success, None when another coordinator owns it.
    """
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, RUN_LOCK_FILE)
    fh = None
    try:
        fh = open(path, "a+b", buffering=0)
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\0")
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except (OSError, IOError):
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        return None


def _release_run_lock(fh):
    """Release a handle returned by `_acquire_run_lock`; safe on None/already-gone."""
    if fh is None:
        return
    try:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def _clear_active_marker(state_dir, owner_pid=None):
    """Remove only THIS coordinator's ACTIVE marker.

    The old implementation unconditionally removed the path. Measured 2026-09-26: runner A
    finished cleanup after runner B had already written its fresh marker, so A deleted B's marker
    and external resume paths started more coordinators on the same `.fleet`. Ownership is part
    of the delete now. Missing/corrupt/mismatched marker is a safe no-op. Returns True iff deleted.
    """
    owner = int(os.getpid() if owner_pid is None else owner_pid)
    marker = _read_active_marker(state_dir)
    try:
        marker_pid = int((marker or {}).get("pid") or 0)
    except Exception:
        return False
    if marker_pid <= 0 or marker_pid != owner:
        return False
    try:
        p = os.path.join(state_dir, ACTIVE_MARKER)
        os.remove(p)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def should_auto_resume(marker_exists, pid_alive, user_stopped=False):
    """PURE decision: should a boot-time supervisor relaunch an interrupted fleet run?

    marker_exists  -- ACTIVE_MARKER is present (a run believes itself to still be live)
    pid_alive      -- the pid recorded in the marker is still a running process
    user_stopped   -- a persistent signal (if any) that the user explicitly asked to stop
                       this run (kept as a parameter for completeness / future-proofing;
                       today an explicit stop -- Ctrl+C or the `stop` command -- already
                       reaches the normal completion path and clears the marker itself,
                       so in practice marker_exists and user_stopped=True do not co-occur)

    Resume iff: the marker is present, its pid is DEAD, and the user did not explicitly
    stop the run. Any of (marker absent / pid alive / user-stopped) -> do nothing. Kept
    side-effect free so it is trivially unit-testable and so scripts/supervisor.ps1's
    PowerShell mirror of this same boolean rule is easy to keep in sync."""
    return bool(marker_exists) and not pid_alive and not user_stopped


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


COMMANDS_DIR = "commands.d"

#: Where landing receipts go: <state_dir>/acks/<jid>.ack. The same layout relay/task_router's
#: _ack_path builds on the sending side.
ACKS_DIR = "acks"

# ---------------------------------------------------------------- the command channel's schema
#
# EVERY FIELD IS CHECKED BEFORE ANY OF IT IS APPLIED (SEC-08). _apply_command used to take
# whatever a file in commands.d/ said: a disk floor of 1e9 GB or -5, a RAM floor of NaN, a
# steer of any size, an `ack` that named any path on the machine. Each field was coerced where
# it was used and a bad one was swallowed by a blanket except -- so a malformed command did
# half of what it said and nobody was told. Now a command either passes whole or is refused
# whole, and the refusal is recorded where the operator looks (status.json's
# `command_rejections`, the fleet console, and the landing receipt when there is one).
#
# THE NUMERIC BOUNDS ARE THE SETTINGS PANEL'S OWN. ui/FleetCockpit.cs clamps SetDiskFloor to
# [0, 100] GB, SetRamFloor to [0, 65536] MB, and maxtabs / autoscale_max to [1, 100] (both at
# load and on every change). A value the panel cannot produce is not one the channel accepts:
# the file is not a second, wider settings UI. 0 stays legal for the disk floor because the
# panel's own 強制開始 button sends exactly that.

DISK_FLOOR_GB_BOUNDS = (0.0, 100.0)
RAM_FLOOR_MB_BOUNDS = (0.0, 65536.0)
TABS_BOUNDS = (1, 100)
#: Goal / steer text. Generous: the chat window sends a pasted instruction verbatim.
MAX_COMMAND_TEXT = 100_000
#: A worker name, a reunlock target.
MAX_NAME = 64
#: A conversation reference or a working directory.
MAX_REF = 4096
#: Entries in one list-valued field (close, steer, add_goal).
MAX_ITEMS = 200
MAX_CHECKS = 50
MAX_CHECKS_JSON = 65_536

_COMMAND_KEYS = frozenset({
    "close", "set_maxtabs", "set_disk_floor_gb", "set_ram_floor_mb", "set_autoscale",
    "steer", "reunlock", "add_goal", "pause", "stop", "ack",
})
_AUTOSCALE_KEYS = frozenset({"on", "max", "default"})
_STEER_KEYS = frozenset({"worker", "text"})
#: Exactly the fields goals_from_command carries through.
_GOAL_KEYS = frozenset({"text", "priority", "checks", "cwd", "jid", "follow_up_to",
                        "resume_conv", "new_task"})

#: A job id / ack stem: what task_router mints (uuid hex) with room for other callers' ids,
#: and nothing that can name a different directory or a device.
_ID_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_WIN_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + ["COM%d" % i for i in range(1, 10)]
    + ["LPT%d" % i for i in range(1, 10)])
_CONTROL = _re.compile(r"[\x00-\x1f\x7f]")


def _is_number(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and _math.isfinite(v))


def _is_whole(v) -> bool:
    return _is_number(v) and float(v).is_integer()


def _is_flag(v) -> bool:
    return isinstance(v, bool) or (isinstance(v, int) and v in (0, 1))


def _is_name(v, allow_empty=True) -> bool:
    return (isinstance(v, str) and len(v) <= MAX_NAME and not _CONTROL.search(v)
            and (allow_empty or bool(v.strip())))


def _is_safe_id(v) -> bool:
    return (isinstance(v, str) and bool(_ID_RE.match(v))
            and v.split(".")[0].upper() not in _WIN_DEVICE_NAMES)


def _text_ok(v, limit=MAX_COMMAND_TEXT) -> bool:
    return isinstance(v, str) and len(v) <= limit


def _in(v, bounds) -> bool:
    return bounds[0] <= v <= bounds[1]


def clamp_to_bounds(v, bounds, whole=False):
    """`v` forced into `bounds` (inclusive) -- the SAME numeric range validate_command enforces
    for this same knob (DISK_FLOOR_GB_BOUNDS / RAM_FLOOR_MB_BOUNDS / TABS_BOUNDS), so the command
    channel and the settings-file follower cannot silently drift apart into two different
    answers for "how big may this number be" (see build_settings_follower).

    Returns None when `v` is not a finite number at all (NaN, +/-inf, or something that will
    not convert to float) -- that is not "out of range", it is not a number, and the caller
    leaves the live value untouched rather than adopting nonsense. Otherwise always returns a
    number inside `bounds`, rounded to a whole number first when `whole` is set (mirrors
    validate_command's _is_whole check for set_maxtabs / set_autoscale).
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not _math.isfinite(f):
        return None
    if whole:
        f = round(f)
    lo, hi = bounds
    clamped = min(max(f, lo), hi)
    return int(clamped) if whole else clamped


def _ack_name(claimed):
    """The receipt's file name taken from a command's `ack`, or None if it is not one."""
    if not isinstance(claimed, str) or not claimed or len(claimed) > MAX_REF:
        return None
    name = claimed.replace("\\", "/").rsplit("/", 1)[-1]
    if not name.endswith(".ack") or not _is_safe_id(name[:-len(".ack")]):
        return None
    return name


def _same_dir(a, b) -> bool:
    """Whether two paths name ONE directory: by file identity when both exist, else by their
    fully resolved, case-folded spelling. Spelling alone would be fooled by 8.3 names and
    junctions; identity alone cannot speak about a directory not created yet."""
    try:
        if os.path.isdir(a) and os.path.isdir(b):
            return os.path.samefile(a, b)
        return (os.path.normcase(os.path.realpath(a))
                == os.path.normcase(os.path.realpath(b)))
    except (OSError, ValueError):
        return False


def ack_receipt_path(state_dir, claimed):
    """Where the landing receipt for a command whose `ack` says `claimed` is written, or None.

    DERIVED HERE, NEVER TAKEN FROM THE FILE (SEC-08). read_commands used to makedirs() and
    open(..., "w") whatever path the command named, so anything able to drop a JSON file into
    commands.d/ could create directories and overwrite files anywhere this account can write,
    including outside a folder the MCP file tools were scoped to. The receipt now always lands
    in <state_dir>/acks/, under a file name checked to be a plain id; and a command whose
    `ack` names any OTHER directory is refused rather than quietly redirected, because that is
    not a sender that knows where this fleet lives.
    """
    name = _ack_name(claimed)
    if name is None:
        return None
    acks = os.path.join(state_dir, ACKS_DIR)
    if not _same_dir(os.path.dirname(claimed) or ".", acks):
        return None
    return os.path.join(acks, name)


def validate_command(cmd, state_dir=None) -> list:
    """Every reason `cmd` may not be applied; [] means it may. Never raises.

    Strict on purpose: an unknown key is an error, not something to skip, because the
    alternative is a command that did half of what its sender meant while reporting nothing.
    Error strings name keys and limits, never the text a field carried -- a steer or a goal
    is the operator's words and has no business in status.json. With `state_dir`, an `ack`
    is also checked to name that state dir's own acks/ directory (see ack_receipt_path).
    """
    try:
        return _validate_command(cmd, state_dir)
    except Exception as exc:                           # a validator bug refuses, never admits
        return ["validator error: %s" % type(exc).__name__]


def _validate_command(cmd, state_dir):
    if not isinstance(cmd, dict):
        return ["command is %s, not an object" % type(cmd).__name__]
    errs = []
    if not cmd:
        errs.append("empty command")
    for k in cmd:
        if k not in _COMMAND_KEYS:
            errs.append("unknown key %r" % _CONTROL.sub("?", str(k))[:40])

    def _items(key, v):
        items = v if isinstance(v, list) else [v]
        if len(items) > MAX_ITEMS:
            errs.append("%s: %d entries, limit %d" % (key, len(items), MAX_ITEMS))
            return []
        return items

    if "close" in cmd:
        v = cmd["close"]
        if not isinstance(v, list) or len(v) > MAX_ITEMS:
            errs.append("close: must be a list of at most %d worker names" % MAX_ITEMS)
        elif not all(_is_name(n, allow_empty=False) for n in v):
            errs.append("close: every entry must be a worker name (1-%d chars)" % MAX_NAME)
    if "set_maxtabs" in cmd:
        v = cmd["set_maxtabs"]
        if not (_is_whole(v) and _in(v, TABS_BOUNDS)):
            errs.append("set_maxtabs: must be a whole number in [%d, %d]" % TABS_BOUNDS)
    if "set_disk_floor_gb" in cmd:
        v = cmd["set_disk_floor_gb"]
        if not (_is_number(v) and _in(v, DISK_FLOOR_GB_BOUNDS)):
            errs.append("set_disk_floor_gb: must be a number in [%g, %g]" % DISK_FLOOR_GB_BOUNDS)
    if "set_ram_floor_mb" in cmd:
        v = cmd["set_ram_floor_mb"]
        if not (_is_number(v) and _in(v, RAM_FLOOR_MB_BOUNDS)):
            errs.append("set_ram_floor_mb: must be a number in [%g, %g]" % RAM_FLOOR_MB_BOUNDS)
    if "set_autoscale" in cmd:
        v = cmd["set_autoscale"]
        if not isinstance(v, dict):
            errs.append("set_autoscale: must be an object")
        else:
            extra = sorted(str(k)[:20] for k in v if k not in _AUTOSCALE_KEYS)
            if extra:
                errs.append("set_autoscale: unknown key(s) %s" % extra[:5])
            if "on" in v and not _is_flag(v["on"]):
                errs.append("set_autoscale.on: must be true/false or 0/1")
            for sub in ("max", "default"):
                # None / 0 mean "not given" -- the reader has always skipped a falsy value.
                if sub in v and v[sub] not in (None, 0) and not (
                        _is_whole(v[sub]) and _in(v[sub], TABS_BOUNDS)):
                    errs.append("set_autoscale.%s: must be a whole number in [%d, %d]"
                                % ((sub,) + TABS_BOUNDS))
    if "steer" in cmd:
        for it in _items("steer", cmd["steer"]):
            if isinstance(it, str):
                ok = _text_ok(it)
            elif isinstance(it, dict):
                ok = (not (set(it) - _STEER_KEYS)
                      and (it.get("worker") is None or _is_name(it.get("worker")))
                      and _text_ok(it.get("text", "")))
            else:
                ok = False
            if not ok:
                errs.append("steer: each entry must be text or {worker, text} "
                            "(worker <= %d chars, text <= %d chars)" % (MAX_NAME, MAX_COMMAND_TEXT))
                break
    if "reunlock" in cmd:
        v = cmd["reunlock"]
        if v is not None and not _is_name(v):
            errs.append("reunlock: must be a worker name, \"\" or \"*\"")
    if "add_goal" in cmd:
        for it in _items("add_goal", cmd["add_goal"]):
            why = _goal_item_error(it)
            if why:
                errs.append("add_goal: " + why)
                break
    for key in ("pause", "stop"):
        if key in cmd and not _is_flag(cmd[key]):
            errs.append("%s: must be true/false" % key)
    if "ack" in cmd:
        if _ack_name(cmd["ack"]) is None:
            errs.append("ack: must name <state>/acks/<id>.ack")
        elif state_dir is not None and ack_receipt_path(state_dir, cmd["ack"]) is None:
            errs.append("ack: names a directory other than this fleet's acks/")
    return errs


def _goal_item_error(it):
    if isinstance(it, str):
        return "" if _text_ok(it) else "text longer than %d chars" % MAX_COMMAND_TEXT
    if not isinstance(it, dict):
        return "an entry is %s, not text or an object" % type(it).__name__
    extra = sorted(str(k)[:20] for k in it if k not in _GOAL_KEYS)
    if extra:
        return "unknown key(s) %s" % extra[:5]
    if it.get("text") is not None and not _text_ok(it["text"]):
        return "text must be a string of at most %d chars" % MAX_COMMAND_TEXT
    if it.get("follow_up_to") is not None and not _text_ok(it["follow_up_to"]):
        return "follow_up_to must be a string of at most %d chars" % MAX_COMMAND_TEXT
    for key in ("cwd", "resume_conv"):
        v = it.get(key)
        if v is not None and not (_text_ok(v, MAX_REF) and not _CONTROL.search(v)):
            return "%s must be a string of at most %d chars" % (key, MAX_REF)
    for key in ("priority", "new_task"):
        if it.get(key) is not None and not _is_flag(it[key]):
            return "%s must be true/false" % key
    if it.get("jid") is not None and not _is_safe_id(it["jid"]):
        return "jid must be a plain id"
    checks = it.get("checks")
    if checks is not None:
        rows = checks if isinstance(checks, list) else [checks]
        if not all(isinstance(c, dict) for c in rows) or len(rows) > MAX_CHECKS:
            return "checks must be an object or a list of at most %d objects" % MAX_CHECKS
        try:
            size = len(json.dumps(checks, ensure_ascii=False))
        except (TypeError, ValueError):
            return "checks are not serialisable"
        if size > MAX_CHECKS_JSON:
            return "checks larger than %d bytes" % MAX_CHECKS_JSON
    return ""


#: How many refused commands status.json keeps. Enough to see a pattern, bounded so a flood of
#: bad files cannot grow the file the cockpit re-reads every second.
MAX_REJECTIONS_KEPT = 20


def record_command_rejection(box, cmd, errors, log=None):
    """Remember a refused command in `box` (surfaced as status.json's `command_rejections`)
    and say so on the console. Records the command's KEYS and the reasons, never its values:
    a steer or a goal is the operator's own words."""
    say = log or (lambda m: print(m, flush=True))
    keys = (sorted(_CONTROL.sub("?", str(k))[:40] for k in cmd)[:12]
            if isinstance(cmd, dict) else [])
    row = {"ts": time.time(), "keys": keys, "errors": list(errors)[:10]}
    box.append(row)
    del box[:-MAX_REJECTIONS_KEPT]
    say("[command] REJECTED %s: %s" % (",".join(keys) or "(no keys)", "; ".join(row["errors"])))
    return row


def admit_command(cmd, state_dir, rejections, log=None) -> bool:
    """The gate _apply_command passes every command through before touching anything.
    True: apply it. False: it was refused and the refusal is already in `rejections`."""
    errs = validate_command(cmd, state_dir)
    if errs:
        record_command_rejection(rejections, cmd, errs, log=log)
        return False
    return True


def _write_receipt(state_dir, claimed, body) -> bool:
    """Drop a landing receipt at the derived path. Never follows what already sits there.

    Written to a temp file in acks/ and renamed over the target, so a link or a hard link
    planted at the receipt's name is REPLACED, not written through. And acks/ itself must
    resolve to a directory whose parent is the state dir: a junction put in its place would
    otherwise carry the write somewhere else.
    """
    target = ack_receipt_path(state_dir, claimed)
    if target is None:
        return False
    acks = os.path.dirname(target)
    tmp = None
    try:
        os.makedirs(acks, exist_ok=True)
        if not _same_dir(os.path.dirname(os.path.realpath(acks)), state_dir):
            print("[command] ack NOT written: %s does not resolve inside the state dir"
                  % ACKS_DIR, flush=True)
            return False
        tmp = "%s.%d.%d.tmp" % (target, os.getpid(), time.time_ns())
        with open(tmp, "w", encoding="utf-8", newline="\n") as afh:
            json.dump(body, afh, ensure_ascii=False)
        os.replace(tmp, target)
        return True
    except OSError:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def _claim_owner_pid(path):
    """Owner pid encoded in ``...claim-<pid>``; 0 for anything else."""
    name = os.path.basename(path)
    tag = ".claim-"
    if tag not in name:
        return 0
    tail = name.split(tag, 1)[1].split(".", 1)[0]
    try:
        return int(tail)
    except Exception:
        return 0


def _recover_stale_command_claims(state_dir):
    """Put dead coordinators' uncommitted command claims back on the input channel.

    A claim is an OS-atomic rename of ``*.json`` to ``*.json.claim-PID``.  The kernel cannot
    make a rename half-happen, and the new single-instance runner means a live owner must be
    left alone.  ``*.applied`` is the commit tombstone: it is cleanup only and is NEVER replayed.
    """
    roots = [state_dir, os.path.join(state_dir, COMMANDS_DIR)]
    recovered = 0
    for root in roots:
        try:
            names = list(os.listdir(root))
        except OSError:
            continue
        for name in names:
            if ".claim-" not in name:
                continue
            path = os.path.join(root, name)
            if name.endswith(".applied"):
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            owner = _claim_owner_pid(path)
            if owner and _pid_alive(owner):
                continue
            original = path.split(".claim-", 1)[0]
            if os.path.exists(original):
                # Never overwrite a newer writer. Preserve the dead claim as evidence instead
                # of turning an ambiguity into a duplicate delivery.
                try:
                    os.replace(path, path + ".orphan")
                except OSError:
                    pass
                continue
            try:
                os.replace(path, original)
                recovered += 1
            except OSError:
                pass
    return recovered


def _claim_one_command(path, display_name=None):
    """Atomically take one command file for this process, or None if somebody else won."""
    claimed = path + ".claim-%d" % os.getpid()
    try:
        os.replace(path, claimed)
    except OSError:
        return None
    try:
        with open(claimed, encoding="utf-8-sig") as fh:
            cmd = json.load(fh)
    except Exception:
        try:
            os.replace(claimed, path + ".bad")
        except OSError:
            pass
        return None
    return {"cmd": cmd, "original": path, "claimed": claimed,
            "name": display_name or os.path.basename(path)}


def claim_commands(state_dir) -> list:
    """Claim every pending fleet command, oldest first, WITHOUT deleting it.

    The live runner applies each returned claim and calls :func:`commit_command_claim` only after
    the command's durable effects are in place.  If the process dies first, the next coordinator
    recovers the dead pid's ``.claim-*`` file and retries it.  This closes the former
    read/delete -> apply/ledger crash window.
    """
    _recover_stale_command_claims(state_dir)
    out = []

    # Shipped pre-commands.d cockpit binaries can still write this legacy file. Claim it too so
    # compatibility does not re-introduce the durability hole.
    legacy = os.path.join(state_dir, "commands.json")
    if os.path.isfile(legacy):
        c = _claim_one_command(legacy, "commands.json")
        if c is not None:
            out.append(c)

    d = os.path.join(state_dir, COMMANDS_DIR)
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return out
    for name in names:
        c = _claim_one_command(os.path.join(d, name), name)
        if c is not None:
            out.append(c)
    return out


def restore_command_claim(claim) -> bool:
    """Return an uncommitted claim to the input channel so the next sweep can retry it."""
    try:
        claimed = claim["claimed"]
        original = claim["original"]
    except Exception:
        return False
    if not os.path.isfile(claimed):
        return False
    if os.path.exists(original):
        try:
            os.replace(claimed, claimed + ".orphan")
        except OSError:
            pass
        return False
    try:
        os.replace(claimed, original)
        return True
    except OSError:
        return False


def commit_command_claim(state_dir, claim, applied=None, rejected_errors=None) -> bool:
    """Commit one claim and only then make it disappear.

    The first rename to ``.applied`` is the commit point.  A crash after it can leave a tombstone
    but can never replay the command; stale tombstones are housekeeping in
    ``_recover_stale_command_claims``.  A landing receipt is written AFTER that commit point.
    ``applied=None`` is used by the backwards-compatible :func:`read_commands` helper and keeps
    its old 'read' semantics; the live drain passes True/False explicitly.
    """
    try:
        claimed = claim["claimed"]
        cmd = claim["cmd"]
        name = claim.get("name") or os.path.basename(claim.get("original") or claimed)
    except Exception:
        return False
    committed = claimed + ".applied"
    until = time.time() + 2.0
    while True:
        try:
            os.replace(claimed, committed)
            break
        except OSError:
            if time.time() >= until:
                return False
            time.sleep(0.02)

    ack = (cmd or {}).get("ack") if isinstance(cmd, dict) else None
    if isinstance(ack, str) and ack:
        body = {"read": True, "ts": time.time(), "file": name}
        if applied is not None:
            body["applied"] = bool(applied)
        errs = list(rejected_errors or [])
        if errs:
            body.update({"rejected": True, "errors": errs[:10]})
        if not _write_receipt(state_dir, ack, body):
            print("[command] no landing receipt for %s: its ack names no file in this fleet's %s/, or the write failed"
                  % (name, ACKS_DIR), flush=True)
    try:
        os.remove(committed)
    except OSError:
        pass                         # .applied is deliberately non-replayable
    return True


def claim_specific_command(state_dir, path):
    """Claim exactly one pending commands.d JSON, but only from this state directory."""
    if not path:
        return None
    command_dir = os.path.join(state_dir, COMMANDS_DIR)
    full = os.path.abspath(path)
    if not full.lower().endswith(".json"):
        return None
    if not _same_dir(os.path.dirname(full), command_dir):
        return None
    _recover_stale_command_claims(state_dir)
    return _claim_one_command(full, os.path.basename(full))


def claim_adopt_command(state_dir, path):
    """Claim a pending add_goal command for a fresh runner to adopt as initial work.

    Only ``add_goal`` plus its optional landing ``ack`` may be adopted. A control command
    (stop/steer/settings/close) belongs to the run it addressed and must never become a new run.
    Invalid commands are restored before returning so a diagnosis never destroys the request.
    Returns ``(claim_or_none, goals, errors)``.
    """
    claim = claim_specific_command(state_dir, path)
    if claim is None:
        return None, [], ["adopt command is not a pending file in this fleet's commands.d"]
    cmd = claim["cmd"]
    errors = list(validate_command(cmd, state_dir))
    if isinstance(cmd, dict):
        extra = sorted(k for k in cmd if k not in ("add_goal", "ack"))
        if extra:
            errors.append("adopt command contains live-control key(s): %s" % extra[:8])
        if "add_goal" not in cmd:
            errors.append("adopt command has no add_goal")
    goals = goals_from_command(cmd) if not errors else []
    if not goals and not errors:
        errors.append("adopt command contains no usable goals")
    if errors:
        restore_command_claim(claim)
        return None, [], errors
    return claim, goals, []


def read_commands(state_dir) -> list:
    """Compatibility consumer: return pending commands and commit them as READ.

    Production fleet execution uses ``claim_commands`` directly and commits only AFTER apply.
    Tests and small seam tools historically call ``read_commands`` as the receiver itself; keep
    that API and its landing-receipt semantics without putting the live runner back on the old
    read/delete-before-apply path.
    """
    out = []
    for claim in claim_commands(state_dir):
        cmd = claim["cmd"]
        errs = validate_command(cmd, state_dir)
        commit_command_claim(state_dir, claim, applied=None, rejected_errors=errs)
        out.append(cmd)
    return out

def goals_from_command(cmd) -> list:
    """The `add_goal` entries in a fleet command file, as goals this run can queue.

    MODULE LEVEL SO THE SEAM CAN BE TESTED. This was a closure inside main()'s _drain_commands,
    which made it unreachable from a test -- so the writer of this file (relay/task_router.py's
    add_goal_to_live_fleet) and its reader were each covered by their own tests and the JOIN
    between them by none. That is the shape of two defects already on this project's record:
    the archive wrote `gate_verdict` while the scheduler read `verdict`, and an adapter wrote
    `keep` while the policy read `kept`. Both sides passed their own tests throughout.

    Accepts a single entry or a list, a dict or a bare string. `checks` and `cwd` are carried
    through so a RETRY re-runs WITH its acceptance gate rather than the bare prompt;
    goal_fields reads them downstream. An entry without text contributes nothing rather than
    raising -- one malformed row must not cost the rest of the file.
    """
    add = (cmd or {}).get("add_goal")
    if add is None:
        return []
    items = add if isinstance(add, list) else [add]
    out = []
    for it in items:
        try:
            if isinstance(it, dict) and it.get("text"):
                g = {"text": it["text"], "priority": bool(it.get("priority"))}
                if it.get("checks"):
                    g["checks"] = it["checks"]
                if it.get("cwd"):
                    g["cwd"] = it["cwd"]
                # THE ADMISSION-TIME ID, carried through same as checks/cwd. task_router's
                # add_goal_to_live_fleet puts it on the item when the sender wants a receipt;
                # without threading it here it dead-ends at this function exactly the way the
                # module docstring above already warns a writer/reader mismatch can happen.
                if it.get("jid"):
                    g["jid"] = it["jid"]
                # WHICH CONVERSATION TO CONTINUE, carried through for the same reason as the
                # three above it. _follow_up builds this field and RelayWorker.__init__ reads
                # it, but until now the only path between them ran inside one live run: a
                # steer delivered to a worker that had already finished. Anything arriving
                # through the command channel -- the chat window's fleet rows, task_router's
                # `entry`, an operator writing the file by hand -- had the field dropped here
                # and quietly started a fresh conversation instead.
                if it.get("follow_up_to"):
                    g["follow_up_to"] = it["follow_up_to"]
                # THE CONVERSATION'S ID, AND THE COMMENT ABOVE MISSED IT. That paragraph was
                # written to carry `follow_up_to` through, and `resume_conv` -- the field that
                # makes `follow_up_to` a fallback rather than the mechanism -- was left out of
                # the same list. So the chat window read the durable id off the transcript,
                # put it on the item (ui/CopilotChat.cs, "THE CONVERSATION BY ITS ID"), and
                # this function dropped it; RelayWorker then matched the conversation by GOAL
                # TEXT and printed "That is a guess -- the caller should carry resume_conv"
                # about a caller that was carrying it. Identity by wording is what
                # docs/incidents/20260912_a_fleet_conversation_could_be_read_and_never_answered.md
                # was closed on, and the close did not reach this hop.
                #
                # The goals-file path never had this bug: it appends the whole dict, so the
                # cockpit's Continue button worked while the same follow-up typed into the
                # chat window did not. One feature, two routes, one of them silently guessing.
                if it.get("resume_conv"):
                    g["resume_conv"] = it["resume_conv"]
                # WHICH VERB SENT IT, carried for the same reason as the four above and with
                # the same hazard in mind: a field set at one end and dropped here is what
                # made resume_conv a guess for weeks. The chat window sets this only for a
                # `/goal ` submission, and the fleet records it as a mechanism -- so this is
                # a field with a reader before it had a writer's second line.
                if it.get("new_task"):
                    g["new_task"] = True
                out.append(g)
            elif isinstance(it, str) and it:
                out.append({"text": it, "priority": False})
        except Exception:
            pass
    return out


def _print_table(workers, total=None):
    # THE DENOMINATOR IS THE WORKERS, NOT THE GOALS THE RUN STARTED WITH. It was len(goals),
    # while the numerator counts terminal workers -- and a fan-out parent is terminal the
    # moment it has proposed its split. So a run that split 4 goals into 44 workers printed
    # [fleet 4/4] while 36 subtasks and 4 merges had not begun. Measured 2026-08-28.
    #
    # Counting workers makes the denominator grow as subtasks appear, which is the honest
    # shape: the line says how much of the work that EXISTS is finished, and the work that
    # exists is not known until the split happens.
    total = len(workers) if total is None else total
    done = sum(1 for w in workers if w.status in TERMINAL)
    def _turn_str(w):
        # max_turns=0 means unlimited; show "t10/∞" to avoid "t10/0" confusion.
        cap = ("∞" if not w.max_turns else str(w.max_turns))
        return "%s[%s t%d/%s]" % (w.name, STATUS_PILL.get(w.status, (w.status,))[0],
                                   w.turn, cap)
    line = "  ".join(_turn_str(w) for w in workers)
    sys.stdout.write("\r\033[K[fleet %d/%d] %s" % (done, total, line))
    sys.stdout.flush()


# Set by main() right after argparse so the KeyboardInterrupt handler at the bottom of
# this file (outside main()'s local scope) knows which state_dir's ACTIVE marker to
# clear on an explicit Ctrl+C. None until a run actually starts.
_ACTIVE_STATE_DIR = None
_ACTIVE_RUN_LOCK = None



def report_duplicate_completions(state_dir, out=print, transcripts=None):
    """Say, at the end of a run, where duplicate completions of one goal disagree.

    A STUCK worker is retried and the retry duplicates the goal, so a run can finish holding
    several independent answers to the same question -- and the ledger keeps whichever landed
    last. Measured on the 290-cinema survey of 2026-09-04: one goal was completed four times,
    five of its subjects came back with conflicting verdicts, one of them three ways, and
    finding that out took a person reading four transcripts.

    It picks no winner. Choosing is the judgement that needs a person or a supervising agent;
    this only makes the choice visible.

    A FUNCTION SO IT CAN BE RUN BY A TEST. Inline at the call site it could only ever be
    asserted against its own source, and source assertions do not execute -- which is how the
    detectors this repository already had came to be correct and unreachable at the same time.
    Best-effort: a reconciler that raised would turn a finished run into a crashed one.
    """
    try:
        import os as _os
        from relay.fleet_reconcile import load_completions, reconcile, disagreements
        where = transcripts or _os.path.join(state_dir, "transcripts")
        repeated = {k: v for k, v in load_completions(run=None, transcripts=where).items()
                    if len(v) > 1}
        reported = 0
        for _key, comps in repeated.items():
            conflicts = disagreements(reconcile(comps))
            if not conflicts:
                continue
            reported += 1
            out("")
            out("  ⚠ この走行で同じゴールが %d 回完了し、%d 件で結論が食い違っています:"
                % (len(comps), len(conflicts)))
            for subject, verdicts in conflicts[:8]:
                out("      %s" % subject[:60])
                for verdict, who in verdicts.items():
                    out("          %-12s <- %s" % (verdict[:12], ", ".join(who)))
            if len(conflicts) > 8:
                out("      ... 他 %d 件" % (len(conflicts) - 8))
            out("    どれを採るかは自動では決めません。"
                "python -m relay.fleet_reconcile で全文を確認してください。")
        return reported
    except Exception:
        return 0


def main():
    global _ACTIVE_STATE_DIR, _ACTIVE_RUN_LOCK
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
    ap.add_argument("--adopt-command", help="rescue one pending commands.d add_goal as this run's initial work; used by the local cockpit when a live run ends during submission")
    ap.add_argument("--force", action="store_true",
                    help="start even when the launch gate's preconditions are unmet; the run still happens and its measurements carry whatever was wrong")
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
    ap.add_argument("--resilience-profile", choices=["off", "review", "security", "fix"],
                    default="off", help="review refusal recovery profile (default off)")
    ap.add_argument("--max-fresh-replays", type=int, default=0,
                    help="identical fresh-conversation replays per goal (default 0)")
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
    ap.add_argument("--autoscale-headroom-mb", type=int, default=-1,
                    help="free RAM (MB) to keep for the user's other work. -1 = the declared "
                         "default in tools/settings_keys.py. An ALIAS for the RAM floor: the "
                         "live autoscale reads ram_box[0], so this only seeds it.")
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
    # ON BY DEFAULT, AND THE OLD HELP TEXT CARRIED THE REASON IT WAS NOT. "a goal that fits
    # should not pay for a split turn and a merge turn" was true while this flag WAS the
    # decision. It is not any more: RelayWorker judges every goal separately
    # (`self.fanout = bool(fanout) and _depth0 and _goal_splittable`), so a goal that fits is
    # judged NO_SPLIT and pays nothing. The flag only decides whether the question is ever
    # asked -- and off by default meant it was asked for nobody who did not know to opt in.
    ap.add_argument("--fanout", action=argparse.BooleanOptionalAction, default=True,
                    help="split a goal into independent sub-goals, run them in parallel, and "
                         "merge the answers -- for work whose SIZE is the problem: a goal that "
                         "cannot fit in one conversation fails at the conversation, not at the "
                         "work. ON by default; each goal is still judged separately (offline "
                         "triage, then the agent itself, which may answer NO_SPLIT), so a goal "
                         "that fits costs nothing. --no-fanout disables the capability.")
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

    # FINAL EXCLUSION LAYER: one state dir may have exactly one coordinator. Do this before
    # coordinator logs, queue receipts, retention, resume expansion, or any durable run-state
    # rewrite. External launchers have guards too, but the process that owns the files is the
    # only layer that can make the invariant unconditional.
    os.makedirs(args.state_dir, exist_ok=True)
    _ACTIVE_STATE_DIR = args.state_dir
    _owner = _active_run_conflict_pid(args.state_dir)
    if _owner:
        print("REFUSING TO START: fleet state directory is already owned by live pid %d: %s"
              % (_owner, args.state_dir), flush=True)
        return 3
    _ACTIVE_RUN_LOCK = _acquire_run_lock(args.state_dir)
    if _ACTIVE_RUN_LOCK is None:
        print("REFUSING TO START: another fleet coordinator holds the state-dir lock: %s"
              % args.state_dir, flush=True)
        return 3
    # Close the marker-vs-lock race: another legacy runner may have written a marker after the
    # first check but before this process took the new OS lock.
    _owner = _active_run_conflict_pid(args.state_dir)
    if _owner:
        _release_run_lock(_ACTIVE_RUN_LOCK)
        _ACTIVE_RUN_LOCK = None
        print("REFUSING TO START: fleet state directory became owned by live pid %d: %s"
              % (_owner, args.state_dir), flush=True)
        return 3

    _adopt_claim = None
    _adopt_goals = []
    if args.adopt_command:
        _adopt_claim, _adopt_goals, _adopt_errors = claim_adopt_command(args.state_dir, args.adopt_command)
        if _adopt_claim is None:
            # If the exact pending file vanished after the UI launched this rescuer, another
            # runner already claimed it. That is success-by-race, not an error; anything still
            # present but invalid is a real refusal.
            if not os.path.exists(args.adopt_command):
                print("ADOPT: command is no longer pending; another runner already took it.", flush=True)
                _release_run_lock(_ACTIVE_RUN_LOCK)
                _ACTIVE_RUN_LOCK = None
                return 0
            print("ADOPT: refusing pending command: %s" % "; ".join(_adopt_errors), flush=True)
            _release_run_lock(_ACTIVE_RUN_LOCK)
            _ACTIVE_RUN_LOCK = None
            return 4

    # Capture the coordinator's own stdout/stderr to a durable log under state_dir, from
    # here (right after argparse) so it covers argparse-error exits too, regardless of
    # which launcher started this process. Best-effort -- never crashes on failure.
    _setup_coordinator_log(args.state_dir)

    # VISIBLE BEFORE ANYTHING CAN REFUSE IT. See _record_cli_submission: until this existed, a
    # goal given on the command line appeared nowhere until the run was already going, so a run
    # that died on a precondition left the operator unable to tell it from a command never
    # typed. Cleared at run start, where the goals ledger takes over.
    _cli_queue_paths = _record_cli_submission(args.state_dir, _adopt_goals + _read_goals(args), sys.argv)

    # RETENTION RUNS ONCE, HERE, AND NOT ON A TIMER -- the same reasoning as the session
    # store's pass: a sweep that can fire mid-run is a sweep that can delete the transcript
    # being written to. Placed AFTER the coordinator log is opened so this run's own log is
    # already among the newest and can never be a candidate.
    #
    # Nothing here bounded .fleet before, and the shape of what it holds is the point: of
    # 255 MB outside the session database, 185 MB was written in the last SEVEN DAYS. Age
    # rules reach the tail of that, not the bulk -- roughly 47 MB. The rest is write rate,
    # about 26 MB a day, which retention cannot fix and this comment should not pretend it
    # does.
    try:
        from relay import fleet_retention
        _ret = fleet_retention.apply(fleet_dir=args.state_dir)
        if _ret.get("freed_bytes"):
            print("fleet retention: freed %.1f MB" % _ret["freed_mb"], flush=True)
    except Exception as _exc:                     # never let housekeeping stop a run
        print("fleet retention skipped: %s" % _exc, flush=True)

    # KeyboardInterrupt already knows this state_dir from the early single-instance gate above.

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
    if args.panel:
        # EXPLICIT BEATS DERIVED, AND USED NOT TO.
        #
        # This read `if args.panel and args._lenses is None`, so --panel was silently ignored
        # whenever the effort level had already chosen a lens -- which `auto`, the default,
        # always does. Asking for the panel on a default run produced a single reviewer and
        # said nothing about it. That is part of why the multi-lens panel shows up in 4.3% of
        # ledger records: not "nobody wanted it", but "asking did not work".
        if args._lenses and list(args._lenses) != list(PANEL_LENSES):
            print("[effort] --panel overrides the effort's lens %s" % (args._lenses,))
        args._lenses = list(PANEL_LENSES)
        args.refuter = True
    print("[effort] %s  (refuter=%s lenses=%s refute<=%d research<=%d)"
          % (_eff, args.refuter, args._lenses, args.max_refute, args.max_research))

    goals = _adopt_goals + _read_goals(args)

    # THE LENS IS CHOSEN BEFORE THE GOALS ARE READ, AND THE GOALS ARE THE EVIDENCE.
    #
    # `auto` picks rootcause_code only when SWE_MINIMALITY or MCP_TASK_DOMAIN says the task is
    # coding. The CLI SWE path sets that; the UI-driven path never has -- so every UI SWE run
    # was reviewed with the domain-agnostic lens. Measured: 155 of 155 ledger records carry
    # `rootcause`, none carry `rootcause_code`.
    #
    # That is not a small difference. fleet_runner's own comment says a non-coding task must
    # never be reviewed with code criteria; the inverse costs the code criteria that exist for
    # this exact workload -- reproduce-the-bug, producer/consumer consistency, hunk discipline.
    # The refuter ran, and asked the wrong questions.
    #
    # Corrected from the goals themselves, which is the only evidence available this late, and
    # said out loud because a silent swap is how the original mistake stayed invisible.
    try:
        if args._lenses == ["rootcause"] and goals:
            _texts = [(g.get("text") if isinstance(g, dict) else str(g)) or "" for g in goals]
            _coding_goals = sum(1 for t in _texts
                                if "fixing a real bug in the open-source project" in t
                                or "Fix ONLY the source" in t)
            if _coding_goals and _coding_goals == len(_texts):
                args._lenses = ["rootcause_code"]
                os.environ.setdefault("SWE_STRONG_SELFTEST", "1")
                print("[effort] every goal is a coding task; lens corrected "
                      "rootcause -> rootcause_code (%d goals)" % _coding_goals)
            elif _coding_goals:
                print("[effort] %d of %d goals look like coding tasks; lens left as %s "
                      "because a mixed run must not be reviewed with code criteria"
                      % (_coding_goals, len(_texts), args._lenses))
    except Exception as _e:
        print("[effort] lens correction skipped: %s" % type(_e).__name__)
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
    # NAMED, NOT A TRACEBACK. This is the first thing that reads a goal's acceptance spec,
    # and it runs long before Playwright -- so a malformed check fails here with no browser
    # open and nothing to reset, which is right. The person who has to fix it is looking at
    # their own goals file, so say which goal and what is wrong with it rather than unwinding
    # the stack at them.
    try:
        gtexts = [goal_fields(g)[0] for g in goals]
    except MalformedCheck as _bad:
        for _i, _g in enumerate(goals, 1):
            try:
                goal_fields(_g)
            except MalformedCheck:
                _t = (_g.get("text") or _g.get("goal") or "") if isinstance(_g, dict) else str(_g)
                print("goal %d of %d has an unusable acceptance check:\n  %s\n  goal: %s"
                      % (_i, len(goals), _bad, _t[:200]))
                break
        sys.exit(2)
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

    # RUN-RESUME: the ledger is the durable owner of every initial goal. For an adopted live
    # command this write is REQUIRED, not best-effort: the command may not be committed away
    # until another durable source can reconstruct it.
    try:
        _write_goals_ledger(args.state_dir, goals, started, raise_on_error=bool(_adopt_claim))
    except Exception as e:
        if _adopt_claim is not None:
            restore_command_claim(_adopt_claim)
        print("[resume] could not durably adopt pending command: %s" % e, flush=True)
        _release_run_lock(_ACTIVE_RUN_LOCK)
        _ACTIVE_RUN_LOCK = None
        return 5
    try:
        _write_atomic(os.path.join(args.state_dir, LAST_RUN_DONE), {})
    except Exception as e:
        sys.stderr.write("[resume] WARN: could not reset done map: %s\n" % e)

    # Write interruption recovery BEFORE committing an adopted command. From the instant the
    # command disappears, a crash must still leave both its goals ledger and an active marker
    # that tells the supervisor to resume that ledger.
    _write_active_marker(args.state_dir, start_ts=started)
    if _adopt_claim is not None:
        if not commit_command_claim(args.state_dir, _adopt_claim, applied=True):
            restore_command_claim(_adopt_claim)
            _clear_active_marker(args.state_dir, owner_pid=os.getpid())
            print("ADOPT: durable goal ledger exists but command commit failed; returned command to queue.", flush=True)
            _release_run_lock(_ACTIVE_RUN_LOCK)
            _ACTIVE_RUN_LOCK = None
            return 5

    # The run is now represented by ledger + active marker; optimistic startup queue rows can go.
    _clear_cli_submission(_cli_queue_paths)

    # an EXPLICIT --max-concurrent (>=0) was given on the CLI (not the -1 "ask the cockpit"
    # sentinel). Used for the precedence rule below: CLI wins over settings.txt autoscale.
    explicit_mc = args.max_concurrent >= 0
    if args.max_concurrent > 0:
        max_conc = args.max_concurrent
    elif args.max_concurrent == 0:
        # The run is a long-lived queue: add_goal can add work after launch.  Asking RAM how
        # many of the *initial* goals fit permanently shrinks a one-goal run to one lane.
        max_conc = auto_concurrency(AUTOSCALE_CEILING_DEFAULT)  # 0 = auto from free RAM
    else:
        # Do not cap the live capacity by len(goals) at t=0.  The pending queue itself prevents
        # over-admission when only one goal exists; keeping the configured capacity lets later
        # add_goal submissions use the idle lanes immediately.
        max_conc = settings_maxtabs()                    # -1 = cockpit setting (default 3)

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
        # THE COCKPIT SHOWS 100 AND THE FLEET USED 3. `autoscale_max` is written only when the
        # operator touches the stepper, so on a machine where they never did, the key is absent
        # -- and settings.txt on this box has neither autoscale_max nor maxtabs. The cockpit
        # then displays its own in-memory default of 100, labelled "high by design", while this
        # line quietly substituted maxtabs (itself defaulted to 3). The screen said one number
        # and the fleet ran another, which is how "RAM is free and it still runs three at a
        # time" looks from outside.
        #
        # With autoscale on, a high ceiling is the point: it hands the decision to
        # ram_target_cap instead of to a number nobody chose. So the fallback is the ceiling the
        # cockpit shows. With autoscale OFF the ceiling is not consulted, and maxtabs remains
        # the fixed cap exactly as before.
        asc_ceiling = AUTOSCALE_CEILING_DEFAULT if autoscale else max(asc_default,
                                                                      settings_maxtabs())
    # Same long-lived-queue rule as max_conc above.  The ceiling is machine/operator capacity,
    # not the number of goals present at startup.  Keep the ordinary tab safety bound instead.
    asc_ceiling = max(TABS_BOUNDS[0], min(int(asc_ceiling), TABS_BOUNDS[1]))
    asc_default = max(1, min(asc_default, asc_ceiling))      # default never exceeds the ceiling
    autoscale_max = asc_ceiling
    if autoscale:
        max_conc = asc_default                               # START at the user's default
    asc_box = [1 if autoscale else 0, asc_ceiling]           # live [on, ceiling] for the cockpit

    # ── disk-floor admission reserve: keep this many GB free on C: at all times. Resolution
    # chain (most explicit wins): CLI --disk-floor-gb >= 0 -> cockpit settings.txt
    # disk_floor_gb -> env SWE_DISK_FLOOR_GB (default 6). A 0 floor disables the disk gate.
    # PROVENANCE, NOT JUST THE VALUE. Printing the winning number and not the branch that
    # produced it is what made this chain un-debuggable from outside: the panel said 1 GB,
    # runs reserved 4, and every investigation had to re-derive the chain by hand and still
    # could not say which step was lying. The raw file read is captured separately from the
    # resolved value so the log can distinguish "the file said 1 and something overrode it"
    # from "the file was not read at all".
    # NOT "CLI". This branch means the parsed namespace holds a non-negative value, which an
    # argparse default or a pre-populated namespace can produce with nothing on the command
    # line -- so the label says what was actually observed and leaves the cause open.
    if args.disk_floor_gb >= 0:
        disk_floor = args.disk_floor_gb
        disk_floor_src = ("parsed namespace held %.1f (a typed flag, an argparse default, or "
                          "a later assignment -- argv is printed below so they can be told "
                          "apart)" % args.disk_floor_gb)
    else:
        disk_floor = settings_disk_floor()
        disk_floor_src = SETTINGS_READ_TRACE.get("disk_floor_gb", "(read not traced)")
    disk_box = [disk_floor]                                   # live disk floor (cockpit-settable)
    # ── RAM-floor admission reserve: keep this many MB free for the user. CLI --ram-floor-mb >= 0
    # -> cockpit settings.txt ram_floor_mb -> --autoscale-headroom-mb when given -> the ONE
    # declared default. That last step used to be --autoscale-headroom-mb's own default of
    # 1400, which is how this knob came to have three defaults that never had to agree.
    if args.ram_floor_mb >= 0:
        ram_floor = args.ram_floor_mb
        ram_floor_src = "parsed namespace held %.0f (flag, default, or assignment)" % args.ram_floor_mb
    else:
        ram_floor = settings_ram_floor(
            default=(float(args.autoscale_headroom_mb) if args.autoscale_headroom_mb >= 0
                     else RAM_FLOOR_DEFAULT_MB))
        ram_floor_src = SETTINGS_READ_TRACE.get("ram_floor_mb", "(read not traced)")
    ram_box = [ram_floor]                                     # live RAM floor (cockpit-settable)
    eval_disk = None if args.eval_disk_gb < 0 else args.eval_disk_gb

    # ── THE FILE IS THE SETTING, NOT THE MOMENT WE STARTED. Everything above reads
    # settings.txt exactly once. A run that outlives the operator's next visit to the
    # settings panel therefore uses numbers they can no longer see or correct, and the
    # only channel that could have told it -- a live push from a cockpit that happens to
    # be running -- is missing whenever the cockpit was restarted, rebuilt, or simply not
    # open. Measured 2026-09-15: a run started 15:08:26, settings saved 15:15, and the
    # run reserved 4 GB / 1024 MB for its whole life while the panel said 1 GB / 512 MB.
    #
    # The follower adopts a key only when the FILE's value CHANGES, so the cockpit's
    # live overrides (強制開始 zeroing the disk gate) survive until the operator next
    # moves that knob, and a run nobody touches behaves exactly as it did before.
    #
    # IT FOLLOWS EVEN WHEN A CLI FLAG PINNED THE VALUE, and that is deliberate. The
    # documented chain is "most explicit wins", but between a flag typed when the run
    # was launched and a knob the operator is moving right now, the one in front of
    # them is the more explicit statement -- it is the one they are watching for an
    # effect. The live cockpit push has always overridden a CLI flag for exactly this
    # reason; a run that ignored the panel because of a flag from an hour ago would be
    # the same defect this block exists to remove, wearing a different hat.
    #
    # on_tick fires every poll_s (1.0 s), so "the operator changes a setting and the
    # run changes" is a second, not a restart.

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
    # PRINTED WHETHER OR NOT THERE IS A FLOOR. The old guard meant a 0 floor -- the disk gate
    # disabled entirely -- said nothing at all, so the most dangerous configuration was the
    # quietest one.
    from relay.relay_fleet import free_disk_gb
    # THE WHOLE PROVENANCE, ON THE LINE THAT REPORTS THE NUMBER. A run whose floor disagrees
    # with the panel has been reported three times and "fixed" once; every investigation had
    # to re-derive the chain from outside because the log printed only the winner.
    print("       disk floor: keep >= %.1f GB free on C: -- %s (free now %.1f GB)"
          % (disk_floor, disk_floor_src, free_disk_gb()))
    print("       RAM floor:  keep >= %.0f MB free -- %s" % (ram_floor, ram_floor_src))
    # EVERY KEY THIS PROCESS ACTUALLY SEES. If the fleet is reading a different copy of the
    # settings file than the cockpit writes, then no setting reaches it -- not just the floor
    # -- and the only way to know is to print what it read, not what we think it read.
    try:
        _sp = _settings_path()
        _raw = io.open(_sp, encoding="utf-8-sig").read()
        print("       settings   : %s (%d bytes) ->" % (_sp, len(_raw.encode("utf-8"))))
        for _ln in _raw.splitlines():
            if _ln.strip():
                print("                    %s" % _ln)
    except Exception as _e:
        print("       settings   : could not be read: %s" % _e)
    print("       argv       : %r" % (getattr(sys, "orig_argv", None) or sys.argv,))
    print("       resolver   : %s" % (getattr(_settings_float, "__module__", "?"),))

    mc_box = [max_conc]                # live concurrency cap (cockpit can change it)
    # BUILT HERE, AFTER EVERY BOX EXISTS. This used to be an inline block 56 lines up,
    # where four closures referenced these lists lazily and the ordering never mattered.
    # Extracting it turned those closures into arguments, and arguments are resolved at
    # the call: every fleet run died at startup with UnboundLocalError on mc_box, while
    # the queue recorded each one as started. Order is checkable; deferral was not.
    settings_follower = build_settings_follower(disk_box, ram_box, mc_box, asc_box)
    add_box = []                       # goals queued mid-run (native chat / cockpit)
    pause_box = [False]                # cockpit pause toggle: freeze the fleet without losing
                                       # state (e.g. across a network switch); resume to continue
    stop_box = [False]                 # cockpit graceful-stop: cancel all workers and end the run
    reunlock_box = [None]               # last {"reunlock":...} outcome -- see apply_reunlock;
                                       # surfaced in status.json so the operator can tell whether
                                       # the button worked rather than watching silence
    rejections_box = []                # commands refused by validate_command -- surfaced in
                                       # status.json as command_rejections (SEC-08)

    def _drain_commands(workers):
        # CLAIM -> APPLY -> COMMIT. A command remains a real file until its durable effects are
        # in place; a crash before commit leaves a .claim-PID the next coordinator recovers.
        for claim in claim_commands(args.state_dir):
            ok, errs = _apply_command(claim["cmd"], workers)
            if ok is True:
                if not commit_command_claim(args.state_dir, claim, applied=True):
                    print("[command] applied but could not commit claim %s" % claim.get("name", "?"), flush=True)
            elif ok is False:        # schema refusal is a terminal, audited consumption
                commit_command_claim(args.state_dir, claim, applied=False, rejected_errors=errs)
            else:                    # application failed before a durable commit; retry later
                restore_command_claim(claim)

    def _apply_command(cmd, workers):
        # WHOLE OR NOT AT ALL (SEC-08). Checked before anything below touches a box. Return an
        # explicit outcome so _drain_commands knows whether it may commit the claimed file.
        _errs = validate_command(cmd, args.state_dir)
        if _errs:
            record_command_rejection(rejections_box, cmd, _errs)
            return False, _errs
        try:
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
            if cmd.get("steer") is not None:
                deliver_steers(cmd["steer"], workers, enqueue=add_box.append)
            # THE FALLBACK BUTTON: {"reunlock": "w0"} (or "" / "*" for every live worker)
            # re-delivers the unlock turn ON DEMAND, for when the automatic recovery in
            # relay_fleet (UNLOCK_PREFIX % password injected into a worker's first turn,
            # plus the lock-refusal heuristic that retries it) never fired or missed a
            # later refusal. See apply_reunlock's docstring for the incident. The result
            # is kept for status.json rather than only printed, because a command that
            # silently did nothing is the exact failure this exists to remove.
            if "reunlock" in cmd:
                reunlock_box[0] = apply_reunlock(cmd.get("reunlock"), workers,
                                                 enqueue=add_box.append)
            # native chat / cockpit queued new goals into the running fleet. Persist the WHOLE
            # command's goal batch atomically BEFORE exposing any item to memory. If that write
            # fails, _apply_command fails and the claimed command is restored for a later sweep.
            _cmd_goals = goals_from_command(cmd)
            if _cmd_goals:
                _append_goals_ledger(args.state_dir, _cmd_goals, started, raise_on_error=True)
            for g in _cmd_goals:
                add_box.append(g)
                # THE ONE PLACE A `/goal ` SUBMISSION IS STILL VISIBLE. The command file is
                # deleted the moment it is read, and after that this goal looks like any
                # other -- which is why "has anyone ever used /goal" was unanswerable rather
                # than merely unanswered. Recorded where the item arrives, not where the
                # worker starts, because the verb is a property of the submission.
                if g.get("new_task"):
                    try:
                        from relay import mechanism_telemetry as _mt
                        _mt.record("new_task_escape", configured=True,
                                   config_source="chat window `/goal ` prefix",
                                   eligible=True, triggered=True, executed=True,
                                   extra={"has_resume_conv": bool(g.get("resume_conv"))})
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
            return True, []
        except Exception as exc:
            print("[command] applying a validated command failed part-way: %s"
                  % type(exc).__name__, flush=True)
            return None, []

    convs_path = os.path.join(args.state_dir, "conversations.json")

    def _register_convs(workers):
        """Keep the shared conversation registry pointing at THIS run's transcripts.

        The merge itself is merge_conv_rows() at module level -- see the two defects recorded
        there. This closure's only job is to turn live workers into registry rows: read the
        file, build one entry per worker, merge, and write only when something moved.
        Exception-swallowing on purpose: a registry hiccup must never stall the fleet."""
        try:
            existing = []
            if os.path.isfile(convs_path):
                try:
                    existing = json.load(open(convs_path, encoding="utf-8-sig"))  # tolerate C# BOM
                except Exception:
                    existing = []
            entries = []
            for w in workers:
                u = getattr(w, "conv_url", "") or ""
                tr = getattr(w, "transcript", "") or ""
                if not u and not tr:
                    continue
                # THE GOAL, NOT COPILOT'S TITLE. This line preferred `conv_title` and fell
                # back to the goal, and Copilot names a conversation from the opening of the
                # first message it receives -- which is PROTOCOL, ~1,400 characters shared by
                # every task. Measured across 424 stored conversations: 174 named after a
                # prompt preamble, 48 "Microsoft Copilot", 39 after the output-discipline
                # block. 213 of 424 identical to rows they have nothing to do with, and the
                # goal that would have identified each one was sitting right here.
                #
                # Copilot's own title is kept beside it rather than discarded: it is the
                # source record, and a derived value should never overwrite one.
                _copilot = (getattr(w, "conv_title", "") or "")[:120]
                try:
                    from relay import conv_title as _ct
                    title = _ct.make_title(w.goal or "", existing=_copilot, key=(u or tr),
                                           when=time.time())
                    _tsrc = _ct.SOURCE
                except Exception:
                    title = (w.goal or _copilot or "")[:60]
                    _tsrc = "fallback"
                entries.append({"url": u, "title": title, "source": "fleet",
                                "title_source": _tsrc, "copilot_title": _copilot,
                                # carry the disk transcript path + worker name so the chat
                                # opens this conversation straight from the .jsonl -- no live
                                # re-scrape (which fails for any conv whose agent the bridge
                                # is not currently connected to).
                                "transcript": tr,
                                # THE FULL GOAL TEXT, UNTRUNCATED -- NOT `title`, which
                                # make_title() cuts down for display. Before this field existed
                                # the registry was the only continuously-updated feed the chat
                                # window has while it is already open (DiscoverTranscripts only
                                # scans once, at startup) and it carried no goal at all, so any
                                # conversation reached through it -- including every interrupt
                                # sent while the worker was still running -- had nothing for
                                # DecideFleetSend to identify it by and refused with
                                # fleet_no_goal even though the worker was live right there.
                                # See docs/incidents/20260924_fleet_interrupt_no_goal.md.
                                "goal": w.goal or "",
                                "name": getattr(w, "name", ""), "ts": time.time()})
            # SEVEN ROWS, ONE TITLE. `make_title` is called once per row above, in isolation,
            # so rows whose goals share an opening come out identical -- and every child of a
            # fan-out carries the parent's goal by design. Measured on a real seven-way split:
            # 1 distinct title of 7. That is the symptom conv_title.py exists to remove (213 of
            # 424 stored rows named after identical text), reproduced by a mechanism that
            # became the default today.
            #
            # `repeated` / `salvageable` / `disambiguate` were written for exactly this and had
            # no caller. WITHIN THIS REGISTRATION ONLY: rewriting a stored title would move a
            # row under somebody who is looking at it, and what was measured is rows appearing
            # together.
            try:
                from relay import conv_title as _ct2
                _dupes = _ct2.repeated([e.get("title", "") for e in entries], min_count=2)
                if _dupes:
                    _counts = {}
                    for _e in entries:
                        _t = (_e.get("title") or "").strip()
                        if _t in _dupes:
                            _counts[_t] = _counts.get(_t, 0) + 1
                    for _e in entries:
                        _t = (_e.get("title") or "").strip()
                        if _t not in _dupes:
                            continue
                        _k = _e.get("url") or _e.get("transcript") or _e.get("name") or ""
                        _e["title"] = (_ct2.disambiguate(_t, key=_k, when=_e.get("ts"))
                                       if _ct2.salvageable(_t, _counts[_t])
                                       else _ct2.neutral_title(key=_k, when=_e.get("ts")))
                        _e["title_source"] = _ct2.SOURCE + "+dedupe"
            except Exception:
                pass          # a cosmetic title is never worth failing a registration over

            merged, changed = merge_conv_rows(existing, entries)
            if changed:
                _write_atomic(convs_path, merged)
        except Exception:
            pass


    _steer_reported = set()

    def on_tick(workers):
        # BEFORE the commands, so that a live cockpit push in this same sweep is the
        # later word and wins. The operator moving a knob sends both -- the file is
        # saved and the command is pushed -- and they must not race to a different
        # answer depending on which the coordinator happened to read first.
        for _key, _val in settings_follower.poll():
            print("[settings] %s -> %s (adopted live from the settings panel)"
                  % (_key, _val), flush=True)
        _drain_commands(workers)
        # THE BUTTON'S JOB, DONE BY THE HARNESS. A refusal nobody classified used to need a
        # person to notice and press "re-unlock"; it is asked for here every sweep instead.
        # See sweep_unclaimed_refusals for the two incidents that bought this.
        for _receipt in sweep_unclaimed_refusals(workers):
            if isinstance(_receipt, dict):
                reunlock_box[0] = _receipt
        report_unused_steers(workers, _steer_reported)
        _register_convs(workers)
        # RUN-RESUME: refresh the completion map so a crash after this sweep can resume
        # only the still-unfinished goals. Cheap (in-memory scan + one atomic write).
        _update_done_map(args.state_dir, workers)
        try:
            _write_atomic(status_path, _snapshot(workers, started, len(goals), mc_box[0],
                                                 disk_floor_gb=disk_box[0], paused=pause_box[0],
                                                 ram_floor_mb=ram_box[0], directive=directive,
                                                 run_label=run_label, goal_count=goal_count,
                                                 # goals accepted but not yet workers --
                                                 # a split's children live here until the
                                                 # next sweep admits them.
                                                 queued=len(add_box or []),
                                                 reunlock=reunlock_box[0],
                                                 command_rejections=rejections_box))
        except Exception as _status_exc:
            # SILENT HERE USED TO MEAN INVISIBLE, AND task_router.fleet_is_live() TRUSTED THE
            # FILE'S MTIME TO MEAN THE PROCESS. `_write_atomic`'s own docstring says a write
            # that truly cannot land is "a real failure ... never a silent skip" and re-raises
            # after its retry deadline -- but this bare `except: pass` caught that re-raise and
            # threw it away, so a run wedged on a losing PermissionError race or a starved disk
            # kept sweeping with live workers while status.json's mtime simply stopped moving.
            # Measured 2026-09-25: exactly that let a live 41-worker run go undetected past
            # FLEET_LIVE_MAX_AGE_S and a second fleet_runner started on top of it. Printing (a)
            # gives the run's own log a trace of what happened instead of a wordless gap, and
            # (b) does not change the non-fatal behaviour -- a status write must never be able
            # to take the run down, so we still swallow and continue.
            print("[status] WARN: could not write status.json this sweep: %s" % _status_exc,
                  flush=True)
        _print_table(workers)

    from playwright.sync_api import sync_playwright
    from relay.relay_fleet import FleetContextLost, reset_socket_route
    from relay.edge_recover import cdp_alive, companion_edge_mb, hard_reset as _edge_hard_reset
    from relay.edge_recover import other_fleet_runs, should_recycle

    #: How many times a discretionary reset defers to a sibling before taking it anyway.
    EDGE_SUPPRESS_MAX = int(os.environ.get("MCP_FLEET_EDGE_SUPPRESS_MAX", "3"))
    _suppressed = [0]

    def hard_reset(port, discretionary=False, escalate=False):
        """Reset the Edge AND forget the socket route that was bound to it.

        `discretionary` marks the resets taken against a browser that is still WORKING -- the
        stall watchdog and the pre-run memory recycle. Those are the ones that hurt a sibling:
        this Edge profile is shared and cannot be split, so resetting it pulls the context out
        from under every other run pointed at the same port. Six recoveries across three runs
        on 2026-08-25 came from exactly that. A reset of a browser that is already unreachable
        is not discretionary and is never suppressed -- there is nothing left to protect.

        Every caller below wants both, and none of them said so. The route captures its token
        through a page in this browser, so a reset leaves it holding credentials for a context
        that no longer exists; it then fails the next capture three times and closes itself for
        the rest of the run. Measured 2026-08-25 20:07 -- see reset_socket_route.
        """
        if discretionary:
            others = other_fleet_runs(port)
            if not others:
                _suppressed[0] = 0       # the reason to hold back is gone; so is the tally
            if others:
                # ESCALATION IS FOR A WEDGE, NOT FOR MEMORY. Its whole justification is "the
                # sibling is plainly not making progress either", which is true of a stall
                # watchdog and false of a pre-run memory recycle -- a sibling can be working
                # perfectly well while this run merely wants a leaner browser. One counter
                # served both, so three quiet memory refusals could end in yanking a shared
                # Edge out from under a productive sibling: the exact harm this exists to
                # prevent, produced by the mechanism meant to prevent it.
                if not escalate:
                    print("[recycle] %d other fleet run(s) are on this Edge (%s) -- not "
                          "resetting it for memory; a working sibling is not an emergency"
                          % (len(others), ",".join(str(x) for x in others)), flush=True)
                    return False
                _suppressed[0] += 1
                if _suppressed[0] < EDGE_SUPPRESS_MAX:
                    print("[recycle] %d other fleet run(s) are on this Edge (%s) -- not "
                          "resetting it (%d/%d); the memory stays until they finish"
                          % (len(others), ",".join(str(x) for x in others),
                             _suppressed[0], EDGE_SUPPRESS_MAX), flush=True)
                    return False
                # ESCALATION, BECAUSE POLITENESS DEADLOCKS. Two runs on one wedged Edge both
                # decide it is wedged, both see each other, and both stand aside -- forever,
                # symmetrically. cdp_alive stays true for a browser that is wedged but still
                # listening, so nothing else fires either. After this many refusals the
                # sibling is plainly not making progress, and a browser both runs are stuck
                # against is not being protected by leaving it alone. Two resets racing is
                # survivable -- each run recovers through its context-lost path; a deadlock
                # is not.
                print("[recycle] suppressed %d times with %d sibling(s) still here -- "
                      "resetting anyway; a shared wedge is not worth protecting"
                      % (_suppressed[0], len(others)), flush=True)
        _suppressed[0] = 0
        _edge_hard_reset(port)
        try:
            reset_socket_route()
        except Exception:
            pass
        return True

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
    # ARMED EXACTLY WHILE THE SWEEP IS RUNNING. The watchdog used to decide whether a run was
    # live by reading `running` out of the status file THIS PROCESS had just written -- a
    # progress figure, not a liveness one. A fan-out parent going terminal at turn 1 made it
    # false for the hour its children took, and the wedge detector switched itself off at the
    # moment the run began its real work; a capture then blocked the main loop for ten minutes
    # and nothing noticed. `running` is fixed, but the shape of the mistake remains: a
    # narrower window still exists between the last child finishing and the merge being
    # queued.
    #
    # The sweep loop's own execution is the fact being asked about, and it is available
    # in-process. Not `stop_wd` alone: that is cleared only after the final cleanup, and a
    # cleanup slower than stall_s would be hard-reset by the very watchdog that is supposed
    # to be finished with it.
    sweep_active = threading.Event()

    def _watchdog():
        last_seen, last_change = None, time.time()
        while not stop_wd.is_set():
            stop_wd.wait(5)
            if stop_wd.is_set() or args.stall_s <= 0:
                continue
            if not sweep_active.is_set():
                last_change = time.time(); continue
            try:
                d = json.load(open(status_path, encoding="utf-8"))
                if d.get("idle"):
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
                    hard_reset(port, discretionary=True, escalate=True)
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
                hard_reset(port, discretionary=True)
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
                # START CLEAN, NOT JUST FINISH CLEAN. run_relay_fleet closes any Copilot page
                # left over when it ENDS, which stops a run leaving one behind -- and does
                # nothing about one that is already there when it begins.
                #
                # That gap cost a measurement. A page left by an earlier run sat open for nine
                # and a half hours; runs were launched on top of it; and every memory figure
                # taken in that window carried it. Stratified afterwards: 341 MB median with
                # no Copilot page open, 697 MB with one. 61.6% of nearly five thousand samples
                # had one. A monitor caught it and was not read before launching -- so the
                # check belongs where a launch happens, not in whoever remembers to look.
                _closed = _close_idle_copilot_pages(context)
                if _closed:
                    print("       start: closed %d Copilot page(s) left by an earlier run "
                          "(they cost ~350 MB each and would have sat in this run's "
                          "measurements)" % _closed, flush=True)

                # THE GATE. Asked AFTER the cleanup above, so it judges what could not be
                # fixed rather than what merely needed tidying, and BEFORE any worker starts,
                # so a contaminated run never begins.
                #
                # It refuses rather than warns. A warning is what already existed -- the
                # monitor wrote a breach at 22:40 and runs were launched on top of it for the
                # next nine and a half hours, which is not a detection failure but an
                # obligation failure.
                #
                # --force exists because a gate with no way past it is a gate somebody
                # eventually deletes.
                _blockers = _launch_blockers()
                if _blockers and not getattr(args, "force", False):
                    print("\n[gate] REFUSING TO START -- the stack is not in a state where "
                          "this run's results would mean anything:", flush=True)
                    for _name, _detail in _blockers:
                        print("       %-24s %s" % (_name, _detail), flush=True)
                    print("       Fix it, or pass --force to run anyway (and know that the "
                          "measurements carry it).", flush=True)
                    return 2
                if _blockers:
                    print("[gate] --force: starting despite %d unmet precondition(s); "
                          "this run's measurements carry them." % len(_blockers), flush=True)
                sweep_active.set()
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
                                      # the RESOLVED floor, never the raw -1 sentinel: the
                                      # autoscale reads ram_box[0] anyway, so passing anything
                                      # else here would be a second number for one fact.
                                      autoscale_headroom_mb=ram_floor,
                                      autoscale_up_margin_mb=args.autoscale_up_margin_mb,
                                      disk_floor_gb=disk_floor, eval_disk_gb=eval_disk,
                                      disk_box=disk_box, ram_box=ram_box,
                                       pause_box=pause_box, stop_box=stop_box,
                                       transcript_dir=transcripts_dir,
                                       run_id="r%x_a%d" % (int(started), attempt),
                                       resilience_profile=args.resilience_profile,
                                       max_fresh_replays=max(0, args.max_fresh_replays),
                                       fanout=args.fanout)
                # DISARM AS SOON AS THE SWEEP RETURNS. What follows -- result mapping, memory
                # recording, notifications, tab teardown -- can outlast stall_s, and a
                # watchdog still armed would hard-reset the browser out from under the very
                # cleanup that is finishing the run.
                sweep_active.clear()
            # KEYED ON GOAL TEXT, SO TWO WORKERS CAN CLAIM THE SAME SLOT -- and the last one
            # written wins. That is fine when they are the same work retried, and wrong in
            # exactly one case: a fan-out parent carries the MERGED answer of everything its
            # children did (relay_fleet back-fills it), while the other claimant is a single
            # conversation that re-ran the whole goal alone.
            #
            # Measured 2026-08-28 over 100 coordinator logs: 22 of 24 runs that split
            # delivered the duplicate rather than the merge. The duplicate's source was the
            # cockpit re-queueing FANOUT, which is fixed on that side now -- but the
            # collision is not unique to it. A resumed leg or a goal submitted twice
            # produces the same two claimants, and the rule here should not depend on
            # nobody ever creating one.
            #
            # So the merge wins its own slot. Not by worker order, which is what let a
            # higher index take it, but by which result is the aggregate of the others.
            for r in res:
                _goal = r["goal"]
                _prior = results_by_goal.get(_goal)
                if _prior is not None and (_prior.get("outcome") or "") == "FANOUT" \
                        and (r.get("outcome") or "") != "FANOUT":
                    print("[fanout] keeping the merged answer for a goal a second worker also claimed (%s vs %s)" % (_prior.get("name"), r.get("name")), flush=True)
                    continue
                results_by_goal[_goal] = r
            pending = []                                   # finished cleanly
        except FleetContextLost as e:
            sweep_active.clear()
            attempt += 1
            pending = e.unfinished
            print("\n[recover] Edge context lost; resuming %d goal(s) (attempt %d/%d)"
                  % (len(pending), attempt, args.max_recover))
            if args.no_auto_recover or attempt > args.max_recover:
                print("[recover] giving up (auto-recover off or attempts exhausted)")
                break
            if not cdp_alive(args.cdp_url):
                hard_reset(port)
        except MalformedCheck as e:
            # A CONFIGURATION ERROR IS NOT A CONNECTION ERROR, whatever route it arrives by.
            # The generic handler below answers every exception with a browser hard reset and
            # `max_recover` retries; for a deterministic bad check that is a wrong diagnosis
            # printed to the operator, a wasted recovery budget, and possibly an Edge reset
            # that costs every worker currently running. Stop, say what is wrong, change
            # nothing.
            print("\n[config] unusable acceptance check -- not a connection problem, so no "
                  "reset and no retry:\n  %s" % e)
            raise SystemExit(2)
        except Exception as e:
            attempt += 1
            print("\n[recover] %s while connecting; hard reset + retry (attempt %d/%d)"
                  % (type(e).__name__, attempt, args.max_recover))
            if args.no_auto_recover or attempt > args.max_recover:
                break
            # ONLY IF THE BROWSER IS ACTUALLY GONE. A context can be lost because a SIBLING
            # run reset this shared Edge, and resetting it again from here is how one reset
            # becomes a round of them.
            if not cdp_alive(args.cdp_url):
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

    def _final_worker_entry(r, max_turns):
        # FIX 2 (P0): recover the cleaned final assistant text and write it to BOTH
        # display_result (new contract field) and last (stop blanking it).
        # If no text is available, last keeps "" rather than being forcibly overwritten.
        raw_last = r.get("last_response", "") or ""
        cleaned = _clean_final_text(raw_last)
        return {
            "name": r["name"], "goal": r["goal"],
            "status": report_status(r["outcome"]),
            "outcome": r["outcome"], "turn": r["turns"],
            "max_turns": max_turns, "reason": r["reason"],
            "verified": r.get("verified"),
            "verify_attempts": r.get("verify_attempts", 0),
            # Carried through from relay_fleet.run_relay_fleet's final return value (which now
            # sets it from its own `run_id` parameter). Without this the LIVE snapshot (built by
            # _snapshot()/_run_id_of() every tick) had run_id, but the FINAL snapshot -- the one
            # on disk the instant `running` flips to False, which is exactly when the cockpit's
            # ArchiveTerminal sees every worker terminal at once -- did not. Same defect class as
            # `verified`/`verify_attempts` above, just in the OTHER snapshot builder.
            "run_id": r.get("run_id", ""),
            # THE ADMISSION-TIME ID (see _snapshot()'s matching field for the full story).
            # Same "final snapshot never got what the live one had" gap as run_id above --
            # relay_fleet.py's return dict now carries jid too, so this just has to read it.
            "jid": r.get("jid", ""),
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
            "task_id": r.get("task_id", ""),
            "parent_task_id": r.get("parent_task_id"),
            "campaign_id": r.get("campaign_id", ""),
            "role": r.get("role", ""),
            "depth": r.get("depth", 0),
            "goal_hash": r.get("goal_hash", ""),
            "fresh_replay_count": r.get("fresh_replay_count", 0),
            "refusal_count": r.get("refusal_count", 0),
            "refusal_history": r.get("refusal_history", []),
            "recovery_cause": r.get("recovery_cause", ""),
            "recovery_result": r.get("recovery_result", ""),
            "recovery_state": r.get("recovery_state", ""),
            "attempt_transcripts": r.get("attempt_transcripts", []),
        }

    elapsed = round(time.time() - started, 1)
    # ONE DEFINITION OF DONE. This counted outcome == "DONE" directly while the rest of
    # the file asked report_status, so a fan-out that split, ran nine subtasks, merged them
    # and wrote its answer to disk was reported as 0 done of 1: FANOUT is not the string
    # "DONE", and the second definition had never heard of it.
    done_count = sum(1 for r in results if report_status(r["outcome"]) == "done")
    # And the total is the work that RAN, which is what status.json said all through the
    # run (len(workers)). Reporting len(goals) at the end shrank a seventeen-worker
    # campaign back to the one goal it started as, at the moment somebody reads it.
    final = {"started": started, "updated": time.time(), "total": len(results),
             "done_count": done_count, "running": False, "elapsed_s": elapsed,
             "directive": directive,
             # FIX 3 (P2): also carry run_label / goal_count into the final snapshot.
             "run_label": run_label, "goal_count": goal_count,
             "workers": [_final_worker_entry(r, args.max_turns) for r in results]}
    _ffv = fanout_family_view(final["workers"])
    for _fw in final["workers"]:
        _fw["fanout"] = _ffv.get(_fw["name"], {"kind": "solo", "campaign_id": _fw.get("campaign_id", ""), "label": ""})
    _write_atomic(status_path, final)
    # RUN-RESUME: merge this FINAL CHUNK into the durable completion map. ``results`` is not
    # necessarily the whole run after reconnects / graceful stop, so replacement here would
    # erase earlier DONE goals and replay them on --resume.
    try:
        _merge_final_done_map(args.state_dir, results)
    except Exception as e:
        sys.stderr.write("[resume] WARN: could not merge final done map: %s\n" % e)
    print("\n\n=== fleet complete in %ss ===" % elapsed)
    for r in results:
        print("  %-4s %-8s turns=%d  %s" % (r["name"], r["outcome"], r["turns"],
                                            (r["goal"][:60] + "...") if len(r["goal"]) > 60 else r["goal"]))
        if r["reason"]:
            print("       reason: %s" % r["reason"])

    # Printed at the end of the run, because a detector whose output nobody reads is
    # the defect this repository keeps rediscovering.
    report_duplicate_completions(args.state_dir)

    # CLEAN COMPLETION: this point is reached whenever the run ends on its own -- goals
    # ran to a terminal outcome, OR a graceful `stop` command (bench/fleet_ctl.py stop /
    # the cockpit) cancelled everything and the loop above still exited normally. Either
    # way nothing is "interrupted" -- clear the ACTIVE marker so a supervisor never
    # mistakes a normal finish for a crash.
    _clear_active_marker(args.state_dir, owner_pid=os.getpid())
    _release_run_lock(_ACTIVE_RUN_LOCK)
    _ACTIVE_RUN_LOCK = None


if __name__ == "__main__":
    try:
        raise SystemExit(main() or 0)
    except KeyboardInterrupt:
        # Explicit user stop (Ctrl+C). Clear the ACTIVE marker (if a run had started and
        # recorded one) so a supervisor never treats a deliberate interrupt as a crash to
        # auto-resume, then exit with the conventional SIGINT status.
        if _ACTIVE_STATE_DIR:
            _clear_active_marker(_ACTIVE_STATE_DIR, owner_pid=os.getpid())
        _release_run_lock(_ACTIVE_RUN_LOCK)
        _ACTIVE_RUN_LOCK = None
        print("\n[fleet] interrupted by user -- ACTIVE marker cleared, not auto-resumable.")
        sys.exit(130)

# -*- coding: utf-8 -*-
"""How much of the Copilot generative-message quota this fleet is using, right now.

WHY IT EXISTS. For an entire night a dense run was refused 217 turns out of 237 and nobody
could say why, because nothing measured the thing the limiter counts. The fleet's own
instrumentation counted MCP tool calls through our gateway -- a number we had, rather than the
number that matters -- and comparing it against Microsoft's published quota gave an answer that
was wrong by an unknown factor. A turn may make no tool calls at all, or several.

WHAT THE LIMITER COUNTS. Microsoft publishes the generative-orchestration quota by name; the
error text `GenAIToolPlannerRateLimitReached` is documented, not internal:

    Microsoft 365 Copilot users:  100 requests/minute, 2,000 requests/hour
    scope:                        per Dataverse ENVIRONMENT, not per Entra ID user
    trial / developer:            10 RPM / 200 RPH
    Retry-After contract:         none published for this quota

  https://learn.microsoft.com/en-us/troubleshoot/power-platform/copilot-studio/licensing/throttling-errors-agents
  https://learn.microsoft.com/en-us/microsoft-copilot-studio/requirements-quotas

The per-MINUTE half is the one a fan-out hits. Microsoft says so directly: a design that looks
safe at the weekly or monthly level can still exceed limits during a one-hour peak. Two
thousand an hour is only reachable at a perfectly flat ~33/minute; a burst trips 100/minute
long before the hour's total goes anywhere near it.

WHAT THIS MODULE WILL NOT CLAIM. That being under the line means being safe. The published
number is scoped to a Dataverse environment and Microsoft states that downstream services may
impose their own, lower limits, so the documented ceiling is a REFERENCE, not a guarantee. The
gauge therefore shows measured refusals beside the reference line rather than deriving safety
from it -- if the two disagree, the refusals are right and the line is the wrong line.

BEST EFFORT, ALWAYS. This sits on the send path. A meter that can fail the turn it observes is
worse than no meter.
"""
from __future__ import annotations

import json
import os
import threading
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METER_PATH = os.path.join(_REPO, ".fleet", "quota_meter.jsonl")

#: The documented ceiling for the Microsoft 365 Copilot users row. A REFERENCE LINE, not a
#: guarantee -- see the module note. Overridable because a trial or developer environment is a
#: tenth of this and the operator knows which they have.
LIMIT_RPM = float(os.environ.get("MCP_QUOTA_RPM", "100"))
LIMIT_RPH = float(os.environ.get("MCP_QUOTA_RPH", "2000"))

#: Records older than this are dropped when the file is rewritten. An hour of context is what
#: the RPH figure needs; keeping more would make the meter itself the thing that fills a disk
#: that has already stopped a run tonight.
KEEP_S = 7200.0

_LOCK = threading.Lock()


def _oldest_ts(path):
    """The timestamp of the FIRST row, or 0.0. One line, so this cost does not grow with the
    file -- which is the whole point of checking it on every append."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            line = fh.readline().strip()
        return float(json.loads(line).get("ts") or 0.0) if line else 0.0
    except (OSError, ValueError, AttributeError):
        return 0.0


def _append(row):
    try:
        with _LOCK:
            os.makedirs(os.path.dirname(METER_PATH), exist_ok=True)
            with open(METER_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
    # ENFORCE THE RETENTION THIS FILE DECLARES. `KEEP_S` says records older than two hours are
    # dropped "when the file is rewritten", and `prune` does the rewriting -- and nothing called
    # it. Measured on the live meter 2026-09-14: 4,845 rows spanning 12.8 DAYS, 100% of them
    # past the window. The reason that matters is written beside KEEP_S itself: keeping more
    # "would make the meter itself the thing that fills a disk that has already stopped a run
    # tonight." The mitigation for a real incident was written and never wired.
    #
    # AND IT COSTS THE READERS TOO. `snapshot()` parses the WHOLE file to answer a question
    # about the last minute, so an unpruned meter makes every admission check slower without
    # bound.
    #
    # TRIGGERED ON THE OLDEST ROW, NOT A SIZE OR A COUNTER. A byte threshold is a number
    # somebody invents; a counter resets with the process. "The first row is older than twice
    # the window" is the policy restating itself, and reading one line does not get slower as
    # the file grows. Twice rather than once so an append never rewrites a file that is merely
    # at the edge.
    try:
        oldest = _oldest_ts(METER_PATH)
        if oldest and (float(row.get("ts") or time.time()) - oldest) > 2 * KEEP_S:
            prune()
    except Exception:
        pass


def record_turn(worker: str = "", conv: str = "", ts: float = None) -> None:
    """One generative message left for Copilot. Call at the moment the send succeeds.

    NOT when a worker is admitted, and not when a tool is called. The quota is spent by the
    turn, and a worker that is editing files or running tests for ten minutes is spending
    nothing -- which is why admitting WORKERS is the wrong control and admitting TURNS is the
    right one.
    """
    _append({"ts": float(ts if ts is not None else time.time()), "event": "turn",
             "worker": str(worker or "")[:64], "conv": str(conv or "")[:80]})


def record_refusal(kind: str, worker: str = "", ts: float = None) -> None:
    """A turn the upstream refused. `kind` is the CLASS, and the classes are not interchangeable:

      rate      -- the limiter said no. This is a capacity signal; back off.
      transport -- a disconnect or a deadline. Retry at the same concurrency; it says nothing
                   about capacity unless it is fleet-wide.
      content   -- the model declined. NOT a capacity signal, and backing off for it teaches the
                   controller from a label that has nothing to do with load.

    Keeping them apart is the point. The controller that existed before this treated all three
    the same and therefore learned from contaminated labels.
    """
    _append({"ts": float(ts if ts is not None else time.time()), "event": "refusal",
             "kind": str(kind or "unknown")[:24], "worker": str(worker or "")[:64]})


def read(path: str = None, since: float = None):
    rows = []
    try:
        with open(path or METER_PATH, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if since is None or float(row.get("ts") or 0) >= since:
                    rows.append(row)
    except OSError:
        return []
    return rows


def snapshot(now: float = None, path: str = None) -> dict:
    """What the cockpit draws. Never raises; an unreadable meter reports zeros and says so.

    `headroom_rpm` is turns-per-minute still available against the REFERENCE line. It is not a
    promise -- `refusals_5m` sitting above zero while headroom looks comfortable is the meter
    telling you the reference line is not the binding one, and that is a finding rather than a
    contradiction.
    """
    now = time.time() if now is None else now
    rows = read(path, since=now - 3600.0)
    turns = [r for r in rows if r.get("event") == "turn"]
    refusals = [r for r in rows if r.get("event") == "refusal"]
    # BOUNDED AT BOTH ENDS. This was `>= now - 60` alone, which is every row after that
    # instant -- including rows AFTER `now`. With now=time.time() there are none, which is why
    # it survived; with a past `now`, the only reason this parameter exists, it counted the
    # whole file. Measured: snapshot(now=<first row's ts>) reported rpm 4698.
    rpm = sum(1 for r in turns if now - 60.0 <= r["ts"] <= now)
    rph = sum(1 for r in turns if r["ts"] <= now)
    by_kind = {}
    for r in refusals:
        if r["ts"] >= now - 300.0:
            by_kind[r.get("kind", "unknown")] = by_kind.get(r.get("kind", "unknown"), 0) + 1
    # THE DENOMINATOR sustainable_workers NEEDS, counted rather than estimated. record_turn
    # has written `worker` on every turn row since the meter existed, so "how many workers were
    # spending quota in that minute" is a set size. Without it that function returns 0.0 for
    # any snapshot, which is why it had no caller worth having.
    workers_1m = len({(r.get("worker") or "") for r in turns
                      if now - 60.0 <= r["ts"] <= now and r.get("worker")})
    per_worker_rpm = (float(rpm) / workers_1m) if workers_1m else 0.0
    return {
        "rpm": rpm,
        "rph": rph,
        "workers_1m": workers_1m,
        "per_worker_rpm": round(per_worker_rpm, 3),
        "limit_rpm": LIMIT_RPM,
        "limit_rph": LIMIT_RPH,
        "pct_rpm": (100.0 * rpm / LIMIT_RPM) if LIMIT_RPM > 0 else 0.0,
        "pct_rph": (100.0 * rph / LIMIT_RPH) if LIMIT_RPH > 0 else 0.0,
        "headroom_rpm": max(0.0, LIMIT_RPM - rpm),
        "refusals_5m": sum(by_kind.values()),
        "refusals_by_kind": by_kind,
        # A minute-by-minute series for the last hour, oldest first, so the cockpit can draw a
        # shape rather than a single number. A single number cannot show a burst.
        "series_rpm": _series(turns, now),
        "measured": bool(rows),
    }


def _series(turns, now, minutes=60):
    buckets = [0] * minutes
    for r in turns:
        age = now - float(r.get("ts") or 0)
        if 0 <= age < minutes * 60:
            buckets[minutes - 1 - int(age // 60)] += 1
    return buckets


def sustainable_workers(snap: dict, per_worker_rpm: float = None) -> float:
    """How many workers the REFERENCE line would support at the observed per-worker turn rate.

    Derived, not chosen. The fleet's concurrency has been a number typed on a command line; this
    is what the arithmetic says it should be. Returns 0.0 when there is nothing measured to
    divide by, because a made-up denominator is how the last estimate went wrong.
    """
    # FROM THE SNAPSHOT WHEN THE CALLER DOES NOT SAY. The argument stays, because a caller
    # asking "what if each worker spent twice as much" is a real question -- but requiring it
    # meant every caller had to compute a number the meter was already holding, and none did.
    rate = per_worker_rpm if per_worker_rpm else float(snap.get("per_worker_rpm") or 0.0)
    if rate <= 0:
        return 0.0
    return round((snap.get("limit_rpm") or 0.0) * 0.7 / rate, 1)


def prune(path: str = None, now: float = None) -> int:
    """Drop records older than KEEP_S. Returns how many were kept."""
    now = time.time() if now is None else now
    p = path or METER_PATH
    rows = [r for r in read(p) if float(r.get("ts") or 0) >= now - KEEP_S]
    try:
        with _LOCK:
            with open(p, "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError:
        return 0
    return len(rows)


def main(argv=None) -> int:
    """`python -m relay.quota_meter` -- what the meter knows, and what it implies.

    A NUMBER NOBODY PRINTS IS A NUMBER NOBODY ACTS ON. `sustainable_workers` existed for
    fourteen days without a caller while the meter recorded 4,698 turns, and the concurrency it
    is about stayed a number typed on a command line.
    """
    import argparse

    ap = argparse.ArgumentParser(description="Copilot turn-quota meter.")
    ap.add_argument("--per-worker-rpm", type=float, default=None,
                    help="override the observed rate (to ask 'what if each worker spent more')")
    args = ap.parse_args(argv)

    snap = snapshot()
    if not snap.get("measured"):
        print("the meter has no records in the last hour")
        return 0
    print("turns    %d/min (%.0f%% of %g)   %d/hour (%.0f%% of %g)"
          % (snap["rpm"], snap["pct_rpm"], snap["limit_rpm"],
             snap["rph"], snap["pct_rph"], snap["limit_rph"]))
    print("workers  %d active in the last minute, %.2f turns each"
          % (snap["workers_1m"], snap["per_worker_rpm"]))
    if snap["refusals_5m"]:
        print("refused  %d in the last 5 min: %s"
              % (snap["refusals_5m"],
                 ", ".join("%s x%d" % kv for kv in sorted(snap["refusals_by_kind"].items()))))
    n = sustainable_workers(snap, args.per_worker_rpm)
    if n <= 0:
        print("sustainable workers: unknown -- nothing measured to divide by")
    else:
        print("sustainable workers: %.1f at the observed rate (reference line x0.7)" % n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

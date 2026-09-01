"""Did the last run keep the promise the code makes about how often it captures?

WHAT THIS IS FOR. The route captures when a token is near expiry. When the margin is longer
than the token -- which this tenant produces about one run in twenty-six -- that condition is
true again the instant the capture meant to satisfy it finishes, so the route captures again.
Measured at 3.8 a minute, each holding a page for thirty-five seconds. The browser was never
without one. It ran for weeks and every individual capture was correct: there was nothing to
see in any single event, only in how many of them there were.

WHAT IT ASSERTS, AND WHAT IT DELIBERATELY DOES NOT.

Not a rate against a measured threshold. The measured distribution is the tenant's behaviour
today -- median token 52 minutes -- and freezing it into a check means the check cries wolf the
day Microsoft changes it, or worse, gets "fixed" by making it adaptive, which is a machine for
learning to call the next defect normal. What is asserted instead is an ARITHMETIC CONSEQUENCE
of a floor this code enforces on itself:

    captures <= ceil(elapsed / MIN_CAPTURE_INTERVAL_S) + surfaces + slack

relay/capture_floor.py will not let the real capture run more often than that, so a run that
exceeds it did not have the floor, or the floor is broken. Both are worth stopping for. The
constant is imported rather than copied, because a check that hardcodes a value somebody can
change by environment variable is a false alarm waiting for a config edit.

Second, the DUTY CYCLE: how much of the run had a capture page open. This is the better
quantity, because the defect was not really "3.8 a minute" -- it was that the browser was never
WITHOUT a page. A count cannot tell a 4-second page from a 35-second one; a duty cycle can, and
it catches a future defect a count would miss, where captures are normal in number but each one
hangs to its timeout.

Third, INSTRUMENT LIVENESS. Both figures are counted by matching text in a log and claim
records in a ledger. If the wording changes, the count silently becomes zero and the check goes
green for ever -- the same shape as an allowlist that fails open. So a run with socket workers
and no capture at all is reported as a contradiction rather than as a pass.

WHAT IT LOOKS AT. The PREVIOUS run, because a launch gate runs before there is a current one.
That is the intended meaning: do not stack another run on top of evidence that the last one
misbehaved. It is also the trap -- a red verdict has no natural way to clear, since the run
that would clear it is the one being blocked. Hence acknowledge().
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEDGER = os.path.join(REPO, ".fleet", "ownership.jsonl")
ACKED = os.path.join(REPO, ".fleet", "capture_budget_acked.json")

#: How much of a run may have a capture page open before it is worth stopping for. Normal is
#: about 0.5% -- 36 recorded spans with a median of 4.0 seconds -- and the spin was near 100%.
#: With two orders of magnitude between them the threshold does not need to be precise, and a
#: generous one is what keeps the verdict line trustworthy.
MAX_DUTY = float(os.environ.get("MCP_CAPTURE_MAX_DUTY", "0.10"))

#: Captures allowed beyond the floor's arithmetic. Covers the coordinator being restarted
#: mid-run, which resets an in-memory floor, and any burst at admission.
SLACK = int(os.environ.get("MCP_CAPTURE_SLACK", "3"))

_CAPTURE = re.compile(r"captured: ([0-9]+) min of token, agent (\S+)")
_WORKER_DONE = re.compile(r"worker_done")


def newest_log(directory=None):
    # BOTH FORMS. Finished coordinator logs are gzipped in place (they compress by 99%), so a
    # glob for "*.log" alone would stop finding anything the moment the newest few rolled over
    # -- this function would return None and the budget check would read a run as having made
    # no captures at all, which is indistinguishable from a run that made none.
    base = directory or os.path.join(REPO, ".fleet")
    logs = (glob.glob(os.path.join(base, "coordinator_*.log"))
            + glob.glob(os.path.join(base, "coordinator_*.log.gz")))
    return max(logs, key=os.path.getmtime) if logs else None


def _floor_interval_s():
    """The floor the code enforces, from the module that enforces it -- never a copy."""
    try:
        import sys
        sys.path.insert(0, REPO)
        from relay.capture_floor import MIN_CAPTURE_INTERVAL_S
        return float(MIN_CAPTURE_INTERVAL_S)
    except Exception:
        return 120.0


def read_log(path, elapsed_s=None):
    """(captures, distinct agent surfaces, elapsed seconds, socket workers) for one run.

    ELAPSED COMES FROM THE FILE'S OWN TIMESTAMPS -- created when the run starts, last
    written when it ends -- rather than from the wall clock, so a check run an hour later
    measures the run and not the hour. `elapsed_s` overrides it for tests, because Windows
    will not let os.utime move a creation time and a fixture cannot otherwise make a file
    look ten minutes old.
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        if path.endswith(".gz"):
            import gzip
            text = gzip.open(path, "rt", encoding="utf-8", errors="replace").read()
        else:
            text = open(path, encoding="utf-8", errors="replace").read()
    except (OSError, IOError):
        return None
    found = _CAPTURE.findall(text)
    elapsed = (float(elapsed_s) if elapsed_s is not None
               else max(os.path.getmtime(path) - os.path.getctime(path), 0.0))
    return {"captures": len(found),
            "surfaces": len({agent for _life, agent in found}) or 1,
            "elapsed_s": elapsed,
            "socket_workers": len(_WORKER_DONE.findall(text)),
            "path": path}


def capture_spans(since=None, path=None):
    """(open, close) timestamps for pages that were claimed AND released.

    ONLY THE PAIRED ONES, and that is a real limitation to state rather than hide: an ordinary
    worker tab is claimed by _open_fresh and never explicitly released -- the lease and the pid
    check retire it -- so this measures CAPTURE pages, not every page. That is the quantity
    wanted here, and it is not the same as "how much page was open in total".
    """
    held, spans = {}, []
    try:
        with open(path or LEDGER, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("kind") != "page":
                    continue
                key, ts = rec.get("key"), float(rec.get("ts") or 0)
                if rec.get("state") == "released":
                    if key in held:
                        spans.append((held.pop(key), ts))
                else:
                    held.setdefault(key, ts)
    except OSError:
        return []
    if since is not None:
        spans = [s for s in spans if s[1] >= since]
    return spans


def acknowledge(run_path, reason, path=None):
    """Record that a red verdict for this run has been seen and explained.

    THE RED HAS TO BE ABLE TO CLEAR. The check reads the previous run, so the run that would
    prove a fix is the one the gate is blocking -- without this, the first red produces a
    culture of forcing past the gate, and the gate dies. An acknowledgement is a recorded
    decision with a reason, not a switch.
    """
    target = path or ACKED
    try:
        data = json.load(open(target, encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[os.path.basename(run_path or "")] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "reason": str(reason)[:300]}
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return data


def _acked(run_path, path=None):
    try:
        data = json.load(open(path or ACKED, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get(os.path.basename(run_path or ""))


def verdict(log_path=None, ledger=None, acked_path=None, now=None, elapsed_s=None):
    """(ok, detail) for the last run's capture budget. Never raises."""
    path = log_path if log_path is not None else newest_log()
    run = read_log(path, elapsed_s=elapsed_s)
    if not run:
        return True, "no run on record yet"

    interval = _floor_interval_s()
    allowed = int(math.ceil(run["elapsed_s"] / max(interval, 1.0))) + run["surfaces"] + SLACK
    over_count = run["captures"] > allowed

    since = None
    if os.path.isfile(path):
        since = os.path.getmtime(path) - run["elapsed_s"]
    spans = capture_spans(since=since, path=ledger)
    open_s = sum(b - a for a, b in spans)
    duty = (open_s / run["elapsed_s"]) if run["elapsed_s"] > 1 else 0.0
    over_duty = duty > MAX_DUTY

    # A count of zero is not evidence of virtue: the log wording could have changed and the
    # counter gone quietly to zero, which would keep this green for ever.
    blind = run["captures"] == 0 and run["socket_workers"] > 0

    ack = _acked(path, acked_path) if (over_count or over_duty or blind) else None
    detail = ("last run %s: %d capture(s) in %.0f min (floor allows %d), page open %.0fs "
              "= %.2f%% duty%s"
              % (os.path.basename(path or "-"), run["captures"], run["elapsed_s"] / 60.0,
                 allowed, open_s, 100.0 * duty,
                 "  [acknowledged: %s]" % ack["reason"][:60] if ack else ""))
    if blind and not ack:
        return False, (detail + "  -- and %d socket workers finished with NO capture recorded, "
                                "which cannot happen: the counter has gone blind"
                       % run["socket_workers"])
    if (over_count or over_duty) and not ack:
        return False, detail
    return True, detail


if __name__ == "__main__":
    ok, why = verdict()
    print(("[ok] " if ok else "[XX] ") + why)

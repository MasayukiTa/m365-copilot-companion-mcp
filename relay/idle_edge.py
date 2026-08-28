"""Whether the fleet's browser may be released while nothing is using it.

WHAT IT COSTS TO KEEP. Measured 2026-08-28 with no run in flight: the fleet Edge held eight
processes and 273 MB for a single about:blank, and the bridge Edge another 249 MB beside it.
The blank page itself is a few MB -- it exists because Edge exits when its last page closes,
and that is a good reason. The cost is the browser the page keeps alive.

WHY ONLY THE FLEET'S. The fleet already starts from a dead browser as its ordinary path:
fleet_runner checks `cdp_alive` and hard-resets when it is not, and recycles even a live one
that has bloated. Nothing about a run depends on the previous run's Edge still being there,
so releasing it costs one cold start at the head of the next run.

The bridge is not like that. It has no equivalent re-entry point, its CDP watchdog reads a
dead Edge as a reason to exit and let the supervisor rebuild everything, and a cold start can
land on a login wall -- which needs a person, in the foreground. Trading 249 MB for "the
conversational path is down until someone notices" is not a trade. This module does not touch
:9223, and there is a test that says so.

UNKNOWN MEANS OCCUPIED, HERE. edge_recover.other_fleet_runs answers "no siblings" when it
cannot enumerate processes, which is right for its own purpose: refusing to recover a wedged
browser because psutil is missing turns a diagnostic gap into an outage. This is the opposite
kind of decision -- discretionary, deferrable, and destructive to a sibling if wrong. On
2026-08-25 four runs shared this profile, one reset the browser, and the run beside it lost
its context mid-turn. So an unreadable process list means "someone may be working", and the
release waits.
"""
from __future__ import annotations

import os
import time

#: The port this module is willing to release. Named, and checked, because the two ports it
#: must never touch are one keystroke away: :9223 is the bridge and :9224 the eval host.
FLEET_CDP_PORT = 9222

#: How long a browser must sit unused before it is released. Not zero: goals arrive in bursts,
#: a resume reconnects moments after a run ends, and paying a cold start for a gap of seconds
#: would cost more than it saves. Fifteen minutes is a judgement, not a measurement.
IDLE_GRACE_S = float(os.environ.get("MCP_FLEET_EDGE_IDLE_S", "900"))


def siblings(port=FLEET_CDP_PORT, exclude_pid=None):
    """(pids, certain). `certain` is False when the process list could not be read.

    edge_recover.other_fleet_runs collapses both cases to an empty list. Here they have to
    stay apart, because the caller's safe direction is the other one.
    """
    try:
        import psutil                                    # noqa: F401
    except Exception:
        return [], False
    try:
        from relay.edge_recover import other_fleet_runs
        return list(other_fleet_runs(port, exclude_pid=exclude_pid)), True
    except Exception:
        return [], False


def may_release(status, now=None, port=FLEET_CDP_PORT, idle_grace_s=None,
                sibling_fn=None, pages=None):
    """(bool, reason). Pure: every input is passed in, so the decision is testable alone.

    `status` is the parsed .fleet/status.json (or None if absent). `pages` is the CDP page
    count when known -- a browser holding a real page is in use whatever the file says.
    """
    now = time.time() if now is None else now
    grace = IDLE_GRACE_S if idle_grace_s is None else idle_grace_s

    if int(port) != FLEET_CDP_PORT:
        return False, "port %s is not the fleet's; this releases only %d" % (port, FLEET_CDP_PORT)

    if status is None:
        # NO STATUS FILE IS NOT PROOF OF IDLENESS. It is proof that nothing has written one,
        # which is also what a fleet looks like in the seconds before its first write.
        return False, "no run status to read; treating the browser as in use"

    if status.get("running"):
        return False, "a run is in flight"

    workers = status.get("workers") or []
    unfinished = [w for w in workers if (w.get("status") or "") not in
                  ("done", "stuck", "maxturns", "error", "cancelled", "content_refused")]
    if unfinished:
        return False, "%d worker(s) have not finished" % len(unfinished)

    if pages is not None and pages > 1:
        # One page is the keep-alive. More than one means something is open that a run put
        # there, and the status file has not caught up.
        return False, "%d page(s) open, more than the keep-alive" % pages

    peers, certain = sibling_fn() if sibling_fn else siblings(port)
    if not certain:
        return False, "could not read the process list; assuming another run may be using it"
    if peers:
        return False, "%d other fleet run(s) share this browser" % len(peers)

    last = _last_activity(status)
    if last is None:
        return False, "the run's finish time is unknown"
    idle = now - last
    if idle < grace:
        return False, "idle for %.0fs, under the %.0fs grace" % (idle, grace)

    return True, "idle %.0fs, no run, no siblings" % idle


def _last_activity(status):
    """When the run last did anything, from whichever field carries it."""
    for key in ("updated", "finished", "started"):
        try:
            v = float(status.get(key) or 0)
        except Exception:
            continue
        if v > 0:
            return v
    return None

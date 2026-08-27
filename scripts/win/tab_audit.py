"""Watch, during a run, for a tab that had no business being open.

A TAB IS NOT THE PROBLEM. It is the fallback, and the fallback is a working path: when the
socket route is blocked a worker takes a tab and finishes the job. Research and analyst turns
need one too. And the socket route itself is BUILT from a tab -- the token is captured by
opening one, reading it and closing it again, which is why tabs appear on a run that never
falls back at all.

What is not allowed is a tab that carried work while the socket route was open and healthy,
or a Copilot page that stays open with nobody using it. This tells those apart:

  * every Copilot page is timed. One that closes inside CAPTURE_GRACE_S is a capture doing its
    job. One that outlives it, with no run in flight, is residue.
  * every worker_done carried on a tab is checked against the fallback record. A tab-carried
    worker with a route fault behind it is the fallback working. One without is the thing to
    stop and explain.

Sampled every 2 seconds rather than the 15 the stack guard uses: a capture tab lives for about
eight, and a 15-second sampler cannot tell "opened and closed properly" from "never opened".

    python scripts/win/tab_audit.py --out .fleet/tab_audit.log
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROUTE_LOG = os.path.join(REPO, ".fleet", "socket_route.jsonl")
PORTS = {9222: "companion", 9223: "bridge"}

#: How long a Copilot page may live before it stops looking like a token capture. Captures
#: measured 8-16 seconds across 200 of them; 45 is generous and still far below the nine and a
#: half hours a leaked page once sat there.
CAPTURE_GRACE_S = float(os.environ.get("TAB_AUDIT_GRACE_S", "45"))


def pages(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=3) as fh:
            return {t.get("id"): t.get("url", "") for t in json.load(fh)
                    if t.get("type") == "page"}
    except Exception:
        return None


def fleet_running():
    try:
        with open(os.path.join(REPO, ".fleet", "status.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return bool(d.get("running"))
    except Exception:
        return False


def route_events(offset):
    """New lines from the route log since `offset`. Returns (events, new_offset)."""
    out = []
    try:
        size = os.path.getsize(ROUTE_LOG)
        if size < offset:
            offset = 0
        with open(ROUTE_LOG, encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
            offset = fh.tell()
    except OSError:
        pass
    return out, offset


def run_started_iso():
    """When the run in flight began, as an ISO string, or None."""
    try:
        with open(os.path.join(REPO, ".fleet", "status.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        if not d.get("running") or not d.get("started"):
            return None
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(d["started"])))
    except Exception:
        return None


def route_faulted_since(iso):
    """Has the route faulted at any point since `iso`?

    Asked of the LOG, covering the whole run, rather than of a counter this process has been
    keeping. A monitor restarted at 12:31 saw none of the faults from 12:08 and reported the
    fallback they caused as the one thing that must never happen -- twice, once for the worker
    and once for the page it was holding. What is true about the run does not depend on when
    anybody started watching it.
    """
    if not iso:
        return False
    try:
        with open(ROUTE_LOG, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if (o.get("at") or "") < iso:
                    continue
                if o.get("event") in ("fallback", "socket_retry", "route_closed"):
                    return True
                if o.get("event") == "worker_done" and o.get("fell_back"):
                    return True
    except OSError:
        pass
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(REPO, ".fleet", "tab_audit.log"))
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--minutes", type=float, default=600.0)
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    log = open(args.out, "a", encoding="utf-8", buffering=1)

    def say(kind, msg):
        line = "%s %-10s %s" % (time.strftime("%H:%M:%S"), kind, msg)
        print(line, flush=True)
        log.write(line + "\n")

    say("start", "grace=%.0fs interval=%.1fs" % (CAPTURE_GRACE_S, args.interval))
    faults = 0                    # route faults seen this session; a tab is legitimate after one
    route_shut = False            # once shut, a tab is the only path a worker has
    seen = {}                       # (profile, page id) -> first time seen
    reported = set()
    _, offset = route_events(0)     # start from the end: only this run's events
    deadline = time.time() + args.minutes * 60

    while time.time() < deadline:
        time.sleep(args.interval)
        now = time.time()
        running = fleet_running()

        for port, profile in PORTS.items():
            live = pages(port)
            if live is None:
                continue
            for pid, url in live.items():
                if "m365.cloud.microsoft" not in url:
                    continue
                key = (profile, pid)
                first = seen.setdefault(key, now)
                age = now - first
                if age > CAPTURE_GRACE_S and key not in reported:
                    reported.add(key)
                    # A LONG-LIVED PAGE IS NOT AUTOMATICALLY RESIDUE. A worker that fell back
                    # holds a tab for as long as its turn takes -- 272 seconds in the mixed
                    # test that found this -- and calling that a leak is a false alarm about
                    # the fallback doing exactly what it exists to do. The three cases differ:
                    #
                    #   no run in flight        -> residue. This is the one that cost nine and
                    #                              a half hours and 411 MB.
                    #   run, route has faulted  -> a fallback worker at work. Expected.
                    #   run, no fault at all    -> a tab while the socket was available, which
                    #                              is the thing that must never happen.
                    if not running:
                        say("RESIDUE", "%s page open %.0fs with NO run in flight %s"
                            % (profile, age, url[:70]))
                    elif faults or route_faulted_since(run_started_iso()):
                        say("fallback tab", "%s page open %.0fs (route faults: %d) -- a "
                            "fallback worker holding its tab" % (profile, age, faults))
                    else:
                        say("TAB WORK", "%s page open %.0fs during a run with NO route fault "
                            "-- a tab while the socket was available %s"
                            % (profile, age, url[:70]))
            for key in [k for k in seen if k[0] == profile and k[1] not in live]:
                age = now - seen.pop(key)
                # ALWAYS report the close, including for a page already flagged as residue.
                # The first version skipped those, so the one page whose lifetime actually
                # mattered -- the one that overran the grace -- was the one whose total
                # lifetime never got recorded. "It went over 45s" and "it went over 45s and
                # then took another minute" need different answers.
                if key in reported:
                    reported.discard(key)
                    say("CLOSED", "%s page finally closed after %.0fs" % (profile, age))
                else:
                    say("capture", "%s page lived %.0fs and closed -- a token capture"
                        % (profile, age))

        events, offset = route_events(offset)
        for ev in events:
            kind = ev.get("event")
            if kind in ("fallback", "socket_retry", "route_closed"):
                faults += 1
                if kind == "route_closed":
                    route_shut = True
                say(kind, str(ev.get("reason") or ev.get("detail") or "")[:110])
            elif kind == "worker_done" and ev.get("route") == "tab":
                # JUDGE FROM THE RECORD, NOT FROM WHAT THIS PROCESS HAPPENED TO WITNESS.
                # The first version asked its own session counter -- "have I seen a route
                # fault since I started?" -- and restarting the monitor at 12:11 made it
                # answer no for a worker whose faults occurred at 12:08. It reported a
                # perfectly ordinary fallback as the one thing that must never happen.
                #
                # The worker writes `fell_back` on the event itself. That is authoritative
                # and does not depend on when anybody started watching. The session counter
                # stays as context, never as the verdict.
                # A worker admitted AFTER the route closed never "fell back" -- it was born
                # on a tab, because there was no socket to leave. `fell_back` alone reads that
                # as the forbidden case. Once the route is shut, a tab is the only path there
                # is, and every worker after that point is legitimately on one.
                legitimate = bool(ev.get("fell_back")) or route_shut
                say("tab work" if legitimate else "TAB WORK",
                    "%s carried on a tab (fell_back=%s, faults seen this session: %d) %s"
                    % (ev.get("worker") or "?", legitimate, faults,
                       str(ev.get("reason") or "")[:70]))
    say("stop", "audit window ended")
    return 0


if __name__ == "__main__":
    sys.exit(main())

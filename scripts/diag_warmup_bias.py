"""Settle, by measurement, a disagreement between two reviewers about the warm-up.

THE DISAGREEMENT. Every arm of the series is preceded by a warm-up pass that drives the TABS
route, whichever transport the arm itself will use, and the baseline the arm is measured
against is taken after it. So the browser starts each arm holding a renderer that the tabs arm
will reuse for free and the socket arm will simply let decay. Both reviewers called this the
central problem and then signed it in opposite directions:

  * One argued the tabs arm's bill is missing a whole renderer -- 170 to 255 MB by the per-arm
    detail -- because a cold deployment would pay for it and a socket deployment may never need
    it. Add that to the measured 150 and the switching benefit clears the 300 MB floor, and the
    verdict flips from "not worth switching" to "worth switching".
  * The other argued the direction is not identifiable in advance, and that the mechanism most
    worth fearing runs the other way: the warm renderer DECAYS during a socket arm because
    nothing is using it, which makes socket look cheap for reasons that have nothing to do with
    sockets. Then the true benefit is below 150, not above it.

Both cannot be right, and the difference spans the decision. Neither can be settled by more
runs of the same design, because the design is what they disagree about.

WHAT THIS MEASURES, AND WHY IT NEEDS NO NEW HARNESS. A null run with the warm-up switched off
puts the same transport in both arms with nothing warmed beforehand, so its FIRST arm is cold
and its second is warm -- the comparison one reviewer asked for, taken from a single run. And
the browser is left in a tabs-warmed state when a tabs run finishes, so sitting still and
watching it afterwards is the passive-decay measurement the other asked for. One run per
transport plus an idle period answers both.

Absolute working set is recorded throughout, from outside, because every quantity in dispute is
about what the BASELINE contains -- and a delta measured against that baseline cannot describe
it.

THIS IS A DIAGNOSTIC, NOT AN EXTENSION OF THE SERIES. Its runs carry warmup=False and the
archive refuses to pool them with the series on that axis. Saying so out loud matters: the
same night, four runs measured against a windowed browser sat in one column with a headless
one because a population change went in without a label.

  python scripts/diag_warmup_bias.py --transport tabs   --idle-s 360
  python scripts/diag_warmup_bias.py --transport socket --idle-s 0
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, ".fleet", "diag")
CDP = os.environ.get("SERIES_CDP_URL", "http://127.0.0.1:9224")


def _stamp(events, name):
    events.append({"event": name, "ts": round(time.time(), 1)})
    print("[diag] %s %s" % (time.strftime("%H:%M:%S"), name), flush=True)


def rebuild(events):
    """A fresh browser, or the reason there is not one. Cold means cold."""
    script = os.path.join(REPO, "scripts", "start_eval_edge.ps1")
    port = CDP.rsplit(":", 1)[-1].split("/")[0]
    p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", script, "-Port", str(port)],
                       cwd=REPO, capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        return "rebuild exited %d: %s" % (p.returncode, (p.stdout or "").strip()[-160:])
    _stamp(events, "browser_rebuilt")
    return ""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", choices=["tabs", "socket"], required=True)
    ap.add_argument("--idle-s", type=float, default=0.0,
                    help="seconds to sit still after the run, measuring passive decay")
    ap.add_argument("--tag", default="")
    # A LUMP COST CANNOT SIZE A PER-WORKER RESERVATION. Everything measured so far ran three
    # workers at once, so it prices a whole arm and says nothing about what admitting one more
    # worker costs -- which is the only question an admission gate asks. Varying this and
    # reading the slope is what separates the two.
    ap.add_argument("--concurrency", default="3")
    a = ap.parse_args(argv)

    os.makedirs(OUT, exist_ok=True)
    tag = a.tag or time.strftime("%m%d_%H%M")
    base = os.path.join(OUT, "diag_%s_%s" % (a.transport, tag))
    events = []

    why = rebuild(events)
    if why:
        print("[diag] REFUSED: %s" % why)
        return 2

    witness = subprocess.Popen(
        [sys.executable, "-u", os.path.join(REPO, "scripts", "win", "watch_tree_ws.py"),
         "--port", CDP.rsplit(":", 1)[-1].split("/")[0],
         "--out", base + "_ws.csv", "--interval", "3"],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # THE OTHER SIDE OF THE BOUNDARY, PER RUN. The browser sampler stops at the CDP owner's
    # tree, and socket work also lands in the process holding the websocket. Started here rather
    # than shared across a night so each run owns its own trace: a shared file has to be carved
    # back up by timestamp afterwards, and mis-carving it is how two runs end up in one column.
    client = subprocess.Popen(
        [sys.executable, "-u", os.path.join(REPO, "scripts", "win", "watch_client_ws.py"),
         "--pattern", "run_route_campaign", "--out", base + "_client.csv", "--interval", "3"],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(12)                       # a settled reading of the fresh browser
        _stamp(events, "settled_fresh")

        # NO --warmup. That is the whole point: arm one meets a cold browser.
        args = ["--null"] + (["--socket-both"] if a.transport == "socket" else [])
        env = dict(os.environ)
        env["MCP_FLEET_MAX_CONCURRENT"] = a.concurrency
        env["MCP_FLEET_CDP_URL"] = CDP
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("SWE_DISK_FLOOR_GB", "3")

        _stamp(events, "run_start")
        with open(base + "_campaign.log", "w", encoding="utf-8") as fh:
            rc = subprocess.run(
                [sys.executable, "-u", "scripts/run_route_campaign.py", "transport/v1"] + args,
                cwd=REPO, env=env, stdout=fh, stderr=subprocess.STDOUT).returncode
        _stamp(events, "run_end")
        events[-1]["returncode"] = rc

        if a.idle_s > 0:
            # THE SHAM. No socket work, no tab work, the same browser, the same sampler.
            # Whatever memory moves here moved without either transport asking it to.
            _stamp(events, "idle_start")
            time.sleep(a.idle_s)
            _stamp(events, "idle_end")
    finally:
        for proc in (witness, client):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

    try:
        with open(os.path.join(REPO, "docs", "research", "results",
                               "route_campaign.json"), encoding="utf-8") as fh:
            rec = json.load(fh)
    except Exception:
        rec = {}
    with open(base + "_events.json", "w", encoding="utf-8") as fh:
        json.dump({"transport": a.transport, "cdp_url": CDP, "idle_s": a.idle_s,
                   "concurrency": a.concurrency,
                   "events": events, "warmup": rec.get("warmup"),
                   "control": rec.get("control"), "candidate": rec.get("candidate"),
                   "memory_gain_mb": rec.get("memory_gain_mb")}, fh, indent=1)
    print("[diag] wrote %s_events.json" % base)
    return 0


if __name__ == "__main__":                                      # pragma: no cover
    sys.exit(main() or 0)

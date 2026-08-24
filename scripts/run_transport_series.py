"""The transport series: a pre-registered schedule, and the runner that executes it.

WRITTEN BEFORE THE DATA, AND KEPT IN THE SAME FILE AS THE THING THAT COLLECTS IT. A plan in a
separate document drifts from the code that runs it, and a threshold chosen after seeing the
numbers is the ruler cut to fit the object. Three verdicts were produced for this hypothesis in
one day -- p=0.0143, then 0.0238, then 0.21 -- each smaller than the last as the instrument got
more honest, and not one of them was declared in advance.

WHAT IS BEING ASKED. Does talking to the backend over a websocket use less memory than driving a
browser tab? `memory_gain_mb` is control peak minus candidate peak, so positive favours the
socket. The decision threshold is route_evaluator.MIN_MEMORY_GAIN_MB (300 MB) and it does not
move for this series.

THE CONFIGURATION IS FROZEN FOR THE WHOLE SERIES, because run_archive refuses to pool across a
change in any of it, and a setting that moves mid-series tears the series into two columns that
cannot be added up:

    max_concurrent   3      adopted after a two-run smoke: minimum free RAM 2,977 and 2,661 MB
                            against a floor of 512, completion 4/4 in every arm, no stall
    goals            saturated-v1 (4 goals)
    warm-up          on -- a tab pass before EVERY arm
    population       fleet-edge-tree; a run recording anything else is QUARANTINED, not averaged

THE SCHEDULE.

    Phase A   8 nulls: two replicates of {socket-vs-socket, tabs-vs-tabs} x {control-first,
              candidate-first}. Both flavours, because taking nulls under only the socket
              condition is exactly what produced the p=0.0143 that did not survive contact
              with a tabs-vs-tabs null.
    Phase B   8 treatments (four per arm order) interleaved with 4 further nulls, so a drift
              across the night lands on both columns instead of on whichever ran later.

    20 runs at about 6.2 minutes each is roughly two hours.

SUCCESS, EITHER WAY. Both of these finish the question:

    CONFIRMED       mean gain >= 300 MB, exact one-sided p <= 0.01, and no single null in the
                    series reaches the treatment mean.
    CONFIRMED-NULL  the 95% CI upper bound on the mean gain is below 300 MB. "No keepable
                    effect" is an answer, not a failure to get one.

A result whose interval straddles 300 buys at most TWO extension nights (+8 treatments, +4
nulls each). If it still straddles, the series stops at NO-KEEP by pre-registered default. That
cap is what makes this finishable rather than open forever.

STOPPING EARLY, DECLARED HERE SO IT CANNOT BE DECIDED LATER.

    futility   after 6 treatments, running mean < 100 MB and CI upper bound < 300 -> stop. A
               real 100 MB effect still cannot become a KEEP at a 300 floor, so the remaining
               runs buy no decision.
    efficacy   after >= 4 treatments and >= 8 nulls, complete column separation AND mean
               >= 400 MB -> may stop. The buffer over 300 pays for having looked early.

    NO OTHER PEEKING, and a single run's own KEEP verdict triggers nothing: a null reached
    +196.8 MB on the old instrument, so one run clearing the floor is a coin toss, which is the
    thing the floor exists to refuse.

WHAT IS DELIBERATELY NOT POWERED FOR. The residual the old instrument hinted at was about 63 MB.
Detecting that would need roughly 55 runs per group, and a statistically certain 63 MB is still
not a KEEP at a 300 floor -- runs spent proving a sub-floor effect exists buy no decision.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULT = os.path.join(REPO, "docs", "research", "results", "route_campaign.json")

#: Frozen for the series. Changing any of these starts a different series.
CONFIG = {"max_concurrent": "3", "goals": "saturated-v1", "population": "fleet-edge-tree"}

#: The decision threshold, taken from the frozen judge rather than restated here.
def floor_mb() -> float:
    from relay.selfimprove.route_evaluator import MIN_MEMORY_GAIN_MB
    return float(MIN_MEMORY_GAIN_MB)


#: (kind, order). kind: "sock" = socket-vs-socket null, "tabs" = tabs-vs-tabs null,
#: "tx" = treatment (tabs against socket).
PHASE_A = [("sock", "ctrl"), ("tabs", "ctrl"), ("sock", "cand"), ("tabs", "cand"),
           ("sock", "ctrl"), ("tabs", "ctrl"), ("sock", "cand"), ("tabs", "cand")]

PHASE_B = [("tx", "ctrl"), ("sock", "ctrl"), ("tx", "cand"), ("tabs", "cand"),
           ("tx", "ctrl"), ("sock", "cand"), ("tx", "cand"), ("tabs", "ctrl"),
           ("tx", "ctrl"), ("tx", "cand"), ("tx", "ctrl"), ("tx", "cand")]

#: How long to wait before retrying a cell the preflight refused. One probe interval: retrying
#: sooner just re-reads the same stamp.
REFUSAL_RETRY_S = 600.0

#: How stale the connector-path evidence may be before a run is refused rather than measured.
#: Four probe intervals: one missed probe is ordinary, four in a row is not.
PROBE_STALE_S = 2400.0


def argv_for(kind: str, order: str) -> list:
    """The campaign flags for one cell of the schedule."""
    args = ["--warmup"]
    if kind in ("sock", "tabs"):
        args.append("--null")
    if kind == "sock":
        args.append("--socket-both")
    if order == "cand":
        args.append("--candidate-first")
    return args


def preflight(summary=None) -> str:
    """Empty string if the next run may proceed, else the reason it may not.

    REFUSING IS NOT A MEASUREMENT, and that is the point: the probe stamp is the one signal
    that says the connector path works end to end, and it says the same thing whichever
    transport carried it. Running arms against a lapsed connector measures refusals and records
    them as memory.
    """
    if summary is None:
        try:
            from tools import tool_probe
            summary = tool_probe.get_summary()
        except Exception as exc:
            return "probe summary unreadable: %s" % type(exc).__name__
    age = summary.get("tool_age_s")
    if age is None:
        return "no probe has ever been recorded"
    if float(age) > PROBE_STALE_S:
        return "the last probe is %.0f minutes old" % (float(age) / 60.0)
    if summary.get("tool_ok") is False and summary.get("tool_inbound") is False:
        return "the last probe never reached this server"
    return ""


def classify(rec) -> str:
    """Empty string if this result may join the series, else why it is quarantined."""
    if not isinstance(rec, dict):
        return "no result recorded"
    pop = (rec.get("control") or {}).get("memory_population")
    if pop != CONFIG["population"]:
        # An unscoped run summed every browser on the machine, which is a different quantity.
        # Averaging it in is the exact mistake this series exists to stop repeating.
        return "population was %r" % (pop,)
    if (rec.get("infra") or {}).get("aborted"):
        return "infra abort"
    if rec.get("memory_gain_mb") is None:
        return "no memory_gain_mb"
    if str(rec.get("max_concurrent")) != CONFIG["max_concurrent"]:
        return "max_concurrent was %r" % (rec.get("max_concurrent"),)
    return ""


def mean_ci(values, z=1.96):
    """(mean, half-width) of the 95% interval, or (None, None) when n < 2."""
    n = len(values)
    if n < 2:
        return (values[0] if n else None), None
    m = sum(values) / n
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    return m, z * math.sqrt(var / n)


def verdict(nulls, treatments):
    """The pre-registered reading of the series so far. Never invents a threshold."""
    from relay.selfimprove.run_archive import separation
    floor = floor_mb()
    out = {"n_null": len(nulls), "n_tx": len(treatments), "floor_mb": floor,
           "state": "collecting", "why": ""}
    if len(treatments) < 2 or len(nulls) < 2:
        out["why"] = "too few runs to read"
        return out
    m, half = mean_ci(treatments)
    sep = separation(nulls, treatments)
    out.update({"tx_mean": round(m, 1), "ci_half": round(half, 1) if half else None,
                "ci_upper": round(m + half, 1) if half else None, "p": sep.get("p"),
                "min_p": sep.get("min_p")})
    if half is None:
        return out
    upper = m + half
    if m >= floor and (sep.get("p") or 1.0) <= 0.01 and max(nulls) < m:
        out["state"], out["why"] = "CONFIRMED", "mean over the floor, separated, p<=0.01"
    elif upper < floor:
        out["state"], out["why"] = "CONFIRMED-NULL", "the interval sits entirely below the floor"
    elif len(treatments) >= 6 and m < 100.0 and upper < floor:
        out["state"], out["why"] = "STOP-FUTILITY", "no remaining run can reach the floor"
    else:
        out["why"] = "the interval straddles the floor"
    return out


def run_one(kind: str, order: str, log_dir: str) -> dict:
    """One campaign. Returns the recorded result, or a dict naming why there is none."""
    env = dict(os.environ)
    env["MCP_FLEET_MAX_CONCURRENT"] = CONFIG["max_concurrent"]
    env.setdefault("SWE_DISK_FLOOR_GB", "3")
    env["PYTHONIOENCODING"] = "utf-8"
    log = os.path.join(log_dir, "series_%s_%s_%d.log" % (kind, order, int(time.time())))
    with open(log, "w", encoding="utf-8") as fh:
        proc = subprocess.run(
            [sys.executable, "-u", "scripts/run_route_campaign.py", "transport/v1"]
            + argv_for(kind, order),
            cwd=REPO, env=env, stdout=fh, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        return {"refused": "campaign exited %d" % proc.returncode}
    try:
        with open(RESULT, encoding="utf-8") as fh:
            rec = json.load(fh)
    except Exception as exc:
        return {"refused": "result unreadable: %s" % type(exc).__name__}
    bad = classify(rec)
    return {"refused": bad} if bad else rec


def main(argv=None) -> int:                                     # pragma: no cover
    argv = sys.argv[1:] if argv is None else argv
    phase = PHASE_A if "--phase-a" in argv else (PHASE_A + PHASE_B)
    log_dir = os.environ.get("SERIES_LOG_DIR") or os.path.join(REPO, ".fleet", "series")
    os.makedirs(log_dir, exist_ok=True)
    nulls, txs, refused = [], [], []
    queue = list(phase)
    attempts = 0
    while queue and attempts < len(phase) * 2:
        kind, order = queue.pop(0)
        attempts += 1
        why = preflight()
        if why:
            print("[series] REFUSED %s/%s -- %s" % (kind, order, why), flush=True)
            refused.append((kind, order, why))
            queue.append((kind, order))          # same cell, re-queued: order stays balanced
            time.sleep(REFUSAL_RETRY_S)
            continue
        print("[series] %s %s/%s" % (time.strftime("%H:%M:%S"), kind, order), flush=True)
        rec = run_one(kind, order, log_dir)
        if rec.get("refused"):
            print("[series]   quarantined: %s" % rec["refused"], flush=True)
            refused.append((kind, order, rec["refused"]))
            queue.append((kind, order))
            continue
        gain = float(rec.get("memory_gain_mb"))
        (txs if kind == "tx" else nulls).append(gain)
        print("[series]   gain=%.1f  null=%d tx=%d" % (gain, len(nulls), len(txs)), flush=True)
        v = verdict(nulls, txs)
        if v["state"] in ("CONFIRMED", "CONFIRMED-NULL", "STOP-FUTILITY"):
            print("[series] STOP: %s -- %s" % (v["state"], v["why"]), flush=True)
            break
    print("[series] === report ===", flush=True)
    print(json.dumps(verdict(nulls, txs), ensure_ascii=False, indent=2), flush=True)
    print("[series] nulls: %s" % [round(x, 1) for x in nulls], flush=True)
    print("[series] treatments: %s" % [round(x, 1) for x in txs], flush=True)
    for cell in refused:
        print("[series] refused: %s" % (cell,), flush=True)
    return 0


if __name__ == "__main__":                                      # pragma: no cover
    sys.exit(main())

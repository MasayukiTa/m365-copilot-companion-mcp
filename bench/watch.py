"""watch.py -- one monitoring tick for the full benchmark run. Samples .fleet/status.json
every `interval`s; returns early with an ALERT if no problem has FINISHED for `stall_s`
(possible stall to investigate), else returns a routine progress snapshot after `report_s`.

  python -m bench.watch [--report 900] [--stall 420] [--interval 90]
"""
import argparse
import json
import os
import time


def snap(p):
    d = json.load(open(p, encoding="utf-8"))
    ws = d.get("workers") or []
    done = sum(1 for w in ws if w.get("outcome") == "DONE")
    mx = sum(1 for w in ws if w.get("outcome") == "MAXTURNS")
    stuck = sum(1 for w in ws if w.get("outcome") == "STUCK")
    pend = sum(1 for w in ws if w.get("status") == "pending")
    fin = done + mx + stuck
    act = [w for w in ws if w.get("status") not in ("done", "pending", "maxturns", "stuck")]
    return d, len(ws), fin, done, mx, stuck, pend, act


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=int, default=900)
    ap.add_argument("--stall", type=int, default=420)
    ap.add_argument("--interval", type=int, default=90)
    ap.add_argument("--status", default=".fleet/status.json")
    args = ap.parse_args()
    p = args.status

    d, total, fin0, *_ = snap(p)
    last_fin = fin0
    last_change = time.time()
    t0 = time.time()
    alert = None

    while time.time() - t0 < args.report:
        time.sleep(args.interval)
        try:
            d, total, fin, done, mx, stuck, pend, act = snap(p)
        except Exception as e:
            continue
        if fin > last_fin:
            last_fin = fin
            last_change = time.time()
        if fin >= total:
            alert = "COMPLETE"
            break
        if time.time() - last_change > args.stall:
            alert = "STALL? no finish in %ds" % int(time.time() - last_change)
            break

    d, total, fin, done, mx, stuck, pend, act = snap(p)
    age = time.time() - os.path.getmtime(p)
    a = act[0] if act else None
    aw = ("%s t=%s/%s st=%s %s" % ((a.get("goal") or "").split("HumanEval_")[-1][:4],
                                   a.get("turn"), a.get("max_turns"), a.get("status"),
                                   (a.get("reason") or "")[:24])) if a else "(none)"
    tag = ("[%s] " % alert) if alert else ""
    print("%sfinished=%d/%d (DONE=%d MAXTURNS=%d STUCK=%d) pending=%d | status %.0fs old | "
          "avail_mb=%s tabs=%s | active: %s"
          % (tag, fin, total, done, mx, stuck, pend, age, d.get("avail_mb"),
             d.get("open_tabs"), aw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

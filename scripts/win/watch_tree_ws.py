"""An independent, absolute, read-only witness to the evaluation browser's memory.

WHY THIS EXISTS. The frozen judge reports one number per arm: the largest upward excursion
above that arm's own start, floored at zero. Four nulls -- runs where both arms are identical
and the true answer is zero -- came back +114.8, +9.8, -187.9 and -99.3, and the per-arm detail
said why: the arm that runs FIRST reports about 103 MB more than the arm that runs second, four
times out of four, whichever transport each is. Position, not transport, is the loudest thing
that estimator hears.

Two facts made it impossible to check that from inside. The judge is frozen, so its statistic
cannot be swapped to see whether another one behaves; and its sampler reports a SIGNED DELTA
against a baseline taken per arm, so a run's numbers cannot be re-derived after the fact -- the
baseline they were measured against is gone.

So this samples ABSOLUTE working set, from outside, and writes every sample down. It starts
nothing, kills nothing, attaches to nothing, and sends no input; it reads the process table and
appends a line. A run measured with this alongside can afterwards be scored by ANY statistic --
end, mean, unclipped peak, per-process decomposition -- against the same trajectory the judge
saw, without re-running anything and without touching the frozen file.

Absolute, not delta, is the point. A delta is only meaningful against the baseline it was taken
from, and the baselines are what is under suspicion.

  python scripts/win/watch_tree_ws.py --port 9224 --out .fleet/witness/run.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _tree_pids(root_pid, procs):
    """Every pid at or below root_pid. The browser's cost is spread over its children."""
    kids = {}
    for pid, (ppid, _n, _w) in procs.items():
        kids.setdefault(ppid, []).append(pid)
    seen, stack = set(), [root_pid]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        stack.extend(kids.get(p, []))
    return seen


def snapshot(root_pid):
    """(total MB, [(pid, name, MB), ...]) for the tree, or (None, []) if the root is gone."""
    import psutil
    procs = {}
    for p in psutil.process_iter(["pid", "ppid", "name", "memory_info"]):
        try:
            i = p.info
            mi = i.get("memory_info")
            procs[i["pid"]] = (i.get("ppid") or 0, i.get("name") or "",
                               (mi.rss if mi else 0) / (1024.0 * 1024.0))
        except Exception:
            continue
    if root_pid not in procs:
        return None, []
    rows = [(pid, procs[pid][1], round(procs[pid][2], 1)) for pid in _tree_pids(root_pid, procs)]
    return round(sum(r[2] for r in rows), 1), sorted(rows, key=lambda r: -r[2])


def cdp_owner_pid(port):
    """The pid listening on the CDP port -- the browser process that owns this profile."""
    import psutil
    for c in psutil.net_connections(kind="tcp"):
        if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
            return c.pid
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9224)
    ap.add_argument("--out", default=".fleet/witness/tree_ws.csv")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--detail-every", type=int, default=12,
                    help="write the per-process breakdown once every N samples")
    a = ap.parse_args(argv)

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    detail = os.path.splitext(a.out)[0] + "_procs.csv"
    fresh = not os.path.exists(a.out)

    with open(a.out, "a", newline="", encoding="utf-8") as fh, \
         open(detail, "a", newline="", encoding="utf-8") as dh:
        w, dw = csv.writer(fh), csv.writer(dh)
        if fresh:
            w.writerow(["ts", "root_pid", "n_procs", "total_mb"])
            dw.writerow(["ts", "pid", "name", "mb"])
        n = 0
        while True:
            root = cdp_owner_pid(a.port)
            if root is None:
                # The browser is between rebuilds. Record the gap rather than skipping it:
                # a rebuild is exactly where a run's baseline gets taken.
                w.writerow([round(time.time(), 1), "", 0, ""])
            else:
                total, rows = snapshot(root)
                w.writerow([round(time.time(), 1), root, len(rows), total])
                if n % max(1, a.detail_every) == 0:
                    for pid, name, mb in rows[:12]:
                        dw.writerow([round(time.time(), 1), pid, name, mb])
                    dh.flush()
            fh.flush()
            n += 1
            time.sleep(a.interval)


if __name__ == "__main__":                                      # pragma: no cover
    sys.exit(main() or 0)

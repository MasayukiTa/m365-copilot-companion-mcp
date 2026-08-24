"""Watch the CLIENT side of the transport -- the memory the browser sampler cannot see.

THE HOLE THIS CLOSES. Every memory figure in this experiment measures the process tree owned by
the CDP port: the browser. Tabs work happens inside that tree. Socket work does not, or not all
of it -- the websocket is held by the fleet's own process, and whatever it buffers, decodes and
retains lives outside the boundary being sampled.

That is not a small asymmetry. It is asymmetric BY CONSTRUCTION, and it flatters exactly the arm
the experiment is trying to give credit to. Two estimators that both stop at the browser will
agree with each other while both undercounting the socket route by the same amount, and their
agreement will read as corroboration when it is a shared blind spot.

So this samples the runner and everything under it -- the python process driving the campaign,
its node children, whatever else it spawns -- on the same clock, from outside, in absolute MB.
An arm's true cost is what BOTH sides moved.

It matches by command line rather than by a pid handed in, because the runner is started and
restarted by the diagnostic between runs and a pid captured once would be watching a corpse for
most of the night. A command-line match is self-referential -- the query's own command line
contains the pattern -- so this excludes itself and its ancestors, the same guard find_procs.ps1
carries for the same reason.

  python scripts/win/watch_client_ws.py --pattern run_route_campaign --out .fleet/witness/cli.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time


def _own_lineage(psutil):
    """This process and every parent. Whatever launched us mentions the pattern too."""
    ids, cur = set(), os.getpid()
    for _ in range(12):
        ids.add(cur)
        try:
            cur = psutil.Process(cur).ppid()
        except Exception:
            break
        if not cur:
            break
    return ids


def sample(pattern, mine, psutil):
    """(total MB, n_procs, roots) for every process matching pattern, plus its descendants."""
    procs = {}
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline", "memory_info"]):
        try:
            i = p.info
            mi = i.get("memory_info")
            procs[i["pid"]] = (i.get("ppid") or 0, i.get("name") or "",
                               " ".join(i.get("cmdline") or []),
                               (mi.rss if mi else 0) / (1024.0 * 1024.0))
        except Exception:
            continue
    roots = [pid for pid, (_pp, _n, cmd, _m) in procs.items()
             if pattern in cmd and pid not in mine]
    if not roots:
        return None, 0, []
    kids = {}
    for pid, (ppid, _n, _c, _m) in procs.items():
        kids.setdefault(ppid, []).append(pid)
    seen, stack = set(), list(roots)
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(kids.get(pid, []))
    return round(sum(procs[p][3] for p in seen), 1), len(seen), roots


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", default="run_route_campaign")
    ap.add_argument("--out", default=".fleet/witness/client_ws.csv")
    ap.add_argument("--interval", type=float, default=3.0)
    a = ap.parse_args(argv)

    import psutil
    mine = _own_lineage(psutil)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    fresh = not os.path.exists(a.out)
    with open(a.out, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if fresh:
            w.writerow(["ts", "n_procs", "total_mb"])
        while True:
            total, n, _roots = sample(a.pattern, mine, psutil)
            # Between runs there is no runner. Record the gap; a blank is not a zero.
            w.writerow([round(time.time(), 1), n, "" if total is None else total])
            fh.flush()
            time.sleep(a.interval)


if __name__ == "__main__":                                      # pragma: no cover
    sys.exit(main() or 0)

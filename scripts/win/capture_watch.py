"""Watch what the capture actually costs, while the fleet runs.

WHAT THIS ANSWERS THAT THE CHECKPOINT DOES NOT. The checkpoint says whether an invariant holds
right now. This says how often a page opens and how far the browser rises when one does --
which is the whole question about the tiering, and it is a RATE, so a single reading cannot
give it.

Both halves matter and neither alone is enough. A cheap page opened constantly is not cheap:
the run that exposed the refresh spin was capturing 3.8 times a minute, and every individual
capture looked ordinary.

    python scripts/win/capture_watch.py --minutes 30
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

_PS = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
       "Select-Object CommandLine, WorkingSetSize | ConvertTo-Json -Compress -Depth 3")


def rss_mb(profile="copilot-companion-edge"):
    """Resident PRIVATE memory of the managed browser, in MB.

    NOT the sum of WorkingSetSize, which is what this was and which over-reported by 2.4 to
    2.9 times: that counter includes SHARED pages, and a Chromium browser is fifteen
    processes sharing one binary. The corrected figure matches what Task Manager shows for
    the same browser -- 122 MB where this used to say 295.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from edge_memory import private_mb
        return private_mb(profile)
    except Exception:
        return None


def copilot_pages(port=9222):
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=3) as fh:
            return sum(1 for t in json.load(fh)
                       if t.get("type") == "page" and "m365.cloud.microsoft" in (t.get("url") or ""))
    except Exception:
        return None


def newest_log():
    logs = glob.glob(os.path.join(REPO, ".fleet", "coordinator_*.log"))
    return max(logs, key=os.path.getmtime) if logs else None


def tier_counts(path):
    """Captures and tiers so far in this run. Read from the LOG rather than counted here, so a
    watcher started late still sees the whole run -- the same lesson the tab audit learned when
    a restart made it report an ordinary fallback as the one thing that must never happen."""
    if not path:
        return {}
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return {}
    return {"captures": len(re.findall(r"captured: [0-9]+ min", txt)),
            "tier1": txt.count("tier 1"), "tier2": txt.count("tier 2"),
            "tier3": txt.count("tier 3"),
            "token_min": [int(m) for m in re.findall(r"captured: ([0-9]+) min", txt)]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--out", default=os.path.join(REPO, ".fleet", "capture_watch.log"))
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    log = open(args.out, "a", encoding="utf-8", buffering=1)

    def say(line):
        stamped = time.strftime("%H:%M:%S") + "  " + line
        print(stamped, flush=True)
        log.write(stamped + chr(10))

    started = time.time()
    path = newest_log()
    base = tier_counts(path)
    say("start  log=%s  captures already=%s" % (os.path.basename(path or "-"),
                                                base.get("captures")))
    peak, samples, page_seen = 0.0, [], 0
    while time.time() - started < args.minutes * 60:
        time.sleep(args.interval)
        mb, pages = rss_mb(), copilot_pages()
        if mb is not None:
            samples.append(mb)
            peak = max(peak, mb)
        if pages:
            page_seen += 1
        now = tier_counts(newest_log())
        mins = (time.time() - started) / 60.0
        new_caps = (now.get("captures") or 0) - (base.get("captures") or 0)
        say("rss=%-7s peak=%-7s copilot_pages=%-4s captures=+%-3d (%.2f/min) "
            "tiers 1/2/3 = %s/%s/%s  token_min=%s"
            % (mb, peak, pages, new_caps, new_caps / mins if mins else 0,
               now.get("tier1"), now.get("tier2"), now.get("tier3"),
               (now.get("token_min") or [])[-3:]))
    med = sorted(samples)[len(samples) // 2] if samples else None
    say("stop   median rss=%s peak=%s  a copilot page was open in %d of %d samples"
        % (med, peak, page_seen, len(samples)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""One screen answering the questions that have to be true, every time, before and during a run.

WHY THIS EXISTS. The monitor already caught the leaked Copilot page -- it wrote a breach at
22:40 and the breach stayed open for nine and a half hours while runs were launched on top of
it. The detector worked; nobody read it. So the failure was not a missing measurement, it was
a measurement nobody was obliged to look at.

This is that obligation in one command: the four things that must hold, each with the number
behind it, and a single verdict line at the end so a glance is enough.

    python scripts/win/checkpoint.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROUTE_LOG = os.path.join(REPO, ".fleet", "socket_route.jsonl")
AUDIT_LOG = os.path.join(REPO, ".fleet", "tab_audit.log")
MANAGED = ("copilot-companion-edge", "copilot-bridge-edge", "copilot-eval-edge")


def _ps(script, timeout=40):
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", script],
                              capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def pages(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=3) as fh:
            return [t.get("url", "") for t in json.load(fh) if t.get("type") == "page"]
    except Exception:
        return None


def edge_state():
    """Per managed profile: (browser processes, headed?, total MB)."""
    raw = _ps("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
              "Select-Object CommandLine, WorkingSetSize | ConvertTo-Json -Compress -Depth 3")
    try:
        data = json.loads(raw) if raw.strip() else []
    except ValueError:
        return {}
    if isinstance(data, dict):
        data = [data]
    out = {}
    for p in data:
        cmd = p.get("CommandLine") or ""
        for prof in MANAGED:
            if prof in cmd:
                rec = out.setdefault(prof, {"mb": 0, "headed": False})
                rec["mb"] += (p.get("WorkingSetSize") or 0) / 1048576.0
                if "--type=" not in cmd and "--headless" not in cmd:
                    rec["headed"] = True
    return out


def route_since(iso):
    done = tab = fell = faults = 0
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
                ev = o.get("event")
                if ev == "worker_done":
                    done += 1
                    if o.get("route") == "tab":
                        tab += 1
                        if o.get("fell_back"):
                            fell += 1
                elif ev in ("fallback", "socket_retry", "route_closed"):
                    faults += 1
    except OSError:
        pass
    return done, tab, fell, faults


def run_state():
    try:
        with open(os.path.join(REPO, ".fleet", "status.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d
    except Exception:
        return {}


def main():
    today = time.strftime("%Y-%m-%d")
    print("checkpoint %s" % time.strftime("%H:%M:%S"))

    verdicts = []

    # 1. No browser owns a window.
    edge = edge_state()
    headed = [p for p, r in edge.items() if r["headed"]]
    verdicts.append(("no browser window", not headed,
                     "headed: %s" % (", ".join(headed) if headed else "none")))

    # 2. No Copilot page is open that nobody is using.
    d = run_state()
    running = bool(d.get("running"))
    open_pages = {}
    for port, name in ((9222, "companion"), (9223, "bridge"), (9224, "eval")):
        urls = pages(port)
        if urls is None:
            continue
        open_pages[name] = [u for u in urls if "m365.cloud.microsoft" in u]
    stray = {k: v for k, v in open_pages.items() if v}
    # During a run a capture page is expected and lives ~30-45s; idle, none should exist.
    verdicts.append(("no idle Copilot page", running or not stray,
                     "copilot pages: %s (run in flight: %s)"
                     % ({k: len(v) for k, v in stray.items()} or "none", running)))

    # 3. Every tab that carried work today was a fallback.
    done, tab, fell, faults = route_since(today)
    verdicts.append(("every tab was a fallback", tab == fell,
                     "today: worker_done=%d socket=%d tab=%d (fell_back=%d) route faults=%d"
                     % (done, done - tab, tab, fell, faults)))

    # 4. The audit has flagged nothing since it started.
    # SINCE THE AUDIT LAST STARTED, not since the file was created. The log is appended
    # across restarts, so counting the whole file makes an old finding -- one already
    # explained and acted on -- reappear as today's alarm for ever. A check that cannot go
    # green again is a check people learn to ignore.
    residue = 0
    tabwork = 0
    try:
        with open(AUDIT_LOG, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        last_start = max((i for i, l in enumerate(lines) if " start " in l), default=-1)
        for line in lines[last_start + 1:]:
            if " RESIDUE " in line:
                residue += 1
            if " TAB WORK " in line:
                tabwork += 1
    except OSError:
        pass
    verdicts.append(("tab audit clean", residue == 0 and tabwork == 0,
                     "RESIDUE=%d  TAB-WORK-without-fault=%d" % (residue, tabwork)))

    for name, ok, detail in verdicts:
        print("  [%s] %-26s %s" % ("ok" if ok else "XX", name, detail))

    mb = {p: round(r["mb"]) for p, r in sorted(edge.items())}
    print("  memory: %s   run: total=%s done=%s queued=%s"
          % (mb, d.get("total"), d.get("done_count"), d.get("queued")))

    bad = [n for n, ok, _ in verdicts if not ok]
    print("VERDICT: %s" % ("all clear" if not bad else "ATTENTION -- " + ", ".join(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

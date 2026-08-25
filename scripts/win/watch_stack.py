"""What the whole stack is actually costing, in one screen, sampled over time.

WHY THIS EXISTS. Every wrong conclusion drawn on 2026-08-25 came from measuring one part and
naming the whole: a browser page count read against one run's status file while four runs
shared it; a memory climb blamed on the fleet when it was the MCP server; a "17-fold"
improvement that was a peak over 22 minutes compared with a peak over 10. The instrument has
to show the parts side by side, on one clock, or the reader supplies the missing half from
imagination.

WHAT IT SHOWS, and why each column is here rather than a nearby one:

  * MCP server RSS -- the tool server every worker calls. It is not part of the fleet and does
    not appear in any fleet status file, which is exactly why it went unnoticed while it grew
    past 8 GB and pushed free RAM under the fleet's own recycle floor.
  * Edge RSS AND page count, per profile -- the companion (fleet), the bridge and the eval
    browser are three separate profiles that look identical in Task Manager. Pages come from
    CDP, so the number is the browser's, not a fleet counter's opinion of it.
  * One row per fleet run, found by scanning for fleet_runner processes rather than by
    reading a status file. Concurrent runs are normal here and a fixed path cannot see them.
  * Socket route events since the sampler started -- reconnects, fallbacks, closures. A run
    that is quietly back on tabs looks healthy in every other column.

Read-only. It starts nothing, kills nothing, and touches no file but its own output.

    python scripts/win/watch_stack.py                 # live, until Ctrl-C
    python scripts/win/watch_stack.py --interval 10 --csv out.csv
    python scripts/win/watch_stack.py --once          # one sample, for a script to read
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROUTE_LOG = os.path.join(REPO, ".fleet", "socket_route.jsonl")

#: CDP ports and what lives on them. Kept here rather than discovered so an unexpected
#: browser shows up as an unnamed profile instead of being silently folded into a known one.
PORTS = {9222: "companion", 9223: "bridge", 9224: "eval"}

_PS_PROCS = r"""
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='msedge.exe'" |
  Select-Object ProcessId, Name, WorkingSetSize, CommandLine | ConvertTo-Json -Compress -Depth 3
"""


def _powershell(script: str) -> str:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=30)
        return out.stdout or ""
    except Exception:
        return ""


def _free_mb() -> float:
    raw = _powershell("(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory").strip()
    try:
        return float(raw) / 1024.0
    except ValueError:
        return -1.0


def processes() -> list:
    raw = _powershell(_PS_PROCS).strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


def _profile_of(cmdline: str) -> str:
    m = re.search(r"--user-data-dir=\"?([^\"\s]+)", cmdline or "")
    if not m:
        return "(default)"
    leaf = m.group(1).rstrip("\\/").replace("\\", "/").rsplit("/", 1)[-1]
    return leaf or "(default)"


def cdp_pages(port: int):
    """Page count straight from the browser. None when it cannot be reached."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=4) as fh:
            targets = json.load(fh)
        return sum(1 for t in targets if t.get("type") == "page")
    except Exception:
        return None


def fleet_runs(procs) -> list:
    """One entry per fleet run, from the process table. Status is read from the run's OWN
    state dir -- the shared default is written by whichever run swept last, so a fixed path
    reports one run's numbers under every run's name."""
    runs = []
    for p in procs:
        cmd = p.get("CommandLine") or ""
        if "fleet_runner" not in cmd or "python.exe" not in (p.get("Name") or ""):
            continue
        if "\\.venv\\" in cmd:
            continue                      # the wrapper; the re-exec'd child is the real one
        m = re.search(r"--goals-file\s+(\S+)", cmd)
        sd = re.search(r"--state-dir\s+(\S+)", cmd)
        state_dir = sd.group(1) if sd else os.path.join(REPO, ".fleet")
        status = {}
        try:
            with open(os.path.join(state_dir, "status.json"), encoding="utf-8") as fh:
                status = json.load(fh)
        except Exception:
            pass
        runs.append({
            "pid": p.get("ProcessId"),
            "goals": os.path.basename(m.group(1)) if m else "?",
            "state_dir": os.path.basename(state_dir.rstrip("\\/")) or ".fleet",
            "rss_mb": round((p.get("WorkingSetSize") or 0) / 1048576.0, 1),
            "done": status.get("done_count"),
            "total": status.get("total"),
            "max_conc": status.get("max_concurrent"),
            "stale_s": (round(time.time() - status["updated"]) if status.get("updated")
                        else None),
        })
    return runs


def route_events(since_iso: str) -> dict:
    counts = {"socket_retry": 0, "fallback": 0, "route_closed": 0, "worker_done": 0}
    tabs = 0
    try:
        with open(ROUTE_LOG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if (o.get("at") or "") < since_iso:
                    continue
                ev = o.get("event")
                if ev in counts:
                    counts[ev] += 1
                if ev == "worker_done" and o.get("route") == "tab":
                    tabs += 1
    except OSError:
        pass
    counts["worker_done_on_tab"] = tabs
    return counts


def sample(since_iso: str) -> dict:
    procs = processes()
    mcp = sum((p.get("WorkingSetSize") or 0) for p in procs
              if "main.py" in (p.get("CommandLine") or ""))
    edge: dict = {}
    for p in procs:
        if (p.get("Name") or "") != "msedge.exe":
            continue
        prof = _profile_of(p.get("CommandLine") or "")
        edge[prof] = edge.get(prof, 0) + (p.get("WorkingSetSize") or 0)
    return {
        "t": time.strftime("%H:%M:%S"),
        "free_mb": round(_free_mb()),
        "mcp_mb": round(mcp / 1048576.0),
        "edge_mb": {k: round(v / 1048576.0) for k, v in sorted(edge.items())},
        "pages": {name: cdp_pages(port) for port, name in PORTS.items()},
        "runs": fleet_runs(procs),
        "route": route_events(since_iso),
    }


def render(s: dict, first: dict, elapsed_min: float) -> str:
    lines = []
    drift = s["mcp_mb"] - first["mcp_mb"]
    rate = ("  (%+.0f MB/min)" % (drift / elapsed_min)) if elapsed_min >= 1 else ""
    lines.append("%s   free %5d MB   MCP server %5d MB%s"
                 % (s["t"], s["free_mb"], s["mcp_mb"], rate))
    for name, port in (("companion", 9222), ("bridge", 9223), ("eval", 9224)):
        mb = next((v for k, v in s["edge_mb"].items() if name in k), None)
        pg = s["pages"].get(name)
        if mb is None and pg is None:
            continue
        lines.append("    edge %-10s %5s MB   pages %s"
                     % (name, mb if mb is not None else "?",
                        pg if pg is not None else "unreachable"))
    for r in s["runs"]:
        stale = "" if (r["stale_s"] or 0) < 60 else "  STATUS STALE %ss" % r["stale_s"]
        lines.append("    run  %-22s %5.0f MB   %s/%s  cap %s  [%s]%s"
                     % (r["goals"][:22], r["rss_mb"], r["done"], r["total"],
                        r["max_conc"], r["state_dir"], stale))
    if not s["runs"]:
        lines.append("    run  (none)")
    rt = s["route"]
    lines.append("    route  done %d (on tab %d)   reconnect %d   fallback %d   closed %d"
                 % (rt["worker_done"], rt["worker_done_on_tab"], rt["socket_retry"],
                    rt["fallback"], rt["route_closed"]))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--interval", type=float, default=20.0, help="seconds between samples")
    ap.add_argument("--once", action="store_true", help="one sample as JSON, then exit")
    ap.add_argument("--csv", help="also append a flat row per sample to this file")
    args = ap.parse_args(argv)

    since = time.strftime("%Y-%m-%dT%H:%M:%S")
    if args.once:
        print(json.dumps(sample(since), ensure_ascii=False, indent=1))
        return 0

    started = time.time()
    first = None
    csv = None
    if args.csv:
        new = not os.path.exists(args.csv)
        csv = open(args.csv, "a", encoding="utf-8")
        if new:
            csv.write("time,free_mb,mcp_mb,companion_mb,companion_pages,runs,"
                      "reconnect,fallback,closed,done_on_tab\n")
    print("watching every %.0fs -- Ctrl-C to stop" % args.interval)
    try:
        while True:
            s = sample(since)
            first = first or s
            print(render(s, first, max((time.time() - started) / 60.0, 0.001)), flush=True)
            if csv:
                comp = next((v for k, v in s["edge_mb"].items() if "companion" in k), "")
                csv.write("%s,%s,%s,%s,%s,%d,%d,%d,%d,%d\n"
                          % (s["t"], s["free_mb"], s["mcp_mb"], comp,
                             s["pages"].get("companion"), len(s["runs"]),
                             s["route"]["socket_retry"], s["route"]["fallback"],
                             s["route"]["route_closed"], s["route"]["worker_done_on_tab"]))
                csv.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("stopped")
    finally:
        if csv:
            csv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

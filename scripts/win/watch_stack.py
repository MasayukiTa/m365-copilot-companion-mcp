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

#: The profiles this stack owns. Everything else on the machine -- above all the user's own
#: Edge, which routinely holds several GB of their real browsing -- is somebody else's and is
#: neither measured against these ceilings nor ever touched. The first run of the guard
#: charged the user's personal browser to the stack and reported a breach for it.
MANAGED_PROFILES = ("copilot-companion-edge", "copilot-bridge-edge", "copilot-eval-edge")

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
    urls = cdp_page_urls(port)
    return None if urls is None else len(urls)


def cdp_page_urls(port: int):
    """The URLs of the open pages, or None when the browser cannot be reached.

    The count alone cannot tell an `about:blank` keep-alive -- a few MB, holding the browser
    open because Edge exits with its last page -- from a Copilot chat page nobody is reading,
    which measured 135 MB while idle. A guard that watches the number would have called both
    of those "1 page" and reported nothing wrong.
    """
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=4) as fh:
            targets = json.load(fh)
        return [t.get("url", "") for t in targets if t.get("type") == "page"]
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


def headed_profiles(procs) -> list:
    """Managed profiles whose BROWSER process is running without --headless.

    Not a cosmetic detail. A headed browser owns a real window, and the socket route opens a
    tab every time it re-keys its token -- which is often, because the tokens observed on
    2026-08-26 lived 16 to 67 minutes. Creating a tab in a headed browser raises that window,
    so from the desk it reads as a Copilot window flashing over whatever the user is doing,
    every few dozen minutes, unprompted. One sign-in left a browser headed for ten hours
    doing exactly that, and no instrument here noticed, because none of them looked.

    --type= excludes renderer and GPU children: only the browser process carries the flags.
    """
    out = []
    for p in procs:
        cmd = p.get("CommandLine") or ""
        if (p.get("Name") or "") != "msedge.exe" or "--type=" in cmd:
            continue
        prof = _profile_of(cmd)
        if prof not in MANAGED_PROFILES or "--headless" in cmd:
            continue
        out.append(prof)
    return sorted(set(out))


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
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "free_mb": round(_free_mb()),
        "mcp_mb": round(mcp / 1048576.0),
        "edge_mb": {k: round(v / 1048576.0) for k, v in sorted(edge.items())},
        "pages": {name: cdp_pages(port) for port, name in PORTS.items()},
        "page_urls": {name: cdp_page_urls(port) for port, name in PORTS.items()},
        "headed": headed_profiles(procs),
        "runs": fleet_runs(procs),
        "route": route_events(since_iso),
    }


#: What the stack is supposed to look like. Each is a thing that was believed to be true
#: while it was not, and that no instrument was checking.
MCP_MB_CEILING = int(os.environ.get("STACK_GUARD_MCP_MB", "3000"))
EDGE_MB_CEILING = int(os.environ.get("STACK_GUARD_EDGE_MB", "2500"))
FREE_MB_FLOOR = int(os.environ.get("STACK_GUARD_FREE_MB", "1200"))


def violations(s: dict) -> list:
    """What is wrong with this sample. Empty means the invariants held.

    Separated from rendering on purpose. A watcher that only draws a screen is read while
    somebody is watching it and proves nothing about the hours nobody was -- which is how a
    browser sat headed for ten hours, and how a Copilot page nobody was reading held 135 MB
    through an afternoon of measurements that never mentioned it.
    """
    out = []
    for prof in s.get("headed") or []:
        out.append(("HEADED", "%s owns a real window -- a tab opened in it will surface"
                    % prof))

    idle = not s.get("runs")
    for name, pages in (s.get("page_urls") or {}).items():
        if pages is None:                      # browser down; not a violation
            continue
        copilot = [u for u in pages if "m365.cloud.microsoft" in u]
        if copilot and idle:
            out.append(("IDLE_PAGE", "%s holds %d Copilot page(s) with no run in flight: %s"
                        % (name, len(copilot), copilot[0][:70])))

    if s["mcp_mb"] > MCP_MB_CEILING:
        out.append(("MCP_MEM", "MCP server at %d MB (ceiling %d)"
                    % (s["mcp_mb"], MCP_MB_CEILING)))
    total_edge = sum(v for k, v in (s.get("edge_mb") or {}).items()
                     if k in MANAGED_PROFILES)
    if total_edge > EDGE_MB_CEILING:
        out.append(("EDGE_MEM", "managed Edge total %d MB (ceiling %d)"
                    % (total_edge, EDGE_MB_CEILING)))
    if s["free_mb"] < FREE_MB_FLOOR:
        out.append(("FREE_RAM", "free RAM %d MB (floor %d)" % (s["free_mb"], FREE_MB_FLOOR)))
    return out


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


def _release_idle_fleet_edge(say):
    """Close the fleet browser when nothing is using it. Off unless --release-idle-edge.

    OFF BY DEFAULT, AND THAT IS THE DESIGN, not caution about an unfinished feature. This
    kills a browser other processes may be about to use, and the decision rests on reading
    a status file and a process list correctly. relay/idle_edge.py refuses on every kind of
    doubt -- but a guard that quietly starts closing browsers because it was left running
    is a surprise nobody asked for. Somebody turns this on.

    Measured 2026-08-28: the fleet Edge held 273 MB for one about:blank with no run in
    flight. The bridge's is NOT touched -- see relay/idle_edge.py for why that one stays.
    """
    try:
        from relay.idle_edge import FLEET_CDP_PORT, may_release
    except Exception as exc:
        say("idle-release unavailable (%s)" % type(exc).__name__)
        return
    try:
        status = None
        path = os.path.join(".fleet", "status.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                status = json.load(fh)
        pages = _page_count(FLEET_CDP_PORT)
        ok, why = may_release(status, pages=pages)
        if not ok:
            return                      # quiet: the common case is 'a run is in flight'
        say("releasing the idle fleet browser on :%d (%s)" % (FLEET_CDP_PORT, why))
        _kill_edge_on(FLEET_CDP_PORT, say)
    except Exception as exc:
        say("idle-release skipped: %s: %s" % (type(exc).__name__, str(exc)[:120]))


def _page_count(port):
    """Open pages on that CDP port, or None when it cannot be asked."""
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port,
                                    timeout=3) as fh:
            return len([t for t in json.load(fh) if t.get("type") == "page"])
    except Exception:
        return None


def _kill_edge_on(port, say):
    """End the browser holding that CDP port, and nothing else.

    Not edge_recover.hard_reset: that one relaunches, which is right for recovery and
    exactly wrong here -- the point is to stop paying for a browser nobody is using.

    The process tree is identified from the port on its own command line, so a browser
    started for another purpose cannot be caught by a name match.
    """
    try:
        import psutil
    except Exception:
        say("psutil missing; not releasing anything")
        return
    roots = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if "msedge" not in (proc.info.get("name") or "").lower():
                continue
            cmd = " ".join(proc.info.get("cmdline") or [])
            if "--remote-debugging-port=%d" % port not in cmd:
                continue
            if "--type=" in cmd:
                continue                # a child; killing the root takes the tree
            roots.append(psutil.Process(proc.info["pid"]))
        except Exception:
            continue
    if not roots:
        say("no browser found on :%d" % port)
        return
    for root in roots:
        try:
            kids = root.children(recursive=True)
        except Exception:
            kids = []
        for proc in kids + [root]:
            try:
                proc.terminate()
            except Exception:
                pass
    say("released %d browser tree(s) on :%d" % (len(roots), port))


def guard(log_path: str, interval: float, since: str, release_idle_edge: bool = False) -> int:
    """Sample forever, record everything, and say something only when an invariant breaks.

    Two files, because they answer different questions. The .jsonl is the record -- every
    sample, so a number can be checked afterwards against the window it was measured over
    rather than against a memory of it. The .violations.log is the alarm: short, timestamped,
    and empty when nothing is wrong, so that its being empty is itself evidence.

    A breach is reported when it STARTS and when it ENDS, not on every sample. A guard that
    prints the same line every twenty seconds for an hour is one nobody reads to the bottom
    of, and the line that mattered is the first one.
    """
    samples = open(log_path if log_path.endswith(".jsonl") else log_path + ".jsonl",
                   "a", encoding="utf-8")
    base = log_path[:-6] if log_path.endswith(".jsonl") else log_path
    alarms = open(base + ".violations.log", "a", encoding="utf-8")

    def say(line):
        stamped = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), line)
        print(stamped, flush=True)
        alarms.write(stamped + "\n")
        alarms.flush()

    say("guard start (interval %.0fs, pid %d)" % (interval, os.getpid()))
    open_breaches = {}
    try:
        while True:
            s = sample(since)
            vs = dict(violations(s))
            samples.write(json.dumps(s, ensure_ascii=False) + "\n")
            samples.flush()
            for kind, detail in vs.items():
                if kind not in open_breaches:
                    say("BREACH %-10s %s" % (kind, detail))
                open_breaches[kind] = detail
            for kind in [k for k in open_breaches if k not in vs]:
                say("CLEARED %-9s (was: %s)" % (kind, open_breaches.pop(kind)))
            if release_idle_edge:
                _release_idle_fleet_edge(say)
            time.sleep(interval)
    except KeyboardInterrupt:
        say("guard stopped")
    finally:
        samples.close()
        alarms.close()
    return 0


def summarize(log_path: str) -> int:
    """Read a guard log back: what it cost, and what broke, over the window it covers.

    Reports the WINDOW alongside every number. A peak means nothing without the span it was
    a peak over -- comparing a peak across 22 minutes with one across 10 is how a 41%
    improvement was once reported as seventeen-fold.
    """
    path = log_path if log_path.endswith(".jsonl") else log_path + ".jsonl"
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError as exc:
        print("cannot read %s: %s" % (path, exc))
        return 1
    if not rows:
        print("%s holds no samples." % path)
        return 1

    span_min = max(len(rows) - 1, 1) * 0.0
    first, last = rows[0], rows[-1]
    try:
        t0 = time.mktime(time.strptime(first.get("iso", ""), "%Y-%m-%dT%H:%M:%S"))
        t1 = time.mktime(time.strptime(last.get("iso", ""), "%Y-%m-%dT%H:%M:%S"))
        span_min = max((t1 - t0) / 60.0, 0.001)
    except (ValueError, TypeError):
        span_min = 0.0

    print("%d samples over %.1f min  (%s .. %s)"
          % (len(rows), span_min, first.get("iso", "?"), last.get("iso", "?")))
    mcp = [r.get("mcp_mb", 0) for r in rows]
    edge = [sum(v for k, v in (r.get("edge_mb") or {}).items() if k in MANAGED_PROFILES)
            for r in rows]
    free = [r.get("free_mb", 0) for r in rows]
    print("  MCP server   first %5d  peak %5d  last %5d MB%s"
          % (mcp[0], max(mcp), mcp[-1],
             ("   (%+.1f MB/min)" % ((mcp[-1] - mcp[0]) / span_min)) if span_min else ""))
    print("  managed Edge first %5d  peak %5d  last %5d MB" % (edge[0], max(edge), edge[-1]))
    print("  free RAM     first %5d  low  %5d  last %5d MB" % (free[0], min(free), free[-1]))

    headed = [r for r in rows if r.get("headed")]
    print("  samples with a browser WINDOW: %d of %d%s"
          % (len(headed), len(rows),
             ("   -> %s" % sorted({p for r in headed for p in r["headed"]})) if headed else ""))
    idle_pages = sum(1 for r in rows if not r.get("runs") and any(
        any("m365.cloud.microsoft" in u for u in (pg or []))
        for pg in (r.get("page_urls") or {}).values()))
    print("  samples with an idle Copilot page: %d of %d" % (idle_pages, len(rows)))

    breaches = os.path.splitext(path)[0]
    breaches = (breaches[:-6] if breaches.endswith(".jsonl") else breaches) + ".violations.log"
    if os.path.exists(breaches):
        lines = [l.rstrip() for l in open(breaches, encoding="utf-8") if "BREACH" in l]
        print("  breaches recorded: %d" % len(lines))
        for l in lines[-12:]:
            print("    %s" % l)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--interval", type=float, default=20.0, help="seconds between samples")
    # A MEASUREMENT ENDS. Three sampling runs started on 2026-08-25 and 2026-08-26 for
    # one-off comparisons were still sampling two and a half days later, each appending to
    # its own CSV, because the loop below had no exit but Ctrl-C and nobody was at the
    # keyboard. Eight processes, three files still growing, and the question they were
    # launched to answer had been answered on the day.
    #
    # Two hours is longer than any comparison run here has needed and short enough that a
    # forgotten one is gone by morning. --minutes 0 is unbounded, for somebody who means it.
    ap.add_argument("--release-idle-edge", action="store_true",
                    help="close the fleet browser (:9222 only) when no run is using it. "
                         "Off by default: it ends a process other runs may be about to "
                         "use, and the bridge browser is never touched.")
    ap.add_argument("--minutes", type=float, default=120.0,
                    help="stop after this long (0 = run until stopped). A --guard is "
                         "unbounded by default: it is meant to stand.")
    ap.add_argument("--once", action="store_true", help="one sample as JSON, then exit")
    ap.add_argument("--csv", help="also append a flat row per sample to this file")
    ap.add_argument("--guard", metavar="LOG",
                    help="run unattended: append every sample to LOG.jsonl and every "
                         "invariant breach to LOG.violations.log. Prints only breaches, so "
                         "silence means the stack held.")
    ap.add_argument("--summary", metavar="LOG",
                    help="read back a --guard log: peaks, rates, and every breach, with "
                         "the window they were measured over")
    args = ap.parse_args(argv)

    if args.summary:
        return summarize(args.summary)

    since = time.strftime("%Y-%m-%dT%H:%M:%S")
    if args.once:
        s = sample(since)
        s["violations"] = [{"kind": k, "detail": d} for k, d in violations(s)]
        print(json.dumps(s, ensure_ascii=False, indent=1))
        return 0

    if args.guard:
        # A GUARD IS MEANT TO STAND, so it is not bounded unless somebody asks. The bound
        # exists for the sampling mode, which is launched to answer one question.
        return guard(args.guard, args.interval, since, args.release_idle_edge)

    started = time.time()
    first = None
    csv = None
    if args.csv:
        new = not os.path.exists(args.csv)
        csv = open(args.csv, "a", encoding="utf-8")
        if new:
            csv.write("time,free_mb,mcp_mb,companion_mb,companion_pages,runs,"
                      "reconnect,fallback,closed,done_on_tab\n")
    deadline = (started + args.minutes * 60) if args.minutes > 0 else None
    print("watching every %.0fs%s" % (args.interval,
          " for %.0f min" % args.minutes if deadline else " until stopped"))
    try:
        while deadline is None or time.time() < deadline:
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
    else:
        if deadline is not None:
            print("stopped: the %.0f minute window ended" % args.minutes)
    finally:
        if csv:
            csv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Everything the cockpit shows, and the things it cannot, in one command.

WHY THIS EXISTS. On 2026-09-16 the operator's settings panel and the fleet disagreed about
a number for a month, and neither side could see it. The panel showed the file it wrote; the
fleet read a different file at the same absolute path. Finding that took most of a day, and
most of that day was spent because there was no way to ask the machine what it thought was
true -- only the GUI, which is one observer with one view.

So the rules this tool is built on, each of them a mistake made that day:

  IT SAYS WHICH CONTEXT IT IS IN, FIRST. A process launched from an agent session runs
  inside the Claude Desktop package and sees a redirected %APPDATA%. The same command run
  from an ordinary shell sees a different file. A status report that does not say which it
  is can be read as authoritative when it is describing somebody else's machine.

  IT NEVER COUNTS ITSELF. Querying processes by command line matches the querying process,
  because the pattern is in its own command line. That produced a "supervisor crash-looping
  every 12 seconds" that was entirely this tool's ancestors looking at themselves.

  IT SEPARATES "NO" FROM "I DO NOT KNOW". A dot that can only be green or red will eventually
  be confidently wrong. Every line here is one of OK / BAD / UNKNOWN, and UNKNOWN says what
  was missing rather than defaulting to either.

  IT NAMES THE EVIDENCE. Not "server ok" but the status code, the latency, and the commit the
  server says it is running. A verdict without its evidence cannot be checked by the next
  person, which is how "fixed" survives a fix that did not work.

Run it from anywhere:

    .venv\\Scripts\\python.exe scripts\\status.py
    .venv\\Scripts\\python.exe scripts\\status.py --json      (machine-readable)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OK, BAD, UNK = "OK", "BAD", "??"

class Report(object):
    """Collects lines so the same run can print text or JSON without computing twice."""

    def __init__(self):
        self.sections = []

    def section(self, title):
        self.sections.append({"title": title, "rows": []})
        return self

    def row(self, verdict, name, detail):
        # A REPORT THAT CRASHES WHILE REPORTING IS THE WORST OUTCOME HERE. A row added before
        # any section opened used to raise IndexError, so a tool whose entire purpose is to
        # say what is true would die mid-sentence and say nothing at all.
        if not self.sections:
            self.section("(uncategorised)")
        self.sections[-1]["rows"].append({"verdict": verdict, "name": name,
                                          "detail": str(detail)})

    def to_text(self):
        out = []
        for sec in self.sections:
            out.append("")
            out.append("== %s " % sec["title"] + "=" * max(0, 62 - len(sec["title"])))
            for r in sec["rows"]:
                out.append("  [%s] %-22s %s" % (r["verdict"], r["name"], r["detail"]))
        return "\n".join(out).lstrip("\n")


# --------------------------------------------------------------------------- primitives
def http(url, timeout=4.0):
    """(status, body, ms) or (None, reason, ms). Proxy bypassed for loopback, as the cockpit
    does -- a corporate proxy in front of 127.0.0.1 would otherwise make a healthy server
    look dead, which is a different fault reported as this one."""
    started = time.time()
    try:
        if "127.0.0.1" in url or "localhost" in url:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            opener = urllib.request.build_opener()
        with opener.open(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body, (time.time() - started) * 1000.0
    except urllib.error.HTTPError as e:
        return e.code, "", (time.time() - started) * 1000.0
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e), (time.time() - started) * 1000.0


def processes():
    """Every process with its command line, EXCLUDING this one and its shell.

    Self-exclusion is not politeness. A query whose pattern appears in its own command line
    matches itself, and on 2026-09-16 that turned an idle machine into an apparent
    crash-loop -- the pids changing every poll were the polls.
    """
    ps = ("Get-CimInstance Win32_Process | "
          "Select-Object ProcessId,ParentProcessId,Name,CommandLine,CreationDate | "
          "ConvertTo-Json -Depth 2 -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, timeout=60)
        rows = json.loads(out.stdout.decode("utf-8", "replace") or "[]")
    except Exception:
        return None                       # UNKNOWN, not "no processes"
    if isinstance(rows, dict):
        rows = [rows]
    mine = {os.getpid()}
    try:
        mine.add(os.getppid())
    except Exception:
        pass
    return [r for r in rows if r.get("ProcessId") not in mine]


def find(procs, needle):
    """Processes whose command line contains `needle`, or None when the list is unknown."""
    if procs is None:
        return None
    n = needle.lower()
    return [p for p in procs
            if (p.get("CommandLine") or "").lower().find(n) >= 0
            and "ConvertTo-Json" not in (p.get("CommandLine") or "")]


def started_at(proc):
    """A start time, in whichever of the three shapes PowerShell hands back.

    ConvertTo-Json renders a CIM datetime as "/Date(1789...)/", NOT the WMI
    yyyymmddhhmmss.ffffff+zzz string -- so the first version of this printed "?" for every
    process and looked like a machine whose processes had no start times. A parser that
    degrades to "?" without saying why is the same defect as a dot that goes green on no
    evidence.
    """
    raw = proc.get("CreationDate")
    if isinstance(raw, dict):                      # some hosts emit {"value": "/Date(..)/"}
        raw = raw.get("value") or raw.get("DateTime")
    raw = str(raw or "")
    m = re.search(r"/Date\((-?\d+)", raw)
    if m:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(int(m.group(1)) / 1000.0))
    if len(raw) >= 14 and raw[:14].isdigit():      # WMI yyyymmddhhmmss
        return "%s-%s %s:%s:%s" % (raw[4:6], raw[6:8], raw[8:10], raw[10:12], raw[12:14])
    try:                                           # ISO-8601, if the host localises it
        return time.strftime("%m-%d %H:%M:%S", time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return "start time unreadable (%r)" % raw[:24]


def read_settings(path):
    try:
        st = os.stat(path)
        text = io.open(path, encoding="utf-8-sig").read()
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)
    keys = {}
    for ln in text.splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            keys[k.strip()] = v.strip()
    return {"path": path, "size": st.st_size, "mtime": st.st_mtime, "keys": keys}, None


# --------------------------------------------------------------------------- the sections
def _row_identity_guard(rep):
    """Is the pre-commit identity guard armed on THIS clone?

    On 2026-09-17 a `git add -A` staged generated files carrying an employee id and a home
    path, and the commit was pushed to a public repository; the fix was a history rewrite, the
    third. The guard could have refused it -- it has refused exactly that file since -- but
    nothing ran it between staging and committing. .githooks/pre-commit does now, via
    core.hooksPath, which is one `git config` away from being unset with no visible symptom:
    commits simply stop being refused, which is indistinguishable from commits being clean.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        # BINARY IN, DECODED DELIBERATELY. text=True would decode with the console code page,
        # and one byte it cannot represent loses the whole answer -- in a tool whose only job
        # is to report what is true.
        from tools.childproc import run as _child_run
        out = _child_run(["git", "-C", repo, "config", "--get", "core.hooksPath"], timeout=20)
        configured = (out.stdout or "").strip()
    except Exception as exc:
        rep.row(UNK, "  identity guard", "could not ask git: %s" % type(exc).__name__)
        return
    hook = os.path.join(repo, ".githooks", "pre-commit")
    if configured == ".githooks" and os.path.isfile(hook):
        rep.row(OK, "  identity guard", "armed -- a commit staging an employee id or a home "
                                        "path is refused before it can be pushed")
    elif configured == ".githooks":
        rep.row(BAD, "  identity guard", "core.hooksPath is set but %s is missing" % hook)
    else:
        rep.row(BAD, "  identity guard", "NOT armed (core.hooksPath=%r). Run: python "
                                         "scripts/install_git_hooks.py" % configured)


def section_context(rep):
    """WHICH MACHINE-VIEW AM I. Everything below is relative to this, so it goes first."""
    rep.section("context -- which view of the filesystem this process has")
    # THROUGH THE RESOLVER, not by hand. This tool built the path itself for an hour --
    # the eighth copy of a fact that already had seven, which is how the settings defect
    # became possible at all. tools/settings_path.py owns it.
    from tools.settings_path import settings_file, old_path, NEW_PATH
    path = settings_file()
    info, err = read_settings(path)
    if info is None:
        rep.row(UNK, "settings.txt", "%s -- %s" % (path, err))
        return None
    rep.row(OK, "settings.txt", "%s" % info["path"])
    # DURING THE MIGRATION, BOTH LOCATIONS. A machine part-way across has a file on each side,
    # and the only symptom of a split is that their identities differ.
    if path != NEW_PATH:
        rep.row(UNK, "  migration", "still on the OLD location; the new one does not exist "
                                    "yet (%s)" % NEW_PATH)
    other = old_path() if path == NEW_PATH else NEW_PATH
    try:
        if os.path.isfile(other):
            st = os.stat(other)
            rep.row(UNK, "  other copy", "%s (%d bytes, %s) -- two files exist; whichever a "
                                         "process reads decides what it believes"
                    % (other, st.st_size,
                       time.strftime("%m-%d %H:%M:%S", time.localtime(st.st_mtime))))
    except OSError:
        pass
    rep.row(OK, "  identity", "%d bytes, modified %s"
            % (info["size"], time.strftime("%m-%d %H:%M:%S", time.localtime(info["mtime"]))))
    rep.row(OK, "  floors it carries", "disk=%s GB  ram=%s MB"
            % (info["keys"].get("disk_floor_gb", "(absent)"),
               info["keys"].get("ram_floor_mb", "(absent)")))
    _row_identity_guard(rep)
    rep.row(OK, "  note", "a DIFFERENT size/mtime from another context means the two are "
                          "not the same file -- that was the 2026-09-16 defect")
    return info


def section_processes(rep, procs):
    rep.section("processes")
    if procs is None:
        rep.row(UNK, "process list", "could not be read; everything below is unknown")
        return
    wanted = [("supervisor", r"scripts\supervisor.ps1"),
              ("MCP server", "main.py"),
              ("devtunnel host", "devtunnel"),
              ("fleet coordinator", "relay.fleet_runner"),
              ("cockpit", "FleetCockpit.exe"),
              ("chat", "CopilotChat.exe")]
    for label, needle in wanted:
        hits = find(procs, needle) or []
        # A .exe is its own process name rather than a command line on some launch paths.
        if not hits and needle.endswith(".exe"):
            hits = [p for p in procs
                    if (p.get("Name") or "").lower() == os.path.basename(needle).lower()]
        if not hits:
            rep.row(BAD if label in ("supervisor", "MCP server") else UNK, label,
                    "not running")
        else:
            rep.row(OK, label, ", ".join("pid=%s since %s" % (h.get("ProcessId"), started_at(h))
                                         for h in hits[:3])
                    + ("  (+%d more)" % (len(hits) - 3) if len(hits) > 3 else ""))


def section_bridge_and_edge(rep):
    code, body, ms = http("http://127.0.0.1:8765/status")
    if code == 200:
        try:
            b = json.loads(body)
            rep.row(OK, "bridge", "HTTP 200 -- transport=%s turn_running=%s busy=%s"
                    % (b.get("transport"), b.get("turn_running"), b.get("busy")))
        except ValueError:
            rep.row(UNK, "bridge", "HTTP 200, unparseable body")
    else:
        rep.row(BAD, "bridge", "127.0.0.1:8765/status -- %s"
                % (("HTTP %s" % code) if code else body))

    code, body, ms = http("http://127.0.0.1:9222/json/version", timeout=3.0)
    if code == 200:
        rep.row(OK, "companion Edge", "HTTP 200 in %.0f ms on :9222" % ms)
    else:
        rep.row(UNK, "companion Edge", ":9222 -- %s (absent is normal when the stack is down)"
                % (("HTTP %s" % code) if code else body))


def section_tunnel(rep, health):
    url = ""
    try:
        for ln in io.open(os.path.join(REPO, ".env"), encoding="utf-8-sig"):
            if ln.startswith("MCP_TUNNEL_URL="):
                url = ln.split("=", 1)[1].strip()
    except Exception:
        pass
    if not url:
        rep.row(UNK, "tunnel", "MCP_TUNNEL_URL is not set in .env")
        return
    origin = "/".join(url.split("/")[:3])
    code, body, ms = http(origin + "/health", timeout=10.0)
    if code != 200:
        rep.row(BAD, "tunnel", "%s/health -- %s" % (origin, ("HTTP %s" % code) if code else body))
        return
    try:
        tpid = json.loads(body).get("server_pid")
    except ValueError:
        tpid = None
    lpid = (health or {}).get("server_pid")
    if tpid and lpid and tpid != lpid:
        rep.row(BAD, "tunnel", "reaches pid %s, but this machine runs %s -- it is forwarding "
                               "somewhere else" % (tpid, lpid))
    else:
        rep.row(OK, "tunnel", "HTTP 200 in %.0f ms and it reaches THIS server (pid %s)"
                % (ms, tpid))


def section_fleet(rep):
    rep.section("fleet")
    path = os.path.join(REPO, ".fleet", "status.json")
    try:
        d = json.load(io.open(path, encoding="utf-8-sig"))
    except Exception as e:
        rep.row(UNK, "status.json", "%s: %s" % (type(e).__name__, e))
        return
    age = time.time() - float(d.get("updated") or 0)
    running = bool(d.get("running"))
    # IDLE IS AN ANSWER. A fleet with nothing to do writes nothing, and a stale timestamp
    # then means "nobody asked it to work", not "I cannot tell". Only a run that claims to
    # be RUNNING while its status has gone quiet is a problem.
    if running and age > 120:
        rep.row(BAD, "run", "claims running=True but status.json has not moved for %.0fs "
                            "-- the coordinator may be wedged" % age)
    elif running:
        rep.row(OK, "run", "running, done %s/%s, status %.0fs old"
                % (d.get("done_count"), d.get("total"), age))
    else:
        rep.row(OK, "run", "idle (last run done %s/%s, %.0f min ago)"
                % (d.get("done_count"), d.get("total"), age / 60.0))

    if d.get("disk_floor_gb") is None and not running:
        rep.row(OK, "floors in the RUN", "not recorded -- no run is holding a floor right now")
    else:
        rep.row(OK, "floors in the RUN", "disk=%s GB  ram=%s MB   <- compare with the file "
                                         "above; a mismatch is the 2026-09-16 defect"
                % (d.get("disk_floor_gb"), d.get("ram_floor_mb")))
    for w in (d.get("workers") or [])[:4]:
        rep.row(OK, "  worker %s" % (w.get("name") or "?"),
                "%s turn=%s %s" % (w.get("status"), w.get("turn"),
                                   str(w.get("reason") or "")[:48]))


def section_health_strip(rep, stale_after_s=90.0):
    """What the COCKPIT'S DOTS ARE SHOWING, read from the panel rather than recomputed.

    On 2026-09-17 the operator asked why the server dot was lit and this tool could not say.
    It knew the server was reachable and that its code was stale -- it prints both -- but the
    dot is what a person looks at before deciding anything, and it existed only on the screen.
    The answer had to be read off a tooltip by hand, which is the inversion of the rule this
    repository works by: the command line first, the GUI after.

    NOT A SECOND OPINION. The cross-checks below still compute their own view. Two views that
    can disagree is the point -- a disagreement is exactly the defect class that cost a month
    in September, and it is only visible when both are written down.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".fleet", "health_strip.json")
    if not os.path.isfile(path):
        rep.section("health strip -- what the panel is showing")
        rep.row(UNK, "strip", "not published (%s). An older cockpit does not write it; "
                              "rebuild ui/ to get it." % path)
        return
    try:
        age = time.time() - os.stat(path).st_mtime
        strip = json.loads(io.open(path, encoding="utf-8").read())
    except Exception as exc:
        rep.section("health strip -- what the panel is showing")
        rep.row(BAD, "strip", "unreadable: %s" % type(exc).__name__)
        return

    rep.section("health strip -- what the panel is showing")
    # A STRIP THAT STOPPED BEING SWEPT IS A DIFFERENT FAILURE from a strip full of green, and
    # before this file existed neither was visible from here.
    if age > stale_after_s:
        rep.row(BAD, "strip age", "%.0f s old -- the cockpit is not sweeping, so every colour "
                                  "below is whatever it was when it stopped" % age)
    else:
        rep.row(OK, "strip age", "%.0f s old" % age)
    worst = {"red": BAD, "yellow": UNK, "gray": UNK, "checking": UNK}
    for d in strip.get("dots") or []:
        mark = worst.get(str(d.get("state")), OK)
        rep.row(mark, "  " + str(d.get("key", "?")),
                "%-8s %s" % (d.get("state"), str(d.get("detail") or "")[:150]))


def section_lock_attribution(rep, hours=24.0):
    """Of the lock classifications made recently, how many could only have been that worker's?

    A classification that names a worker without being able to establish it is not wrong on
    its face -- it is the fleet choosing a visible unlock over a silent lock -- but the rate is
    what says whether the choice is currently cheap or expensive, and nothing printed it.
    """
    from tools import settings_path as _sp          # for REPO, resolved in one place
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".fleet", "lock_refusals.jsonl")
    if not os.path.isfile(path):
        return
    cutoff = time.time() - hours * 3600.0
    total = exclusive = unknown = 0
    by_branch = {}
    try:
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or '"classified_locked"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if float(row.get("ts") or 0) < cutoff:
                    continue
                total += 1
                by_branch[row.get("branch") or "?"] = by_branch.get(row.get("branch") or "?", 0) + 1
                att = row.get("attribution") or {}
                if not att:
                    unknown += 1            # written before the field existed
                elif att.get("exclusive"):
                    exclusive += 1
    except OSError:
        return
    if not total:
        return
    rep.section("lock attribution -- how often it could be established")
    pct = 100.0 * exclusive / float(total)
    rep.row(OK if pct >= 50 else UNK, "last %gh" % hours,
            "%d classification(s), %d could only have been that worker (%.0f%%)"
            % (total, exclusive, pct))
    rep.row(OK, "  by branch", ", ".join("%s=%d" % kv for kv in sorted(by_branch.items())))
    if unknown:
        rep.row(UNK, "  no record", "%d were classified before the certainty was recorded"
                % unknown)


def section_settings(rep):
    """What each settings control does to a run, grouped by WHEN it lands.

    Deliberately grouped rather than listed per key: fifteen rows of prose is a section people
    scroll past, and the only question an operator has here is which group a knob is in.
    """
    from tools import settings_keys as SK
    said = {
        SK.LIVE: "changes a fleet that is ALREADY RUNNING, within about a second",
        SK.EACH_GATE: "re-read at every admission/approval decision; work in flight continues",
        SK.SWEEP_START: "read once when a coordinator starts -- the NEXT run gets it",
        SK.BRIDGE_START: "read once when the bridge starts -- it has to be restarted",
        SK.UI_ONLY: "window state; nothing outside the GUI reads it",
    }
    rep.section("settings -- when a change takes effect")
    for effect in SK.EFFECTS:
        keys = SK.names_with_effect(effect)
        if not keys:
            continue
        rep.row(OK, effect, said[effect])
        rep.row(OK, "", "  " + ", ".join(keys))


def section_resources(rep):
    rep.section("resources")
    try:
        import shutil
        free = shutil.disk_usage("C:\\").free / (1024.0 ** 3)
        rep.row(OK if free > 4 else BAD, "C: free", "%.2f GB" % free)
    except Exception as e:
        rep.row(UNK, "C: free", "%s" % e)


def section_recent(rep):
    rep.section("recent trouble")
    log = os.path.join(os.environ.get("TEMP", ""), "m365-companion-supervisor.log")
    try:
        lines = [l.rstrip() for l in io.open(log, encoding="utf-8", errors="replace")
                 if l.strip()]
    except Exception as e:
        rep.row(UNK, "supervisor log", "%s: %s" % (type(e).__name__, e))
        return
    interesting = [l for l in lines[-400:]
                   if ("failed" in l.lower() or "launched" in l.lower()
                       or "exiting" in l.lower() or "did not establish" in l.lower())]
    if not interesting:
        rep.row(OK, "supervisor log", "no failures or relaunches in the recent tail")
    for l in interesting[-6:]:
        rep.row(UNK, "  ", l[:110])
    if lines:
        rep.row(OK, "log last line", lines[-1][:110])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    rep = Report()
    settings = section_context(rep)
    section_processes(rep, processes())
    rep.section("endpoints")
    health = _endpoints(rep)
    section_fleet(rep)
    section_health_strip(rep)
    section_lock_attribution(rep)
    section_settings(rep)
    section_resources(rep)
    section_recent(rep)
    _crosscheck(rep, settings, health)

    if args.json:
        print(json.dumps({"at": time.time(), "sections": rep.sections},
                         ensure_ascii=False, indent=1))
    else:
        print(rep.to_text())
    return 0


def _endpoints(rep):
    """The MCP server, the bridge, Edge and the tunnel -- and for the tunnel, whether it
    reaches THIS server rather than merely answering."""
    code, body, ms = http("http://127.0.0.1:8000/health")
    health = None
    if code == 200:
        try:
            health = json.loads(body)
            rep.row(OK, "MCP server", "HTTP 200 in %.0f ms -- pid %s, head %s, code=%s, up %.0f min"
                    % (ms, health.get("server_pid"), str(health.get("server_head"))[:12],
                       health.get("server_code"), (health.get("server_uptime_s") or 0) / 60.0))
        except ValueError:
            rep.row(UNK, "MCP server", "HTTP 200 but the body is not JSON")
    else:
        rep.row(BAD, "MCP server", "127.0.0.1:8000/health -- %s"
                % (("HTTP %s" % code) if code else body))
    section_bridge_and_edge(rep)
    section_tunnel(rep, health)
    return health


def _crosscheck(rep, settings, health):
    """THE CHECKS NO SINGLE COMPONENT CAN DO, because each one only sees its own half."""
    rep.section("cross-checks")
    if settings and health:
        code = health.get("server_code")
        if code == "stale":
            rep.row(BAD, "server code", "the server is running code older than the checkout "
                                        "(head %s) -- restart it to pick up fixes"
                    % str(health.get("server_head"))[:12])
        elif code == "current":
            rep.row(OK, "server code", "matches the checkout")
        else:
            rep.row(UNK, "server code", "server_code=%r" % code)
    else:
        rep.row(UNK, "server code", "no /health body to compare")

    path = os.path.join(REPO, ".fleet", "status.json")
    try:
        run = json.load(io.open(path, encoding="utf-8-sig"))
    except Exception:
        run = None
    _crosscheck_floor(rep, settings, run)


def _crosscheck_floor(rep, settings, run):
    """Does the number in the file match the number the fleet is actually using?

    THIS IS THE LINE THAT WOULD HAVE SAVED A DAY. On 2026-09-16 settings.txt said 1 for a
    month and every coordinator reserved 4, and no single component could see both. Split out
    of the section that reads files so it can be tested without a machine.
    """
    want = (settings or {}).get("keys", {}).get("disk_floor_gb") if settings else None
    got = run.get("disk_floor_gb") if run else None
    if want is not None and got is not None:
        try:
            same = abs(float(want) - float(got)) < 1e-6
        except (TypeError, ValueError):
            same = False
        rep.row(OK if same else BAD, "floor agreement",
                "settings.txt says %s, the run uses %s%s"
                % (want, got, "" if same else
                   "  <- the panel and the fleet disagree; run this from the OTHER context "
                   "and compare the settings.txt identity at the top"))
    elif run is None:
        rep.row(UNK, "floor agreement", "no .fleet/status.json to compare against")
    elif got is None:
        rep.row(OK, "floor agreement", "nothing to compare: no run is holding a floor "
                                       "(settings.txt says %s)" % want)
    else:
        rep.row(UNK, "floor agreement", "settings.txt carries no disk_floor_gb line")


if __name__ == "__main__":
    sys.exit(main())

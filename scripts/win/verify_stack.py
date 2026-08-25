"""Is the stack actually running what the repository says, and on the transport it claims?

WHY THIS EXISTS. "I clicked start_all -- did it pick up the change?" was being answered by
reading a log, counting browser tabs over CDP, and comparing file timestamps by hand: three
sources, none of them the process itself, and every one of them a way to describe somebody
else's bridge. Three verification rounds were run against the wrong bridge in one night
because a losing launch and a silent success look identical from outside.

Each line below is a question with a PASS/FAIL, and the evidence beside it. Reporting is
read-only: it starts nothing, stops nothing, and writes nothing.

`--fix` acts on one verdict only -- a process older than the source it runs. It stops that
process and lets its own launcher bring it back, which is what start_all does and what the
supervisors exist for. It touches nothing else, and it refuses while work is in flight: a
bridge killed mid-turn loses the answer somebody is waiting for, and a fleet run loses the
tool calls its workers have already made.

    python scripts/win/verify_stack.py            # report
    python scripts/win/verify_stack.py --fix      # report, then restart what is stale
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROUTE_LOG = os.path.join(REPO, ".fleet", "socket_route.jsonl")
BRIDGE = "http://127.0.0.1:8765"

#: The directories start_all.ps1 compares against a process's start time when deciding
#: whether that process is stale. Mirrored here so this answers the same question the
#: launcher asks, rather than a similar-sounding one.
WATCHED = ("bridge", "relay", "tools")

OK, BAD, MEH = "PASS", "FAIL", "----"


def newest_source_mtime():
    """Newest non-test .py in the watched directories -- what a restart would pick up."""
    newest, where = 0.0, ""
    for sub in WATCHED:
        d = os.path.join(REPO, sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            try:
                m = os.path.getmtime(os.path.join(d, name))
            except OSError:
                continue
            if m > newest:
                newest, where = m, "%s/%s" % (sub, name)
    return newest, where


def bridge_status():
    try:
        with urllib.request.urlopen(BRIDGE + "/status", timeout=8) as r:
            return json.load(r)
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, str(exc)[:70])}


def cdp_pages(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=4) as fh:
            return [t.get("url", "") for t in json.load(fh) if t.get("type") == "page"]
    except Exception:
        return None


def mcp_started():
    """Start time of the MCP server process, or 0. PowerShell because this is Windows.

    ISO 8601 rather than -UFormat %s: the epoch conversion came back hours off, which read as
    a process NEWER than the edit it actually predates -- a verifier handing out a false PASS,
    which is worse than not checking at all.
    """
    script = (
        "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'main\\.py' } | "
        "Sort-Object CreationDate | Select-Object -First 1; "
        "if ($p) { Get-Date $p.CreationDate -Format o }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=25).stdout.strip()
        if not out:
            return 0.0
        import datetime

        return datetime.datetime.fromisoformat(out.split(".")[0]).timestamp()
    except Exception:
        return 0.0


def fleet_transport(limit=60):
    """How the last `limit` finished fleet workers were carried."""
    socket = tab = 0
    first_at = last_at = ""
    try:
        rows = []
        with open(ROUTE_LOG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("event") == "worker_done":
                    rows.append(o)
        window = rows[-limit:]
        if window:
            first_at, last_at = window[0].get("at", ""), window[-1].get("at", "")
        for o in window:
            if o.get("route") == "socket":
                socket += 1
            elif o.get("route") == "tab":
                tab += 1
    except OSError:
        pass
    return socket, tab, first_at, last_at


def say(state, question, evidence):
    print("  [%s] %-46s %s" % (state, question, evidence))


def fleet_runs_active():
    """PIDs of live fleet runs. Their workers are mid-turn and mid-tool-call."""
    script = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              "Where-Object { $_.CommandLine -match 'fleet_runner' } | "
              "ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=25).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def stop_and_wait(match, label, timeout_s=120):
    """Stop the matching processes and wait for a launcher to put one back.

    Stopping rather than starting, because each of these has a supervisor whose job is to
    notice and replace it -- and because launching one from here would mean reproducing the
    environment its own launcher sets, which is how two subtly different bridges come to
    exist on one machine.
    """
    kill = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -match '%s' } | "
            "Sort-Object ParentProcessId | Select-Object -First 1 | "
            "ForEach-Object { taskkill /PID $_.ProcessId /T /F }" % match)
    count = ("(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match '%s' } | Measure-Object).Count" % match)
    print("     stopping %s and waiting for its launcher..." % label)
    subprocess.run(["powershell", "-NoProfile", "-Command", kill],
                   capture_output=True, text=True, timeout=60)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(3)
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", count],
                                 capture_output=True, text=True, timeout=25).stdout.strip()
            if out.isdigit() and int(out) > 0:
                print("     %s is back" % label)
                return True
        except Exception:
            pass
    print("     %s did NOT come back within %ds -- start it by hand or click start_all"
          % (label, timeout_s))
    return False


def main():
    fix = "--fix" in sys.argv[1:]
    force = "--force" in sys.argv[1:]
    newest, where = newest_source_mtime()
    st = bridge_status()
    stale = []
    print("stack verification -- %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))

    print("the bridge")
    if "error" in st:
        say(BAD, "is it answering?", st["error"])
    else:
        fresh = st.get("started", 0) >= newest
        if not fresh:
            stale.append(("bridge", "copilot_bridge"))
        say(OK if fresh else BAD, "running tonight's code?",
            "started %s, newest source %s (%s)"
            % (time.strftime("%H:%M:%S", time.localtime(st.get("started", 0))),
               time.strftime("%H:%M:%S", time.localtime(newest)), where))
        t = st.get("transport")
        # "none" is not a failure, it is a question nobody has asked yet: a bridge picks its
        # transport when a turn starts, so a freshly restarted one has no answer to give.
        # Calling that FAIL made this script's first run accuse a perfectly healthy bridge.
        state = OK if t == "socket" else (MEH if t == "none" else BAD)
        detail = ("no turn sent yet -- send one and re-run" if t == "none"
                  else "socket_enabled=%s" % st.get("socket_enabled"))
        say(state, "carrying its conversation on a socket?", "transport=%s  %s" % (t, detail))
        pages = cdp_pages(9223) or []
        copilot = [u for u in pages if "m365.cloud.microsoft" in u]
        say(OK if not copilot else MEH, "holding no Copilot page?",
            "%d Copilot page(s) of %d; release_startup_page=%s"
            % (len(copilot), len(pages), st.get("release_startup_page")))
        store = st.get("store") or {}
        say(OK if store.get("sessions") else BAD, "conversations in the SQLite store?",
            ", ".join("%s=%s" % (k, v) for k, v in sorted(store.items())) or "(nothing)")
        say(OK if st.get("conversation") else MEH, "this conversation has an id recorded?",
            st.get("conversation") or "(none yet -- no socket turn in this session)")

    print("\nthe MCP server")
    started = mcp_started()
    if not started:
        say(BAD, "is it running?", "no main.py process found")
    else:
        if started < newest:
            stale.append(("MCP server", "main\.py"))
        say(OK if started >= newest else BAD, "running tonight's code?",
            "started %s, newest source %s"
            % (time.strftime("%H:%M:%S", time.localtime(started)),
               time.strftime("%H:%M:%S", time.localtime(newest))))

    print("\nthe fleet")
    socket, tab, first_at, last_at = fleet_transport()
    total = socket + tab
    # A tab in this window is worth seeing but is not necessarily wrong NOW -- the window
    # reaches back through whatever the log holds, including runs that predate a fix. So it
    # shows the split and the span and leaves "is that acceptable" to a reader who knows when
    # the fix landed. A check that cries FAIL over history stops being read.
    say(OK if total and not tab else MEH, "last 60 workers carried on sockets?",
        ("socket=%d tab=%d  (%s .. %s)" % (socket, tab, first_at[-8:], last_at[-8:]))
        if total else "(no finished workers recorded)")

    if not fix:
        if stale:
            print("\n%d process(es) are older than the source they run: %s"
                  % (len(stale), ", ".join(n for n, _ in stale)))
            print("Click start_all again, or re-run this with --fix.")
        else:
            print("\nEverything running matches the source on disk.")
        return 0

    print("\n--fix")
    if not stale:
        print("     nothing to do; everything running matches the source on disk.")
        return 0
    runs = fleet_runs_active()
    if runs and not force:
        print("     REFUSING: %d fleet run(s) are live (%s). Their workers are mid-turn and "
              "mid-tool-call;\n     restarting the MCP server would break calls they have "
              "already made. Wait for them, or pass --force."
              % (len(runs), ",".join(str(x) for x in runs)))
        return 1
    if (st.get("turn_running") or st.get("busy")) and not force:
        print("     REFUSING: the bridge is mid-turn. Somebody is waiting for that answer, "
              "and on a\n     goal that acts the act may already have happened. Wait, or "
              "pass --force.")
        return 1
    ok = True
    for label, match in stale:
        ok = stop_and_wait(match, label) and ok
    print("\n     re-run this to confirm what came back.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

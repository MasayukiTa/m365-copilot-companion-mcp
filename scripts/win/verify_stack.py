"""Is the stack actually running what the repository says, and on the transport it claims?

WHY THIS EXISTS. "I clicked start_all -- did it pick up the change?" was being answered by
reading a log, counting browser tabs over CDP, and comparing file timestamps by hand: three
sources, none of them the process itself, and every one of them a way to describe somebody
else's bridge. Three verification rounds were run against the wrong bridge in one night
because a losing launch and a silent success look identical from outside.

Each line below is a question with a PASS/FAIL, and the evidence beside it. Read-only: it
starts nothing, stops nothing, and writes nothing.

    python scripts/win/verify_stack.py
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


def main():
    newest, where = newest_source_mtime()
    st = bridge_status()
    print("stack verification -- %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))

    print("the bridge")
    if "error" in st:
        say(BAD, "is it answering?", st["error"])
    else:
        fresh = st.get("started", 0) >= newest
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

    print("\nA FAIL on \"running tonight's code\" means the process predates the edit: click "
          "start_all again,\nor stop that process and let its launcher bring it back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

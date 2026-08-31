# -*- coding: utf-8 -*-
"""Whether the SERVER THAT IS RUNNING contains the code that is on disk.

THE INCIDENT THIS COMES FROM, 2026-09-01. Every shadow assessment in a benchmark run came back
UNVERIFIABLE with the reason "no tool calls recorded for this task". The evidence ledger held
zero rows and its file did not exist. The wiring in the gateway was correct, and a test asserted
it was correct -- by reading main.py's source, which is exactly what a source assertion is good
for and exactly why it caught nothing. The server process had started at 17:01 and the wiring
was written at 19:20. The code was right; it had simply never run.

WHY NO ORDINARY TEST FINDS THIS. Every test imports the module from disk, so every test sees the
new code. The defect lives entirely in the gap between the file and the process, and nothing
that reads the file can see across it. The pipeline was reporting UNVERIFIABLE, which was
literally true and read as "the workers did nothing" rather than "nothing was recording".

WHAT THIS CHECKS. One question: is any file the server would import newer than the moment the
server started? That is answerable without importing anything, without a health endpoint, and
without the server's cooperation -- which matters, because a stale server answers health checks
perfectly well. It is the same question as "is this deployed", asked of a process instead of a
host.

FAILS CLOSED. If the process cannot be found or its start time cannot be read, the answer is
"cannot tell", never "fresh". A freshness check that reports fresh when it does not know is the
thing it exists to prevent.
"""
from __future__ import annotations

import os
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Directories whose contents the server actually imports. Not the whole repo: a benchmark log
#: or a test file being newer than the process says nothing about the server's behaviour, and a
#: check that cries stale for those gets ignored, which is worse than not having it.
WATCHED = ("tools", "relay", "bench")

#: Files that are not imported by the running server even though they sit in a watched
#: directory. A test file changing cannot change what the server does.
def _is_watched(path):
    base = os.path.basename(path)
    if not base.endswith(".py"):
        return False
    if base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py":
        return False
    return "__pycache__" not in path.replace("\\", "/")


def server_processes(pattern="main.py"):
    """(pid, start_epoch) for each running server process. Empty if psutil is missing -- which
    the caller must treat as "cannot tell", not as "none running"."""
    try:
        import psutil
    except Exception:
        return None
    found = []
    for p in psutil.process_iter(["name", "cmdline", "create_time"]):
        try:
            if "python" not in (p.info.get("name") or "").lower():
                continue
            cmd = " ".join(p.info.get("cmdline") or [])
            if pattern in cmd and REPO.lower() in cmd.lower().replace("/", "\\"):
                found.append((p.pid, float(p.info.get("create_time") or 0)))
        except Exception:
            continue
    return found


def newer_than(when, root=None, watched=WATCHED):
    """Imported files modified after `when`, newest first. This is the evidence."""
    root = root or REPO
    out = []
    for rel in watched:
        base = os.path.join(root, rel)
        if not os.path.isdir(base):
            continue
        for parent, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                path = os.path.join(parent, f)
                if not _is_watched(path):
                    continue
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime > when:
                    out.append((os.path.relpath(path, root), mtime))
    # main.py itself is the gateway and is not under a watched directory.
    top = os.path.join(root, "main.py")
    try:
        if os.path.getmtime(top) > when:
            out.append(("main.py", os.path.getmtime(top)))
    except OSError:
        pass
    return sorted(out, key=lambda x: -x[1])


def check(pattern="main.py", root=None):
    """Returns {fresh, why, stale_files, started, pids}. fresh is None for "cannot tell".

    THREE ANSWERS, NOT TWO. Fresh, stale, and unknown are different, and collapsing unknown
    into either one is how this check would come to be trusted when it should not be.
    """
    procs = server_processes(pattern)
    if procs is None:
        return {"fresh": None, "why": "psutil unavailable, so the running code cannot be dated",
                "stale_files": [], "started": None, "pids": []}
    if not procs:
        return {"fresh": None, "why": "no %s process is running" % pattern,
                "stale_files": [], "started": None, "pids": []}
    started = min(t for _pid, t in procs)
    if not started:
        return {"fresh": None, "why": "the process start time could not be read",
                "stale_files": [], "started": None, "pids": [p for p, _ in procs]}
    stale = newer_than(started, root)
    return {
        "fresh": not stale,
        "why": ("" if not stale else
                "%d imported file(s) changed after the server started %.1f h ago; it is running "
                "code that is no longer on disk" % (len(stale), (time.time() - started) / 3600)),
        "stale_files": [f for f, _ in stale[:20]],
        "started": started,
        "pids": [p for p, _ in procs],
    }


def main(argv=None):
    r = check()
    if r["fresh"] is True:
        print("FRESH: the running server matches the code on disk")
        return 0
    if r["fresh"] is None:
        print("UNKNOWN: %s" % r["why"])
        return 2
    print("STALE: %s" % r["why"])
    for f in r["stale_files"]:
        print("   %s" % f)
    print("Restart it, or the change is committed and not running.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

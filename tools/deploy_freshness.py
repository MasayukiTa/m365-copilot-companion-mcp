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
#:
#: VERIFIED RATHER THAN ASSUMED. The first version listed bench/ as well, and immediately
#: reported four bench files as making the server stale -- which is exactly the crying-wolf this
#: comment warns against, in the same file. main.py's own import graph names two local
#: packages, tools and relay, and bench is not one of them: those are standalone scripts the
#: server never loads. The list is checked against that graph by a test, so adding an import
#: cannot silently take a directory out of scope.
WATCHED = ("tools", "relay")

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


import hashlib


def file_sha256(path):
    """Hex sha256 of a file's bytes, or "" when it cannot be read.

    A file that cannot be read returns "" -- the same sentinel a missing file gets -- so a
    disappearance reads as a content change, the conservative side. Never raises.
    """
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def watched_files(root=None, watched=WATCHED):
    """Every file the running server imports, as root-relative posix paths, sorted.

    The same selection newer_than() walks (WATCHED dirs filtered by _is_watched, plus the
    top-level main.py), but WITHOUT a time test -- the caller decides what to compare. Pure
    apart from reading the directory tree; never raises on a single unreadable entry.
    """
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
                out.append(os.path.relpath(path, root).replace("\\", "/"))
    top = os.path.join(root, "main.py")
    if os.path.isfile(top):
        out.append("main.py")
    return sorted(set(out))


def fingerprint(root=None, watched=WATCHED):
    """{relative/posix/path: sha256} for every watched file the server imports.

    Recorded once at server start and compared later by changed_against(). A file present at
    start but gone later keeps its recorded hash and compares as changed; a file that appears
    later is added by the second fingerprint and compares as changed too.
    """
    root = root or REPO
    return {rel: file_sha256(os.path.join(root, rel.replace("/", os.sep)))
            for rel in watched_files(root, watched)}


def changed_against(baseline, root=None, watched=WATCHED):
    """Watched files whose CONTENT differs from `baseline`, sorted. This is the mtime-proof
    evidence.

    WHY CONTENT, NOT mtime. newer_than() dates files by mtime, which is cheap but wrong for
    the thing that triggers it most: ``git checkout`` / ``pull`` / ``merge`` rewrites the
    working tree and bumps the mtime of a watched file even when its bytes are identical (a
    branch switch that touches an unrelated file, a fast-forward that re-materialises a file
    to the same content). mtime then says 'stale', the supervisor cycles the live server, and
    an outside client sees a 20s-2min outage for a change the server never actually needed to
    load. Comparing the recorded sha256 to the current one is immune to that: only a real
    byte change counts.

    `baseline` is a {rel: sha256} mapping from fingerprint() taken at process start. Pure
    apart from reading the files; never raises.
    """
    current = fingerprint(root, watched)
    keys = set(baseline or {}) | set(current)
    return sorted(k for k in keys if (baseline or {}).get(k) != current.get(k))


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


def check(pattern="main.py", root=None, baseline=None):
    """Returns {fresh, why, stale_files, started, pids}. fresh is None for "cannot tell".

    THREE ANSWERS, NOT TWO. Fresh, stale, and unknown are different, and collapsing unknown
    into either one is how this check would come to be trusted when it should not be.

    ``baseline`` -- when given, a {rel: sha256} fingerprint() taken at the moment the server
    started. The mtime list from newer_than() is then CONFIRMED by content: a watched file
    whose mtime moved but whose bytes are identical to the baseline (the ``git checkout`` /
    ``pull`` case) is dropped, so it does not count as staleness. Without a baseline the old
    mtime-only rule stands, which is the conservative side for a caller that cannot record a
    fingerprint.
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
    if baseline is not None:
        # CONTENT IS THE AUTHORITY when we have a baseline. A git checkout re-stamps mtimes,
        # so using mtime even as a pre-filter would both over-report (same-bytes touch) and,
        # if a change somehow landed with an unchanged mtime, under-report. changed_against
        # hashes the watched set directly and answers purely on bytes.
        stale_files = list(changed_against(baseline, root))
    else:
        stale_files = [f for f, _ in newer_than(started, root)]
    stale = bool(stale_files)
    return {
        "fresh": not stale,
        "why": ("" if not stale else
                "%d imported file(s) changed after the server started %.1f h ago; it is running "
                "code that is no longer on disk" % (len(stale_files), (time.time() - started) / 3600)),
        "stale_files": stale_files[:20],
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

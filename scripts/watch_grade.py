# -*- coding: utf-8 -*-
"""Watch a running Pro grade WITHOUT touching the WSL VM it is running inside.

WHY THIS EXISTS. The grade runs in one held ssh session, because the WSL VM tears down when a
`wsl -d ... --exec` command returns and takes dockerd with it. So every ordinary way of looking
at it -- a `df`, a one-line `tail` inside the distro -- is itself a wsl session whose exit can
kill the grade. Measured: a 14-instance grade died about twenty seconds after two progress
checks were issued against the same host, leaving `client_loop: send disconnect` and no
eval_results.json.

The log lives on /mnt/c, so it is readable from the Windows side, where no VM is involved. That
is the only safe way to look while a grade is in flight, and this script is that way.

It also reports the failures that must not be waited out. A grade that cannot find its image
does not fail: it returns None per instance and the wrapper records a zero. Fourteen of those
were once written in 87 seconds and read as a score of 0.0%.

    python scripts/watch_grade.py --tag clean1
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bench"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pro_grade_remote as G   # noqa: E402

REMOTE_LOG = "C:/swe-grade/pro_grade_%s.out"

#: Lines that mean this run is producing zeros rather than measurements.
BAD = [
    ("Failed to pull or find image locally", "an image is missing and cannot be pulled; every "
                                             "instance that hits this is recorded as a zero"),
    ("returned None", "the harness produced no report for an instance"),
    ("No space left on device", "the eval filesystem filled up"),
    ("Read-only file system", "the eval filesystem went read-only again"),
]


def read(host: str, tag: str, tail: int = 40) -> str:
    """The log, read from the Windows side. Starts no WSL session, so it cannot kill the grade."""
    cmd = "Get-Content '%s' -Tail %d" % (REMOTE_LOG % tag, tail)
    return (G.ssh(host, cmd, 180)[1] or "")


def assess(text: str):
    """(state, notes). state is one of running / finished / broken / unknown."""
    notes = []
    for needle, why in BAD:
        n = text.count(needle)
        if n:
            notes.append("%d x %s -- %s" % (n, needle, why))

    started = re.search(r"START pro grade", text)
    done = re.search(r"DONE_PRO_GRADE", text)
    m = re.search(r"RESOLVED (\d+)/(\d+)", text)
    if m:
        notes.append("reported RESOLVED %s/%s" % (m.group(1), m.group(2)))

    # A DURATION IS EVIDENCE. Pulling an image and running a repository's test suite does not
    # happen in seconds; a whole batch finishing in a minute means nothing was evaluated.
    t0 = re.search(r"\[(\d\d:\d\d:\d\d)\] START pro grade", text)
    t1 = re.search(r"DONE_PRO_GRADE (\d\d:\d\d:\d\d)", text)
    if t0 and t1:
        def secs(s):
            h, m_, s_ = (int(x) for x in s.split(":"))
            return h * 3600 + m_ * 60 + s_
        d = secs(t1.group(1)) - secs(t0.group(1))
        if d < 0:
            d += 24 * 3600
        notes.append("took %d seconds end to end" % d)
        if d < 120:
            notes.append("TOO FAST TO BE A MEASUREMENT: under two minutes for a whole batch")

    if done:
        return "finished", notes
    if started:
        return "running", notes
    if not text.strip():
        return "unknown", notes + ["the log is empty; the grade may not have started"]
    return "unknown", notes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tag", required=True, help="the grade's tag, e.g. clean1")
    ap.add_argument("--tail", type=int, default=40)
    a = ap.parse_args(argv)

    host = G.ssh_host()
    if not host:
        print("no eval host configured")
        return 2
    text = read(host, a.tag, a.tail)
    state, notes = assess(text)
    print("state: %s" % state)
    for n in notes:
        print("  - %s" % n)
    print("--- last lines ---")
    print("\n".join(text.strip().splitlines()[-6:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

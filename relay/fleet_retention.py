"""Retention for the working directory, which nothing has ever bounded.

.fleet held 375 MB, and the shape of it matters more than the total: 120 MB is the session
database, which is real history and stays, and the other 255 MB is logs. 185 MB of those 255
were written in the last SEVEN DAYS -- about 26 MB a day, none of it ever removed. This is not
an accumulation of old junk that a one-time sweep fixes; it is a steady write with no ceiling,
and left alone it adds roughly three quarters of a gigabyte a month.

WHAT MAY BE DELETED IS DECIDED BY WHO READS IT, checked rather than assumed:

  coordinator_*.log   capture_budget.newest_log() takes max(mtime) and reads THAT one. No
                      caller anywhere opens an older one, so past runs' logs are diagnostics
                      for runs that have finished. Aged out.

  *.log.<n>           rotations. A rotation that is itself superseded is a copy of a copy;
                      faulthandler.log.1 alone was 12.8 MB.

  _*                  scratch that workers wrote into .fleet and left -- 217 files of
                      _apply_cell_styles.py and _agent_resolve.png. Nothing imports them.

  *.jsonl             NOT aged out. tool_ledger.read() walks the whole file, and that ledger
                      is what a claimed result gets checked against; dropping old lines would
                      quietly weaken a verification rather than free disk. These get a SIZE
                      ceiling that keeps the TAIL, so the recent end -- the end every reader
                      cares about -- survives, and only a runaway writer is ever truncated.

  sessions.sqlite3    not touched here at all. It has its own retention (session_store.
                      apply_retention) with its own settings, and a second policy reaching
                      into the same file is how two mechanisms end up disagreeing about what
                      is still live.

DEFAULTS ARE DELIBERATE, AND THE PRECEDENT IS INSIDE THIS REPOSITORY. The session store's
retention defaults to keeping everything, with the reason recorded where it is set: that store
exists because history had been disappearing. So nothing here removes anything a reader can
still reach. What it removes is superseded copies, scratch, and diagnostics for runs that
ended -- and it says what it removed, every time.
"""
from __future__ import annotations

import glob
import io
import os
import re
import time

#: Diagnostics for finished runs. Generous because they cost little and are the first thing
#: wanted when a run is being reconstructed; only the newest is ever read by code.
COORDINATOR_KEEP_DAYS = float(os.environ.get("MCP_FLEET_LOG_DAYS", "14"))
#: ...and never fewer than this many, however old. A quiet fortnight must not leave the next
#: investigation with nothing to read.
COORDINATOR_KEEP_MIN = int(os.environ.get("MCP_FLEET_LOG_KEEP", "20"))

#: Superseded rotations to keep. 1 = keep the immediately-previous file, drop older ones.
ROTATION_KEEP = int(os.environ.get("MCP_FLEET_ROTATION_KEEP", "1"))

#: Worker scratch left in the .fleet root.
SCRATCH_KEEP_DAYS = float(os.environ.get("MCP_FLEET_SCRATCH_DAYS", "14"))

#: Per-file ceiling for append-only ledgers, tail kept. The largest today is 7 MB, so this is
#: a stop on a runaway writer rather than a policy that bites -- said plainly because a limit
#: that never fires should not be reported as if it were doing work.
JSONL_MAX_MB = float(os.environ.get("MCP_FLEET_JSONL_MAX_MB", "64"))

#: "faulthandler.log.1" -> base "faulthandler.log", index 1. Anchored and split in one place:
#: the first version of this reassembled the base from match offsets and was unreadable, which
#: on a function that deletes files is a defect in itself.
_ROTATION = re.compile(r"^(.*\.(?:log|jsonl))\.(\d+)$")


def _age_days(path, now):
    try:
        return (now - os.path.getmtime(path)) / 86400.0
    except OSError:
        return 0.0


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _rm(path, dry_run):
    n = _size(path)
    if not dry_run:
        try:
            os.remove(path)
        except OSError:
            return 0
    return n


def coordinator_logs(fleet_dir, now=None, dry_run=False,
                     keep_days=None, keep_min=None):
    """Remove coordinator logs for runs that finished, keeping the recent ones."""
    now = time.time() if now is None else now
    keep_days = COORDINATOR_KEEP_DAYS if keep_days is None else keep_days
    keep_min = COORDINATOR_KEEP_MIN if keep_min is None else keep_min
    paths = sorted(glob.glob(os.path.join(fleet_dir, "coordinator_*.log")),
                   key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
                   reverse=True)
    # The newest `keep_min` are kept whatever their age -- that is the floor, not the policy.
    freed, removed = 0, []
    for p in paths[keep_min:]:
        if _age_days(p, now) <= keep_days:
            continue
        freed += _rm(p, dry_run)
        removed.append(os.path.basename(p))
    return freed, removed


def rotations(fleet_dir, dry_run=False, keep=None):
    """Remove superseded rotations (`x.log.2` and older), keeping `keep` of them."""
    keep = ROTATION_KEEP if keep is None else keep
    by_base = {}
    try:
        names = os.listdir(fleet_dir)
    except OSError:
        return 0, []
    for n in names:
        m = _ROTATION.match(n)
        if not m:
            continue
        by_base.setdefault(m.group(1), []).append((int(m.group(2)), n))
    freed, removed = 0, []
    for _base, items in by_base.items():
        for idx, name in sorted(items):
            if idx <= keep:
                continue
            p = os.path.join(fleet_dir, name)
            freed += _rm(p, dry_run)
            removed.append(name)
    return freed, removed


def scratch(fleet_dir, now=None, dry_run=False, keep_days=None):
    """Remove worker scratch left in the .fleet ROOT. Not recursive: the subdirectories are
    structured stores (sessions, transcripts, guard), and a name-prefix rule has no business
    reaching into them."""
    now = time.time() if now is None else now
    keep_days = SCRATCH_KEEP_DAYS if keep_days is None else keep_days
    freed, removed = 0, []
    try:
        names = os.listdir(fleet_dir)
    except OSError:
        return 0, []
    for n in names:
        if not n.startswith("_"):
            continue
        p = os.path.join(fleet_dir, n)
        if not os.path.isfile(p):
            continue
        if _age_days(p, now) <= keep_days:
            continue
        freed += _rm(p, dry_run)
        removed.append(n)
    return freed, removed


#: Per-run worker transcripts (transcripts/r<run>_a0_w<n>.jsonl) and the benchmark scratch
#: beside them. The single biggest non-database category: 4199 files, 80 MB, of which 34 MB
#: predates the last thirty days.
STORE_KEEP_DAYS = float(os.environ.get("MCP_FLEET_STORE_DAYS", "30"))

#: Subdirectories the age rule may enter, BY NAME. An allow-list rather than a deny-list: a
#: rule that descends everywhere except what it remembers to exclude fails open, and the thing
#: it would eventually reach is the session database. Naming what may be entered means a new
#: store added later is untouched until someone decides otherwise, which is the safe default.
STORE_DIRS = ("transcripts", "swe")


def stores(fleet_dir, now=None, dry_run=False, keep_days=None, names=None):
    """Age out per-run files inside the named stores.

    `sessions` is deliberately absent from STORE_DIRS: that directory holds the conversation
    database, which has its own retention with its own settings, and two policies on one store
    is how they come to disagree about what is still live.
    """
    now = time.time() if now is None else now
    keep_days = STORE_KEEP_DAYS if keep_days is None else keep_days
    freed, removed = 0, []
    for name in (names or STORE_DIRS):
        root = os.path.join(fleet_dir, name)
        if not os.path.isdir(root):
            continue
        for parent, _dirs, files in os.walk(root):
            for f in files:
                p = os.path.join(parent, f)
                # Never a database, wherever it turns up. The extension check is cheap and it
                # is the last line between an age rule and someone's history.
                if os.path.splitext(f)[1].lower() in (".sqlite3", ".db", ".sqlite"):
                    continue
                if _age_days(p, now) <= keep_days:
                    continue
                freed += _rm(p, dry_run)
                removed.append(os.path.relpath(p, fleet_dir))
    return freed, removed


def cap_jsonl(fleet_dir, dry_run=False, max_mb=None):
    """Hold each append-only ledger under a ceiling, KEEPING THE TAIL.

    Truncating the head of an evidence ledger is a real cost, so this is sized as a stop on a
    runaway writer rather than a routine trim. The tail is what every reader wants: the last
    events, not the first.
    """
    max_mb = JSONL_MAX_MB if max_mb is None else max_mb
    limit = int(max_mb * 1024 * 1024)
    freed, trimmed = 0, []
    try:
        names = os.listdir(fleet_dir)
    except OSError:
        return 0, []
    for n in names:
        if not n.endswith(".jsonl"):
            continue
        p = os.path.join(fleet_dir, n)
        before = _size(p)
        if before <= limit:
            continue
        if dry_run:
            freed += before - limit
            trimmed.append(n)
            continue
        try:
            with io.open(p, "rb") as fh:
                fh.seek(before - limit)
                # Land on a line boundary: half a JSON object at the head of the file is a
                # parse error for every reader, which is worse than the bytes it saved.
                fh.readline()
                tail = fh.read()
            with io.open(p, "wb") as fh:
                fh.write(tail)
            freed += before - _size(p)
            trimmed.append(n)
        except OSError:
            continue
    return freed, trimmed


def apply(fleet_dir=None, now=None, dry_run=False):
    """Run every rule. Returns a report; never raises."""
    fleet_dir = fleet_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".fleet")
    # BYTES AS WELL AS MEGABYTES. Reporting only round(x, 1) MB prints 0.0 for anything under
    # about 50 KB, so a dry run over a handful of small files says it would free nothing --
    # indistinguishable from a rule that is not matching, which is the one thing a dry run
    # exists to let someone tell apart.
    rep = {"dir": fleet_dir, "dry_run": bool(dry_run),
           "freed_mb": 0.0, "freed_bytes": 0, "rules": {}}
    if not os.path.isdir(fleet_dir):
        return rep
    for name, fn in (("coordinator_logs", coordinator_logs),
                     ("rotations", rotations),
                     ("scratch", scratch),
                     ("stores", stores),
                     ("cap_jsonl", cap_jsonl)):
        try:
            if fn in (coordinator_logs, scratch, stores):
                freed, items = fn(fleet_dir, now=now, dry_run=dry_run)
            else:
                freed, items = fn(fleet_dir, dry_run=dry_run)
        except Exception as exc:
            rep["rules"][name] = {"error": "%s: %s" % (type(exc).__name__, exc)}
            continue
        rep["rules"][name] = {"freed_mb": round(freed / 1048576.0, 1),
                              "freed_bytes": freed, "count": len(items)}
        rep["freed_bytes"] += freed
    rep["freed_mb"] = round(rep["freed_bytes"] / 1048576.0, 1)
    return rep


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is a dry run that only reports)")
    ap.add_argument("--dir", default=None)
    args = ap.parse_args()
    rep = apply(fleet_dir=args.dir, dry_run=not args.apply)
    print("%s  %s" % (rep["dir"], "(dry run)" if rep["dry_run"] else ""))
    for name, r in rep["rules"].items():
        if "error" in r:
            print("  %-20s ERROR %s" % (name, r["error"]))
        else:
            print("  %-20s %6.1f MB  %d item(s)" % (name, r["freed_mb"], r["count"]))
    print("  %-20s %6.1f MB total" % ("", rep["freed_mb"]))

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
import json
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


def _setting(key, default):
    """A number from the cockpit's settings.txt, falling back to the env var and then the
    default. Wired so these knobs live where the CONVERSATION retention already lives: that one
    is settable in the cockpit and this one was environment-only, which meant "the retention
    period is configurable" was half true and the half that was not was the half writing 26 MB
    a day."""
    try:
        from relay.fleet_runner import _settings_float
        return _settings_float(key, default)
    except Exception:
        return default


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
    keep_days = _setting("fleet_log_days", COORDINATOR_KEEP_DAYS) if keep_days is None else keep_days
    keep_min = COORDINATOR_KEEP_MIN if keep_min is None else keep_min
    # BOTH FORMS. Once compression became the default, matching only "*.log" meant every
    # gzipped log fell outside this rule and was kept forever -- the compression would have
    # quietly disabled the retention that runs beside it.
    paths = sorted(glob.glob(os.path.join(fleet_dir, "coordinator_*.log"))
                   + glob.glob(os.path.join(fleet_dir, "coordinator_*.log.gz")),
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
    keep_days = _setting("fleet_scratch_days", SCRATCH_KEEP_DAYS) if keep_days is None else keep_days
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
    keep_days = _setting("fleet_store_days", STORE_KEEP_DAYS) if keep_days is None else keep_days
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


#: Finished logs are gzipped rather than deleted. THIS IS THE RULE THAT MATTERS, and the
#: measurement is why: sampled on the live directory, coordinator logs compress by 99% (31.0 MB
#: -> 0.2 MB) and run transcripts by 93%. They are enormously repetitive text.
#:
#: That changes the whole shape of the problem. Age-based deletion reached 47 MB of 255 because
#: 185 MB of it was written in the last week -- retention cannot touch recent evidence without
#: destroying it. Compression reaches ALL of it and destroys none: the same 26 MB a day becomes
#: about 2, and every line is still there to read.
COMPRESS_AFTER_HOURS = float(os.environ.get("MCP_FLEET_COMPRESS_HOURS", "6"))

#: The newest coordinator logs stay uncompressed. capture_budget.newest_log() takes max(mtime)
#: over `coordinator_*.log` and reads it; compressing the file a live run is still appending to
#: would corrupt it, and compressing the one the budget check reads would silently make that
#: check see nothing.
COMPRESS_KEEP_PLAIN = int(os.environ.get("MCP_FLEET_COMPRESS_KEEP_PLAIN", "3"))


def open_maybe_gz(path, mode="rt", encoding="utf-8", errors="replace"):
    """Open `path`, or its .gz sibling if that is what exists. Readers call this instead of
    open() so compression is invisible to them -- a reader that has to know is a reader that
    will one day be added without knowing."""
    import gzip as _gzip
    if not os.path.exists(path) and os.path.exists(path + ".gz"):
        path = path + ".gz"
    if path.endswith(".gz"):
        return _gzip.open(path, mode, encoding=encoding, errors=errors)
    return io.open(path, mode, encoding=encoding, errors=errors)


def compress(fleet_dir, now=None, dry_run=False, after_hours=None, keep_plain=None):
    """Gzip finished logs in place. Returns (bytes_saved, [names]).

    Never touches a file that is still being written: `after_hours` keeps anything recent, and
    the newest `keep_plain` coordinator logs stay plain whatever their age.
    """
    import gzip as _gzip
    now = time.time() if now is None else now
    after_hours = _setting("fleet_compress_hours", COMPRESS_AFTER_HOURS) if after_hours is None else after_hours
    keep_plain = COMPRESS_KEEP_PLAIN if keep_plain is None else keep_plain
    cutoff_days = after_hours / 24.0

    targets = sorted(glob.glob(os.path.join(fleet_dir, "coordinator_*.log")),
                     key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
                     reverse=True)[keep_plain:]
    for name in STORE_DIRS:
        targets += glob.glob(os.path.join(fleet_dir, name, "*.jsonl"))
        targets += glob.glob(os.path.join(fleet_dir, name, "*", "*.jsonl"))

    saved, done = 0, []
    for p in targets:
        if p.endswith(".gz") or not os.path.isfile(p):
            continue
        if _age_days(p, now) <= cutoff_days:
            continue
        before = _size(p)
        if not before:
            continue
        if dry_run:
            saved += int(before * 0.95)     # measured 93-99%; deliberately the low end
            done.append(os.path.relpath(p, fleet_dir))
            continue
        gz = p + ".gz"
        try:
            with io.open(p, "rb") as src, _gzip.open(gz, "wb", compresslevel=6) as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
            # WRITE FULLY, THEN REMOVE. The original is deleted only once the compressed file
            # exists and is non-empty -- a crash between the two must leave the log readable,
            # not leave a truncated .gz where the evidence used to be.
            if _size(gz) <= 0:
                raise IOError("empty archive")
            # Keep the original mtime: every age rule here, and newest_log() in capture_budget,
            # decide by mtime. A rewrite that stamps "now" would make the whole history look
            # freshly written and quietly exempt itself from every rule that follows.
            st = os.stat(p)
            os.utime(gz, (st.st_atime, st.st_mtime))
            os.remove(p)
            saved += before - _size(gz)
            done.append(os.path.relpath(gz, fleet_dir))
        except (OSError, IOError):
            try:
                if os.path.exists(gz) and os.path.exists(p):
                    os.remove(gz)
            except OSError:
                pass
            continue
    return saved, done


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


#: Registry entries younger than this are kept even when nothing links them: a fleet run
#: registers its conversations as it goes, and yanking a row from under a LIVE run would
#: unregister a conversation the run is still using.
CONV_KEEP_HOURS = float(os.environ.get("MCP_FLEET_CONV_HOURS", "24"))


def _linked_sessions(fleet_dir):
    """(sids, conv_urls) of every session that still has a row, or None if the store cannot
    be read -- and the caller then does nothing at all, because an unreadable session table
    would make EVERY registry row look unlinked and delete the lot.

    BOTH KEYS, because the registry rows do not all carry the same one. A chat row links by
    `name` = sid and its `url` is EMPTY; a fleet row carries an M365 url and no name. Matching
    on url alone looked like it worked and did not: an empty registry url compared equal to the
    539 sessions whose conv_url is also empty, so 14 rows counted as linked that were linked to
    nothing. Empties are dropped from both sets for that reason.
    """
    import sqlite3
    db = os.path.join(fleet_dir, "sessions", "sessions.sqlite3")
    if not os.path.isfile(db):
        return None
    try:
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute("SELECT sid, conv_url FROM sessions").fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    sids = {(r[0] or "").strip() for r in rows if (r[0] or "").strip()}
    urls = {(r[1] or "").strip() for r in rows if (r[1] or "").strip()}
    return sids, urls


def _guid(url):
    u = (url or "").strip()
    return u.rsplit("/conversation/", 1)[-1] if "/conversation/" in u else ""


def conversations(fleet_dir, now=None, dry_run=False, keep_hours=None):
    """Drop registry rows that no longer point at anything readable.

    WHY THIS EXISTS. .fleet/conversations.json is the THIRD place a conversation lives, beside
    the sessions table and the transcript file, and it is the one nothing was tidying. Measured
    on the live registry: 425 rows, of which 411 matched no session row -- 409 fleet worker
    conversations and 2 chats. The cockpit lists these, and opening one re-registers the session
    and brings it back into the chat, which is why deleted conversations reappeared. Deleting is
    already a hard DELETE of the row, its turns and both files; it was never a soft flag.

    They are also not a route to the stored history: fleet_turns is keyed by run and worker
    ("r6a8cfa11_w0"), and NONE of the 409 conversation GUIDs appear as a key. There is no join
    from a registry row to the data, so a row with no session behind it cannot reach anything
    locally. (The M365 URL may still open server-side; what is gone is any local record.)
    """
    now = time.time() if now is None else now
    keep_hours = CONV_KEEP_HOURS if keep_hours is None else keep_hours
    path = os.path.join(fleet_dir, "conversations.json")
    if not os.path.isfile(path):
        return 0, []
    got = _linked_sessions(fleet_dir)
    if got is None:
        # FAIL CLOSED. Every row looks unlinked when the table cannot be read, and acting on
        # that would empty the registry on exactly the failure it should be cautious about.
        return 0, []
    sids, linked = got
    guids = {_guid(u) for u in linked if _guid(u)}
    try:
        rows = json.load(io.open(path, encoding="utf-8-sig"))
    except Exception:
        return 0, []
    if not isinstance(rows, list):
        return 0, []
    before = _size(path)
    kept, dropped = [], []
    for r in rows:
        if not isinstance(r, dict):
            kept.append(r)
            continue
        url = (r.get("url") or "").strip()
        name = str(r.get("name") or "").strip()
        # sid FIRST, because that is how a chat row links and those are the operator's own
        # conversations. Then the whole url, then the GUID: the registry's url and the store's
        # conv_url are not always spelled the same (agent prefix, query string), and a match
        # that misses on punctuation deletes a row that IS linked.
        if name and name in sids:
            kept.append(r)
            continue
        if url and (url in linked or (_guid(url) and _guid(url) in guids)):
            kept.append(r)
            continue
        try:
            age_h = (now - float(r.get("ts") or 0)) / 3600.0
        except (TypeError, ValueError):
            age_h = 1e9
        if age_h <= keep_hours:
            kept.append(r)
            continue
        dropped.append(r.get("title") or url[:60])
    if dropped and not dry_run:
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as fh:
            json.dump(kept, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    return max(0, before - (_size(path) if not dry_run else 0)), dropped

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
    # COMPRESSION FIRST, DELETION SECOND. It is the rule that reaches the bulk -- the recent
    # 185 MB that age rules must not touch -- and running it first means the deletions that
    # follow are working on files already a fiftieth of their size.
    for name, fn in (("compress", compress),
                     ("coordinator_logs", coordinator_logs),
                     ("rotations", rotations),
                     ("scratch", scratch),
                     ("stores", stores),
                     ("conversations", conversations),
                     ("cap_jsonl", cap_jsonl)):
        try:
            if fn in (coordinator_logs, scratch, stores, compress, conversations):
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


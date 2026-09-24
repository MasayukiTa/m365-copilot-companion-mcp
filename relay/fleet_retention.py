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
import shutil
import subprocess
import sys
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

#: Subtrees of a named store that are WORKSPACES, not per-run output, and are never entered.
#:
#: swe/work holds the benchmark clones, their worktrees and pip target directories
#: (`_np123/numpy`, `<repo>-main/.git/objects/pack/...`) that runs reuse. Two things are wrong
#: with walking it under a per-file age rule, measured 2026-09-24:
#:
#:   * It is 101,170 files in 46,069 directories, and this rule stat()ed every one of them on
#:     every fleet start. That walk was the whole of the ~60 s between "fleet_runner started"
#:     and the run's `started` stamp (277 s on a cold copy; 24 s warm), with nothing deleted.
#:   * Were anything in it older than the window, the rule would delete it FILE BY FILE -- a
#:     pack file a clone has not rewritten in thirty days is still the repository, and the
#:     clone would be left corrupt rather than removed.
#:
#: Named, relative to the .fleet root, for the same fail-safe reason STORE_DIRS is.
STORE_SKIP = (os.path.join("swe", "work"),)

#: FILE_ATTRIBUTE_REPARSE_POINT. A junction is not a symlink to Python 3.10's is_symlink(), so
#: os.walk descended through one -- and an age rule following a junction deletes files that
#: are not in the store at all.
_REPARSE_POINT = 0x400


def _store_files(root, skip):
    """Every regular file under `root` as a DirEntry, without following links or junctions.

    NO stat() PER FILE. On Windows a DirEntry carries the size and times the directory listing
    already returned, so the age test below costs nothing extra; os.walk + getmtime cost one
    stat per file plus one lstat per directory, which is where the startup minute went.

    A directory that holds `.git` is a checkout and is one unit: it is never entered, because
    the only thing a per-file rule can do inside one is corrupt it.
    """
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        if any(e.name == ".git" for e in entries):
            continue
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                st = e.stat(follow_symlinks=False)
                if getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
                    continue
                if e.is_dir(follow_symlinks=False):
                    if os.path.normcase(os.path.abspath(e.path)) not in skip:
                        stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    yield e, st
            except OSError:
                continue


def stores(fleet_dir, now=None, dry_run=False, keep_days=None, names=None):
    """Age out per-run files inside the named stores.

    `sessions` is deliberately absent from STORE_DIRS: that directory holds the conversation
    database, which has its own retention with its own settings, and two policies on one store
    is how they come to disagree about what is still live. STORE_SKIP and any checkout are not
    entered either -- see there.
    """
    now = time.time() if now is None else now
    keep_days = _setting("fleet_store_days", STORE_KEEP_DAYS) if keep_days is None else keep_days
    skip = {os.path.normcase(os.path.abspath(os.path.join(fleet_dir, s))) for s in STORE_SKIP}
    freed, removed = 0, []
    for name in (names or STORE_DIRS):
        root = os.path.join(fleet_dir, name)
        if not os.path.isdir(root):
            continue
        for entry, st in _store_files(root, skip):
            # Never a database, wherever it turns up. The extension check is cheap and it
            # is the last line between an age rule and someone's history.
            if os.path.splitext(entry.name)[1].lower() in (".sqlite3", ".db", ".sqlite"):
                continue
            if (now - st.st_mtime) / 86400.0 <= keep_days:
                continue
            # CONFIRMED BEFORE IT IS DELETED. The listing's time is the directory's copy, which
            # NTFS may update late for a file another process holds open; a real stat on the
            # handful of candidates costs nothing and makes the deletion rest on the file.
            p = entry.path
            if _age_days(p, now) <= keep_days:
                continue
            freed += _rm(p, dry_run)
            removed.append(os.path.relpath(p, fleet_dir))
    return freed, removed


#: swe/work never shrinks. STORE_SKIP (above) stops the per-file age rule from entering it, and
#: that fix was correct as far as it went -- but "correct" there meant "does nothing", and
#: nothing else in this file touches that directory either. Measured 2026-09-24: 1.27 GB across
#: roughly a hundred thousand files, all of it benchmark clones, worktrees and pip targets that
#: fleet_runner leaves behind once a run finishes with them, and none of it has ever been freed.
#:
#: The unit of deletion here is DIFFERENT IN KIND from every rule above. Every other rule in
#: this file deletes individual files: a log, a rotation, a scratch file, one aged-out entry in
#: a per-run store. Deleting individual files out of the MIDDLE of a git checkout is exactly
#: the failure STORE_SKIP's docstring describes -- a pack file untouched for thirty days is
#: still the repository, and removing it file-by-file does not clean the clone, it corrupts it.
#: So this rule never deletes a file. It deletes a whole clone directory, as one atomic
#: `shutil.rmtree`, once the WHOLE of it -- not its own directory entry, but everything nested
#: inside it -- has gone untouched for the keep window, and only once nothing still running
#: references its path.
CLONE_KEEP_DAYS = float(os.environ.get("MCP_FLEET_CLONE_DAYS", "14"))

#: How often the clone sweep is actually allowed to RUN, regardless of how often apply() itself
#: is called. See the measurement note on workspace_clones() for why a walk over swe/work still
#: needs a floor under how often it happens at all, even off the critical path.
CLONE_SWEEP_HOURS = float(os.environ.get("MCP_FLEET_CLONE_SWEEP_HOURS", "24"))

#: Sidecar recording when the clone sweep last actually completed a walk over swe/work, so the
#: cost is amortized to at most once per CLONE_SWEEP_HOURS no matter how many times apply() runs
#: in between -- fleet_runner.py calls apply() at the start of EVERY fleet run.
_CLONE_SWEEP_STATE_NAME = "clone_sweep_state.json"

#: A clone mid-removal is renamed to this prefix BEFORE shutil.rmtree runs on it -- see
#: _clone_sweep_run_once()'s own comment for why. Dotted so it never collides with a real clone
#: name (benchmark harnesses do not check out repositories starting with a dot) and so a listing
#: of swe/work makes an in-flight deletion obvious at a glance.
_DELETING_PREFIX = ".deleting-"

#: The sweep holds this lock file for as long as it runs -- measured worst case on the synthetic
#: tree was under six minutes, so anything still holding the lock two hours later did not
#: release it because it is running, it did not release it because it crashed. Stolen rather
#: than honoured past this age, or a crash permanently wedges the sweep off.
_CLONE_SWEEP_LOCK_NAME = "clone_sweep.lock"
_CLONE_SWEEP_LOCK_STALE_HOURS = 2.0


def _clone_sweep_due(fleet_dir, now, sweep_hours):
    """Whether enough time has passed since the last COMPLETED walk to justify another one.

    Unreadable or missing state means "never swept" -- due, not skipped. A state file that
    cannot be parsed must not silently disable the sweep forever.
    """
    path = os.path.join(fleet_dir, _CLONE_SWEEP_STATE_NAME)
    try:
        data = json.load(io.open(path, encoding="utf-8-sig"))
        last = float(data.get("last_swept_ts") or 0)
    except Exception:
        return True
    return (now - last) / 3600.0 >= sweep_hours


def _record_clone_sweep(fleet_dir, now):
    """Stamp that the walk just ran. TMP-THEN-REPLACE, the same atomic idiom conversations()
    uses above: a crash mid-write must leave either the old stamp or the new one, never a
    truncated file that makes every future call see a corrupt state and re-walk needlessly (or,
    worse, a parse exception that some future caller mistakes for permission to skip a real
    sweep -- which is why _clone_sweep_due treats unreadable as due, not as skip)."""
    path = os.path.join(fleet_dir, _CLONE_SWEEP_STATE_NAME)
    tmp = path + ".tmp"
    try:
        with io.open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"last_swept_ts": now}, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def _clone_sweep_lock_acquire(fleet_dir, now):
    """Atomically claim the right to run the sweep, or return None if someone already has it.

    O_CREAT|O_EXCL is the whole mechanism: the OS refuses the second create, so two sweeps
    racing to start -- two fleets starting within the same throttle window, each deciding the
    sweep is due before either has recorded completion -- can never both believe they hold it.
    This is the ONLY thing that makes "two concurrent triggers run one sweep" true; the due-check
    in workspace_clones() is not atomic across processes and cannot be the safety rail by itself.
    """
    path = os.path.join(fleet_dir, _CLONE_SWEEP_LOCK_NAME)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age_h = (now - os.path.getmtime(path)) / 3600.0
        except OSError:
            age_h = 0.0
        if age_h < _CLONE_SWEEP_LOCK_STALE_HOURS:
            return None
        # STALE, NOT HELD. A prior sweep process died holding this; refusing to ever steal it
        # would mean one crash disables clone cleanup forever, which is worse than the small
        # chance of overlapping a genuinely-still-running sweep past its measured worst case.
        try:
            os.remove(path)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return None
    except OSError:
        # fleet_dir itself missing or unwritable -- nothing to lock. apply() already checks
        # fleet_dir exists before any rule runs, so in production this branch is defensive;
        # a direct caller (a test, or a manual CLI invocation) gets a clean "did not sweep"
        # rather than a raised exception.
        return None
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    except OSError:
        pass
    os.close(fd)
    return path


def _clone_sweep_lock_release(lock_path):
    try:
        os.remove(lock_path)
    except OSError:
        pass


def _newest_mtime_and_size(root):
    """The newest mtime and total byte size found anywhere under `root`, recursively, never
    following a symlink or a junction.

    REUSES _store_files()'s NO-STAT-PER-FILE TECHNIQUE RATHER THAN A SECOND PASS. On Windows a
    DirEntry's stat(follow_symlinks=False) is populated from the directory listing NtQuery
    already returned; os.walk() + os.path.getmtime() costs a real stat() syscall per file and
    per directory, which is the exact cost STORE_SKIP exists to keep out of swe/work. This
    function is the one place this rule is allowed to look at every file in a clone -- finding
    the true recursive newest mtime has no cheaper definition -- so it must not also be the
    place that reintroduces a stat-per-file walk.

    A clone touched two days ago by a `pip install` in a venv six directories down is NOT stale
    even though the clone's own top-level directory entry has not changed in months; that is
    why the caller cannot use the top-level entry's own mtime and must call this.

    FILE MTIMES ONLY, NOT DIRECTORY MTIMES. A directory's own mtime is bumped by adding or
    removing an entry inside it -- which is exactly what writing a new file already does, at
    the same instant -- so a real write is never missed by looking at files alone. Counting
    directory mtimes too would add nothing a file mtime does not already cover, while making
    the result depend on directory-entry churn (a rename, a delete) that leaves no file behind
    to justify calling the clone anything but stale.
    """
    newest = 0.0
    total = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                st = e.stat(follow_symlinks=False)
                if getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    if st.st_mtime > newest:
                        newest = st.st_mtime
                    total += st.st_size
            except OSError:
                continue
    return newest, total


def _default_path_in_use(path):
    """True/False/None: whether any LIVE process's command line names `path` -- None when the
    question could not be answered at all.

    Mirrors orphan_reaper.candidates()'s own technique (Win32_Process via CimInstance, matched
    case-insensitively against the command line) because that module already established it as
    the only real signal on Windows: fleet_runner's ACTIVE_MARKER records pid/argv/start_ts but
    never a workspace path, so there is no marker to read here, only the process table itself.

    None on ANY failure -- PowerShell not found, WMI refusing the query, a timeout, malformed
    JSON -- so the caller can fail closed exactly as _linked_sessions() does above: an
    unanswerable question about what is running must never be treated as "nothing is running".
    """
    norm = os.path.normcase(os.path.abspath(path))
    ps = "Get-CimInstance Win32_Process | Select-Object CommandLine | ConvertTo-Json -Compress"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        if r.returncode != 0:
            return None
        rows = json.loads(r.stdout or "[]")
    except Exception:
        return None
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return None
    for row in rows:
        try:
            cmd = row.get("CommandLine") or ""
        except AttributeError:
            return None
        if norm in os.path.normcase(cmd):
            return True
    return False


def _finish_leftover_deletes(work_root, dry_run):
    """Finish any `.deleting-*` directory a prior sweep renamed but never got to rmtree.

    Nothing checks age or in-use here: something already decided, in a PREVIOUS sweep, that
    this directory should go, and the rename already took it out from under whatever path any
    live process could still be watching. Re-deciding would only re-run a check that already
    passed once and cannot un-happen.
    """
    removed = []
    try:
        with os.scandir(work_root) as it:
            entries = list(it)
    except OSError:
        return removed
    for e in entries:
        if not e.name.startswith(_DELETING_PREFIX):
            continue
        try:
            # A .deleting- entry is always something THIS module renamed from a real
            # directory it had already verified was not a junction; the checks below are
            # defensive, not load-bearing, in case anything else ever lands a file matching
            # the prefix.
            if e.is_symlink():
                continue
            st = e.stat(follow_symlinks=False)
            if getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
                continue
        except OSError:
            continue
        if dry_run:
            removed.append(e.name)
            continue
        try:
            shutil.rmtree(e.path)
            removed.append(e.name)
        except OSError:
            continue
    return removed


def _clone_sweep_run_once(fleet_dir, keep_days, in_use, now, dry_run=False):
    """Do the actual walk-and-delete over swe/work, synchronously, in whatever process calls
    it. This is the function a background process (or a test) runs; workspace_clones() itself
    never calls the parts of this that cost real time -- see its own docstring for why.

    LOCKED FOR THE WHOLE CALL, not dry_run. O_CREAT|O_EXCL on a lock file is the only thing
    that makes "two concurrent triggers run one sweep" true: two fleet starts landing in the
    same throttle window can both decide a sweep is due and both launch one, and the decision
    that only one of them actually runs has to be made by whichever of them gets here first,
    atomically, not by whichever of them decided to launch first (that race is not atomic
    across processes). The loser returns immediately, having touched nothing.

    RENAME BEFORE RMTREE, on every deletion. shutil.rmtree() is not atomic -- it is thousands of
    individual unlink() calls on a big clone -- and this function is meant to run detached,
    where nothing guarantees it finishes: the process can be killed, the machine can sleep, a
    disk fault can interrupt it partway through. A directory caught mid-rmtree under its ORIGINAL
    name is ambiguous: is it a live clone that happens to be missing files, or a deletion that
    stopped short? Renamed to `.deleting-<name>-<ts>` FIRST, the ambiguity is gone -- nothing
    still watching the original path can find it there any more (the in-use check already
    passed before the rename), and nothing mistakes a `.deleting-*` name for a live clone,
    including this function on its own next call, which instead finishes it unconditionally via
    _finish_leftover_deletes() before it looks for anything new to remove.
    """
    now = time.time() if now is None else now
    freed, removed = 0, []
    lock_path = None
    if not dry_run:
        lock_path = _clone_sweep_lock_acquire(fleet_dir, now)
        if lock_path is None:
            return 0, []
    try:
        work_root = os.path.join(fleet_dir, "swe", "work")
        if not os.path.isdir(work_root):
            return freed, removed

        removed.extend(_finish_leftover_deletes(work_root, dry_run))

        try:
            with os.scandir(work_root) as it:
                children = list(it)
        except OSError:
            return freed, removed

        for e in children:
            if e.name.startswith(_DELETING_PREFIX):
                continue    # already handled above
            try:
                if e.is_symlink():
                    continue
                st = e.stat(follow_symlinks=False)
                if getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
                    continue
                if not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue

            # The top-level directory's OWN mtime is deliberately not consulted here, for the
            # same reason _newest_mtime_and_size() does not track directory mtimes below it: it
            # is a listing entry, not a file, and every real write inside the clone is already
            # visible as a file mtime.
            newest, size = _newest_mtime_and_size(e.path)
            if (now - newest) / 86400.0 <= keep_days:
                continue

            used = in_use(e.path)
            if used or used is None:
                # FAIL CLOSED -- see _default_path_in_use()'s own docstring. `used is None`
                # means the question could not be answered at all, and that is not a reason
                # to guess.
                continue

            if dry_run:
                freed += size
                removed.append(e.name)
                continue

            deleting_path = os.path.join(
                work_root, "%s%s-%d" % (_DELETING_PREFIX, e.name, int(now)))
            try:
                os.rename(e.path, deleting_path)
            except OSError:
                continue
            try:
                shutil.rmtree(deleting_path)
            except OSError:
                # Renamed but not fully removed -- exactly the state _finish_leftover_deletes()
                # exists to clean up on the NEXT sweep. Still counted as freed/removed here:
                # the clone's original name is already gone, which is the part that matters to
                # a caller asking "is this still a live clone".
                pass
            freed += size
            removed.append(e.name)

        if not dry_run:
            _record_clone_sweep(fleet_dir, now)
        return freed, removed
    finally:
        if lock_path is not None:
            _clone_sweep_lock_release(lock_path)


def _spawn_clone_sweep_subprocess(fleet_dir, keep_days, now):
    """The production launcher: a detached, low-priority child process that runs the sweep and
    exits on its own. Never waited on -- Popen() returns as soon as the child is created, which
    is the whole point: workspace_clones() must return before the sweep has done anything.

    A SEPARATE PROCESS, not a thread. A thread dies with its parent -- if fleet_runner's own
    process is what apply() runs inside and that process later exits or is killed (a fleet run
    ending, a supervisor restart), a thread doing the sweep dies with it, silently, and the next
    apply() sees `not due` was never true so does nothing until the throttle window closes on
    its own. A separate process, once launched, survives the launcher exiting.
    """
    try:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) |
                             getattr(subprocess, "DETACHED_PROCESS", 0) |
                             getattr(subprocess, "IDLE_PRIORITY_CLASS", 0))
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        subprocess.Popen(
            [sys.executable, "-m", "relay.fleet_retention", "--clone-sweep-worker",
             "--dir", fleet_dir, "--keep-days", str(keep_days), "--now", str(now)],
            cwd=repo_root, creationflags=creationflags, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        # Launching is best-effort. A failed launch leaves the throttle state untouched, so
        # the NEXT due apply() call tries again rather than this one raising into fleet_runner.
        pass


def workspace_clones(fleet_dir, now=None, dry_run=False, keep_days=None, sweep_hours=None,
                     in_use=None, launcher=None):
    """Remove whole benchmark clones/workspaces under swe/work, once each is both old and
    unused -- but never do the removing (or even the walk that finds candidates) IN this call.

    OFF THE CRITICAL PATH. Measured on a synthetic tree of the same shape as the real swe/work
    (~100k files, ~60k dirs, 40 clones): the discovery walk alone is ~52 s and a walk that also
    deletes is ~340 s. apply() runs at the start of EVERY fleet start, so doing either of those
    inline is exactly the cost f7571a1 removed, reintroduced. This function instead only ever
    does two cheap things before returning: read the small throttle state file to decide
    whether a sweep is due, and, if so, hand off to `launcher` (real work happens in
    _clone_sweep_run_once(), run by the launcher, never by this function). Both cost
    microseconds; neither touches swe/work.

    `launcher(fleet_dir, keep_days, now)` defaults to _spawn_clone_sweep_subprocess(), a
    detached child process with its own lock file (_clone_sweep_lock_acquire(), inside
    _clone_sweep_run_once()) so two callers deciding a sweep is due at the same moment still
    only run one sweep between them. Tests inject a launcher that runs the same worker
    function in-thread instead, so they can wait on it deterministically without spawning a
    real process or invoking PowerShell.

    dry_run BYPASSES THE LAUNCHER ENTIRELY and runs synchronously in this process. A dry run
    exists to be read back immediately by its own caller (the --apply CLI printout, or a test
    inspecting the report) and must never mutate anything, so there is no background process
    for it to race with and no reason to defer it; see _clone_sweep_run_once(dry_run=True).

    Returns (freed_bytes, [clone names]): for dry_run, what a real sweep would do right now;
    otherwise (0, []) whether or not a sweep was actually launched -- the caller gets the
    result later, from the sidecar state file's `last_swept_ts`, not from this call.
    """
    now = time.time() if now is None else now
    keep_days = _setting("fleet_clone_days", CLONE_KEEP_DAYS) if keep_days is None else keep_days
    sweep_hours = (_setting("fleet_clone_sweep_hours", CLONE_SWEEP_HOURS)
                  if sweep_hours is None else sweep_hours)

    if dry_run:
        use = _default_path_in_use if in_use is None else in_use
        return _clone_sweep_run_once(fleet_dir, keep_days, use, now, dry_run=True)

    if not _clone_sweep_due(fleet_dir, now, sweep_hours):
        return 0, []

    launch = launcher if launcher is not None else _spawn_clone_sweep_subprocess
    launch(fleet_dir, keep_days, now)
    return 0, []


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
        keep = int(limit * JSONL_TRIM_FRACTION)
        if dry_run:
            freed += before - keep
            trimmed.append(n)
            continue
        if _keep_tail(p, before, keep):
            freed += before - _size(p)
            trimmed.append(n)
    return freed, trimmed


#: A capped ledger is cut to this fraction of the ceiling, not to the ceiling itself.
#:
#: Cut to exactly the ceiling, a ledger that is written every day sits a few hundred KB over it
#: at every start, and was rewritten whole at every start: measured 2026-09-24, tool_events.jsonl
#: at 64.0 MB, "freed 0.1 MB" -- 64 MB read and 64 MB written to free 0.1. With headroom the
#: rewrite happens once per tenth of the ceiling written, and the tail kept is still 57 MB.
JSONL_TRIM_FRACTION = float(os.environ.get("MCP_FLEET_JSONL_TRIM_FRACTION", "0.9"))

#: Bytes held in memory at once while a tail is moved.
_COPY_CHUNK = 1024 * 1024


def _keep_tail(path, size, keep):
    """Rewrite `path` as its last `keep` bytes, starting on a line boundary. True if it did.

    STREAMED, NOT READ WHOLE. The first version read the tail into one bytes object -- the full
    64 MB ceiling, resident in the fleet process at every start, for the length of a disk write.
    This moves it a megabyte at a time into a sibling file and swaps it in, so the peak is one
    chunk. If the swap is refused (another process holds the ledger open without delete
    sharing), the tail is streamed back over the original instead, which is what the in-memory
    version did too, just without holding it.
    """
    tmp = path + ".captmp"
    try:
        with io.open(path, "rb") as src:
            src.seek(size - keep)
            # Land on a line boundary: half a JSON object at the head of the file is a
            # parse error for every reader, which is worse than the bytes it saved.
            src.readline()
            with io.open(tmp, "wb") as dst:
                while True:
                    chunk = src.read(_COPY_CHUNK)
                    if not chunk:
                        break
                    dst.write(chunk)
        try:
            os.replace(tmp, path)
            return True
        except OSError:
            pass
        with io.open(tmp, "rb") as src, io.open(path, "wb") as dst:
            while True:
                chunk = src.read(_COPY_CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
        return True
    except OSError:
        return False
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


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
                     ("workspace_clones", workspace_clones),
                     ("conversations", conversations),
                     ("cap_jsonl", cap_jsonl)):
        try:
            if fn in (coordinator_logs, scratch, stores, compress, conversations,
                     workspace_clones):
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
    # INTERNAL. This is the entry point _spawn_clone_sweep_subprocess() launches as a detached
    # child process -- not meant to be typed by a person, though nothing stops it. Kept as a
    # plain CLI flag rather than a separate script because a separate script is one more file
    # whose relative import path could drift out of sync with this one.
    ap.add_argument("--clone-sweep-worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--keep-days", type=float, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.clone_sweep_worker:
        fleet_dir = args.dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".fleet")
        keep_days = (args.keep_days if args.keep_days is not None
                    else _setting("fleet_clone_days", CLONE_KEEP_DAYS))
        now = args.now if args.now is not None else time.time()
        freed, removed = _clone_sweep_run_once(fleet_dir, keep_days, _default_path_in_use, now)
        print("clone sweep: %.1f MB freed, %d removed" % (freed / 1048576.0, len(removed)))
        raise SystemExit(0)

    rep = apply(fleet_dir=args.dir, dry_run=not args.apply)
    print("%s  %s" % (rep["dir"], "(dry run)" if rep["dry_run"] else ""))
    for name, r in rep["rules"].items():
        if "error" in r:
            print("  %-20s ERROR %s" % (name, r["error"]))
        else:
            print("  %-20s %6.1f MB  %d item(s)" % (name, r["freed_mb"], r["count"]))
    print("  %-20s %6.1f MB total" % ("", rep["freed_mb"]))


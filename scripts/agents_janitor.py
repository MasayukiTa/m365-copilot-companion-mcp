# -*- coding: utf-8 -*-
"""Reclaim C: by deleting FINISHED worker clones under %TEMP%\\m365-companion\\agents.

WHY THIS EXISTS. Measured 2026-11-09: free space on C: oscillated between 0.3 and 2.5 GB and
fell from 1.5 GB to 0.25 GB in twenty minutes (22:00-22:20). .fleet/disk_series.jsonl named the
writers: node.exe doing node_modules-style installs (~345 MB a time), svchost's WpnUserService,
and `git clone` of GitHub repos that fleet workers make into %TEMP%\\m365-companion\\agents. A
worker clones the repo, does its work, and exits -- and its clone is NEVER removed. Each is a
full checkout (this repo alone updates 2153 files on clone), so a few dozen finished workers can
exhaust the disk on their own. This is the ROOT CAUSE of the fleet running C: out of space.

WHAT IT DOES. It walks the agents directory and deletes a child folder ONLY when ALL of these
hold -- the conjunction is the whole safety argument, so each clause is checked independently
and a single failure keeps the folder:

  1. `git status --porcelain` is empty                 -> no uncommitted or untracked work
  2. no local commit is missing from the remotes       -> nothing unpushed would be lost
  3. no LIVE worker in .fleet/status.json names it      -> no running worker owns it
  4. nothing inside was modified in the last 60 minutes -> it is quiet, not mid-run

If ANY check cannot be made (git unavailable, status.json unreadable, a stat fails), the folder
is KEPT. "I could not tell" is treated as "do not delete", because the cost of a wrong delete
(losing uncommitted work) is unbounded and the cost of a wrong keep is one stale clone.

DRY RUN BY DEFAULT. With no flags it reports what it WOULD delete and changes nothing. `--apply`
is required to delete. Either way it prints free space before and after and writes one log line
per folder saying what it decided and why.

Never deletes its own clone, and never deletes uncommitted work -- clause 1 guarantees the
second and the janitor folder fails clause 3/4 for itself while it runs besides.

Stdlib only and import-safe: the supervisor calls main() in-process on a tick.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time


#: Folders that are shared infrastructure, not disposable worker clones. The janitor NEVER
#: touches these by name, independent of their git state -- e.g. `repo`/`_base` are base clones
#: workers branch from, `shared` is shared scratch, `claude`/`m365-work`/`rewrite` are tooling
#: checkouts. Deleting any of them would break live and future workers, so they are refused
#: before any other check runs.
PROTECTED_NAMES = frozenset({"repo", "shared", "rewrite", "_base", "claude", "m365-work"})


#: A folder is "quiet" when nothing in it changed in this many seconds. 60 minutes: long enough
#: that a worker which merely paused between turns is not mistaken for a finished one, short
#: enough that a genuinely finished clone is reclaimed on the next sub-3GB tick.
QUIET_SECONDS = 60 * 60

#: How deep to walk when deciding "nothing was modified recently". A clone with node_modules has
#: tens of thousands of files; we stop early once we find ONE recent file (that alone keeps the
#: folder), and otherwise cap the walk so a single call cannot run unbounded.
_MAX_STAT_FILES = 200_000


def agents_root() -> str:
    """%TEMP%\\m365-companion\\agents. Honours MCP_AGENTS_ROOT for tests."""
    override = os.environ.get("MCP_AGENTS_ROOT")
    if override:
        return override
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or os.path.expanduser("~")
    return os.path.join(temp, "m365-companion", "agents")


def free_bytes(path: str) -> int | None:
    """Free bytes on the volume holding `path`, or None if it cannot be read."""
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return None


def _gb(n: int | None) -> str:
    return "unknown" if n is None else "%.3f GB" % (n / (1024.0 ** 3))


def _git(folder: str, *args: str) -> subprocess.CompletedProcess | None:
    """Run git inside `folder`. Returns the completed process, or None if git could not run.

    A None return (git missing, timeout, OSError) must be read by the caller as "cannot tell",
    never as "clean".
    """
    try:
        return subprocess.run(
            ["git", "-C", folder, *args],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def is_git_repo(folder: str) -> bool:
    r = _git(folder, "rev-parse", "--is-inside-work-tree")
    return bool(r is not None and r.returncode == 0 and r.stdout.strip() == "true")


def working_tree_clean(folder: str) -> bool | None:
    """True when `git status --porcelain` is empty. None when it cannot be determined."""
    r = _git(folder, "status", "--porcelain")
    if r is None or r.returncode != 0:
        return None
    return r.stdout.strip() == ""


def all_commits_pushed(folder: str) -> bool | None:
    """True when no local commit is missing from the remotes. None when undeterminable.

    `git log --branches --not --remotes` lists commits reachable from a local branch but from no
    remote-tracking branch -- i.e. work that exists only here. Empty output means everything is
    on a remote. If there are no remotes at all, every local commit is unpushed, so this is
    False (keep the folder) rather than vacuously True.
    """
    remotes = _git(folder, "remote")
    if remotes is None or remotes.returncode != 0:
        return None
    if remotes.stdout.strip() == "":
        return False  # no remote -> nothing is known-safe; keep
    r = _git(folder, "log", "--branches", "--not", "--remotes", "--format=%H")
    if r is None or r.returncode != 0:
        return None
    return r.stdout.strip() == ""


def live_worker_names(status_path: str) -> set[str] | None:
    """Folder-name tokens mentioned by any LIVE worker in .fleet/status.json.

    Returns a set of strings that appear in live workers' fields (run_id, cwd, clone path,
    worker name, ...). A folder is protected when its basename is a substring of, or equal to,
    any of these. Returns None when the file exists but cannot be read/parsed -- the caller then
    protects EVERY folder, because a status file we cannot read might name any of them.

    A MISSING status file is not an error: it means no fleet is running, so no worker is live.
    That returns an empty set (nothing protected on this ground).
    """
    if not os.path.exists(status_path):
        return set()
    try:
        with open(status_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    tokens: set[str] = set()

    def _terminal(w: dict) -> bool:
        state = str(w.get("state", w.get("status", ""))).lower()
        return state in {"done", "stuck", "failed", "error", "cancelled", "canceled", "terminal"}

    workers = []
    if isinstance(data, dict):
        for key in ("workers", "live", "roster"):
            v = data.get(key)
            if isinstance(v, list):
                workers.extend(v)
    elif isinstance(data, list):
        workers = data
    for w in workers:
        if not isinstance(w, dict):
            continue
        if _terminal(w):
            continue
        for field in ("run_id", "cwd", "clone", "clone_path", "path", "folder",
                      "worker", "name", "dir", "workdir"):
            val = w.get(field)
            if isinstance(val, str) and val.strip():
                tokens.add(val.strip())
    return tokens


def _mentioned_by_live(folder_name: str, folder_path: str, tokens: set[str]) -> bool:
    norm_path = os.path.normcase(os.path.abspath(folder_path))
    for tok in tokens:
        if folder_name and folder_name in tok:
            return True
        try:
            if norm_path in os.path.normcase(os.path.abspath(tok)):
                return True
        except (OSError, ValueError):
            continue
    return False


def recently_modified(folder: str, now: float | None = None,
                      quiet_seconds: int = QUIET_SECONDS) -> bool | None:
    """True when ANY file under `folder` was modified within quiet_seconds. None on stat failure.

    Stops at the first recent file. A single unreadable entry makes the answer None (cannot tell
    -> keep), because a file we could not stat might be the one that is still being written.
    """
    if now is None:
        now = time.time()
    cutoff = now - quiet_seconds
    seen = 0
    try:
        root_mtime = os.path.getmtime(folder)
    except OSError:
        return None
    if root_mtime >= cutoff:
        return True
    for dirpath, dirnames, filenames in os.walk(folder):
        # Skip .git: our own `git status`/`git log` (and git's own housekeeping) touch files
        # under .git, so its mtimes reflect THIS scan, not the worker's activity. Worker changes
        # to tracked files are caught by clause 1 (git status --porcelain); clause 4 is about
        # whether the WORKING TREE is quiet, so .git metadata must not keep an idle clone alive.
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            seen += 1
            if seen > _MAX_STAT_FILES:
                return None
            fp = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(fp) >= cutoff:
                    return True
            except OSError:
                try:
                    if os.path.lexists(fp):
                        return None
                except OSError:
                    return None
        for name in dirnames:
            dp = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(dp) >= cutoff:
                    return True
            except OSError:
                return None
    return False


def evaluate(folder_path: str, status_tokens: set[str] | None,
             self_path: str | None = None, now: float | None = None) -> tuple[bool, str]:
    """Decide whether `folder_path` MAY be deleted. Returns (deletable, reason).

    reason is a short machine-stable token suitable for a log line. The checks are ordered so the
    cheapest, most decisive ones run first; the first failing one wins.
    """
    name = os.path.basename(os.path.normpath(folder_path))

    if name in PROTECTED_NAMES:
        return False, "protected-name"

    if self_path is not None:
        try:
            if os.path.normcase(os.path.abspath(folder_path)) == \
               os.path.normcase(os.path.abspath(self_path)):
                return False, "is-the-janitor-clone"
        except (OSError, ValueError):
            return False, "self-path-uncertain"

    if not os.path.isdir(folder_path):
        return False, "not-a-directory"

    if status_tokens is None:
        return False, "status-unreadable"
    if _mentioned_by_live(name, folder_path, status_tokens):
        return False, "named-by-live-worker"

    if not is_git_repo(folder_path):
        return False, "not-a-git-repo"

    clean = working_tree_clean(folder_path)
    if clean is None:
        return False, "status-check-failed"
    if not clean:
        return False, "working-tree-dirty"

    pushed = all_commits_pushed(folder_path)
    if pushed is None:
        return False, "push-check-failed"
    if not pushed:
        return False, "has-unpushed-commits"

    recent = recently_modified(folder_path, now=now)
    if recent is None:
        return False, "mtime-check-failed"
    if recent:
        return False, "modified-within-60min"

    return True, "deletable"


def iter_agent_folders(root: str, self_path: str | None) -> list[str]:
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return []
    out = []
    for e in entries:
        try:
            if not e.is_dir():
                continue
        except OSError:
            continue
        out.append(e.path)
    return out


def run(apply: bool, root: str | None = None, self_path: str | None = None,
        status_path: str | None = None, log=print, now: float | None = None) -> dict:
    """Scan the agents root and delete (or, in dry run, report) finished clones.

    Returns a summary dict. `log` is called with one string per folder and for the free-space
    lines, so the supervisor can route it into its own log.
    """
    root = root or agents_root()
    if self_path is None:
        self_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if status_path is None:
        status_path = os.path.join(self_path, ".fleet", "status.json")

    mode = "APPLY" if apply else "DRY-RUN"
    before = free_bytes(root) if os.path.isdir(root) else free_bytes(os.path.dirname(root))
    log("[agents-janitor] %s root=%s free-before=%s" % (mode, root, _gb(before)))

    status_tokens = live_worker_names(status_path)

    deleted, kept, freed = [], [], 0
    for folder in iter_agent_folders(root, self_path):
        name = os.path.basename(folder)
        deletable, reason = evaluate(folder, status_tokens, self_path=self_path, now=now)
        if not deletable:
            kept.append((name, reason))
            log("[agents-janitor] KEEP   %s (%s)" % (name, reason))
            continue
        if apply:
            size = _folder_size(folder)
            try:
                shutil.rmtree(folder, onerror=_force_rw)
                freed += size
                deleted.append((name, reason))
                log("[agents-janitor] DELETE %s (%s, ~%s)" % (name, reason, _gb(size)))
            except OSError as exc:
                kept.append((name, "delete-failed:%s" % type(exc).__name__))
                log("[agents-janitor] KEEP   %s (delete-failed: %s)" % (name, exc))
        else:
            deleted.append((name, reason))
            log("[agents-janitor] WOULD-DELETE %s (%s)" % (name, reason))

    after = free_bytes(root) if os.path.isdir(root) else free_bytes(os.path.dirname(root))
    log("[agents-janitor] %s done deleted=%d kept=%d free-after=%s"
        % (mode, len(deleted), len(kept), _gb(after)))
    return {"mode": mode, "deleted": deleted, "kept": kept,
            "free_before": before, "free_after": after, "freed_estimate": freed}


def _folder_size(folder: str) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(folder):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _force_rw(func, path, _exc):
    """shutil.rmtree onerror: clear read-only (git packs are read-only on Windows) and retry."""
    import stat
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete. Without this, dry run: report only, change nothing.")
    ap.add_argument("--root", default=None,
                    help="override the agents root (default %%TEMP%%\\m365-companion\\agents).")
    ap.add_argument("--status", default=None,
                    help="path to .fleet/status.json (default: this checkout's).")
    args = ap.parse_args(argv)
    summary = run(apply=args.apply, root=args.root, status_path=args.status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

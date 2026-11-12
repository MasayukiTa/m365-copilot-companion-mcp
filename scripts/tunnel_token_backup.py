# -*- coding: utf-8 -*-
"""Keep a private copy of the Dev Tunnel token cache, and restore it when the live file is empty.

WHY THIS EXISTS (measured 2026-11-11/12)
----------------------------------------
The owner had to redo the Dev Tunnel sign-in four times in one night although the design
is that one sign-in persists. The MSAL token cache
``%LOCALAPPDATA%\\DevTunnels\\devtunnels-tokens-microsoft`` was valid at 4182 B (20:44)
and 4214 B (00:33) and then became 0 B at 00:55:47 with no sign-in window open. An MSAL
cache write is truncate-then-write, so a crash or a second concurrent writer between the
truncate and the write leaves the file at 0 B -- and a 0-byte cache reads as "logged out",
forcing a fresh browser sign-in.

Cause-and-concurrency is handled elsewhere (ranges A-C of this task). THIS module is the
safety net for range (D): while the cache is healthy it is copied to a private folder under
the user profile; when the live file is found at 0 B and no ``devtunnel`` login process is
running, the last healthy copy is restored, so the persisted sign-in survives a zeroing
without a human re-authenticating.

DESIGN
------
* The two decisions are PURE functions (``should_backup`` / ``should_restore``): no I/O, no
  clock, no process calls, mirroring the shape the supervisor's other gates use
  (scripts/supervisor_host_launch.decide_host_launch, scripts/stale_server_check). The
  caller measures the sizes and whether a login process is running and acts on the one word
  each returns. This is what the tests pin on every platform.
* The I/O helpers (``do_backup`` / ``do_restore``) copy atomically: write to a temp file in
  the destination directory, then ``os.replace`` onto the target. A copy that is interrupted
  never leaves a half-written backup or a half-written live cache -- the same truncate-then-
  write hazard that zeroes the original must not be reproduced by the safety net.
* The backup lives under ``%LOCALAPPDATA%\\m365-companion\\devtunnel-token-backup`` (see
  ``default_backup_dir``): the user profile, NOT the repo, NOT H:, NOT SharePoint, per the
  task's storage rule. Only one generation is kept (``<name>`` and ``<name>.prev``) so a
  single bad copy cannot overwrite the only good one in place.

WHAT "HEALTHY" MEANS. A cache larger than ``MIN_HEALTHY_BYTES`` (1000 B) is treated as a
real token cache worth copying. The two observed healthy sizes were 4182 B and 4214 B; the
broken state was exactly 0 B. 1000 B is well below the smallest healthy size and far above
zero, so a truncated-but-nonzero cache is neither backed up (it is not clearly healthy) nor
mistaken for the empty state that triggers a restore.
"""
from __future__ import annotations

import os
import errno

#: A live cache at least this many bytes is treated as healthy and worth copying. The two
#: measured healthy sizes were 4182 B and 4214 B; the broken size was 0 B. 1000 B sits well
#: below the healthy sizes and far above zero. Chosen as a floor, not a guess at the exact
#: cache size, which varies with the number of cached accounts/scopes.
MIN_HEALTHY_BYTES = 1000

#: The MSAL token cache file name the Dev Tunnel CLI uses under %LOCALAPPDATA%\DevTunnels.
#: Named here so the supervisor and the tests agree on exactly one file; the directory is
#: resolved separately so a test can point at a COPY of the cache dir (never the live one).
TOKEN_CACHE_NAME = "devtunnels-tokens-microsoft"


def should_backup(live_size):
    """True when the live cache is healthy enough to copy.

    ``live_size`` is the size in bytes of the live token cache, or ``None`` when the file does
    not exist / cannot be stat'd. Only a file strictly larger than ``MIN_HEALTHY_BYTES`` is
    copied: a missing, zero, or merely-truncated cache is never promoted into the backup,
    because overwriting the one good copy with a bad one is the failure this guards against.
    """
    if live_size is None:
        return False
    return live_size > MIN_HEALTHY_BYTES


def should_restore(live_size, login_running, backup_size):
    """True when the live cache should be overwritten from the backup.

    Restore fires only when ALL of these hold:

      * ``live_size == 0`` -- the live cache is exactly the broken, zeroed state. A missing
        file (``None``) is NOT restored here: absence is a fresh/clean install, not the
        truncate-then-write zeroing this recovers from, and silently materialising a cache
        where the product expects none is out of scope. A non-zero live cache is left alone.
      * ``not login_running`` -- no ``devtunnel`` login/sign-in process is currently running.
        A sign-in in flight is itself truncating-then-writing the cache; restoring underneath
        it would race that writer and could hand the CLI a half-old cache. When a login is
        running we wait for it instead.
      * ``backup_size`` is healthy (``> MIN_HEALTHY_BYTES``) -- there is a known-good copy to
        restore from. A missing (``None``) or unhealthy backup gives nothing to restore, so
        the broken state is reported, not papered over with an empty file.
    """
    if login_running:
        return False
    if live_size != 0:
        return False
    if backup_size is None or backup_size <= MIN_HEALTHY_BYTES:
        return False
    return True


class CopyWriteError(OSError):
    """A destination write or flush failed during an atomic copy."""


def is_disk_full_write(error):
    """Classify only destination write errors; never inspect drive free space."""
    return isinstance(error, CopyWriteError) and (
        error.errno == errno.ENOSPC or getattr(error, "winerror", None) == 112
    )


def run_cycle(cache_dir, backup_dir, login_running):
    """Return independently reported backup and restore outcomes for one retryable cycle."""
    def attempt(action):
        try:
            return action()
        except OSError as exc:
            return "failed-disk-full" if is_disk_full_write(exc) else "failed-io"

    backup = attempt(lambda: do_backup(cache_dir, backup_dir))
    restore = attempt(lambda: do_restore(cache_dir, backup_dir, login_running))
    return backup, restore


def default_cache_dir(localappdata=None):
    """The directory the live Dev Tunnel token cache lives in.

    ``%LOCALAPPDATA%\\DevTunnels``. ``localappdata`` overrides the environment so a test can
    point the whole module at a COPY of the cache directory and never touch the live one.
    Returns ``None`` when LOCALAPPDATA is unknown (e.g. a non-Windows CI runner), so callers
    fail closed rather than inventing a path.
    """
    base = localappdata if localappdata is not None else os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return os.path.join(base, "DevTunnels")


def default_backup_dir(localappdata=None):
    """The private folder the healthy copy is kept in.

    ``%LOCALAPPDATA%\\m365-companion\\devtunnel-token-backup`` -- under the user profile, not
    the repo, not H:, not SharePoint (the task's storage rule). ``None`` when LOCALAPPDATA is
    unknown, same fail-closed contract as ``default_cache_dir``.
    """
    base = localappdata if localappdata is not None else os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return os.path.join(base, "m365-companion", "devtunnel-token-backup")


def _size_or_none(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _atomic_copy(src, dst):
    """Copy ``src`` onto ``dst`` atomically: temp file in dst's dir, then ``os.replace``.

    The temp file is created in the destination directory so ``os.replace`` is a same-volume
    rename (atomic on both NTFS and POSIX). An interrupted copy leaves only the temp file,
    never a half-written ``dst`` -- the truncate-then-write hazard that zeroes the original
    must not be reproduced by this safety net.
    """
    dst_dir = os.path.dirname(dst)
    os.makedirs(dst_dir, exist_ok=True)
    tmp = dst + ".tmp." + str(os.getpid())
    with open(src, "rb") as fh:
        data = fh.read()
    try:
        try:
            with open(tmp, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
        except OSError as exc:
            wrapped = CopyWriteError(exc.errno, str(exc))
            wrapped.winerror = getattr(exc, "winerror", None)
            raise wrapped from exc
        os.replace(tmp, dst)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def do_backup(cache_dir, backup_dir, name=TOKEN_CACHE_NAME):
    """Copy the live cache to the backup folder when it is healthy. Returns the action taken.

    Only copies when ``should_backup`` says the live file is healthy. Before overwriting the
    existing backup it rolls the current backup to ``<name>.prev`` (one generation), so a
    single bad copy cannot destroy the only good one. Returns one of ``"backed-up"``,
    ``"skipped-unhealthy"`` (nothing healthy to copy). Never raises for the ordinary
    missing-file case; genuine I/O errors propagate.
    """
    src = os.path.join(cache_dir, name)
    live_size = _size_or_none(src)
    if not should_backup(live_size):
        return "skipped-unhealthy"
    dst = os.path.join(backup_dir, name)
    # Roll the current good backup aside before writing the new one (one generation kept).
    prev_size = _size_or_none(dst)
    if prev_size is not None and prev_size > MIN_HEALTHY_BYTES:
        try:
            _atomic_copy(dst, dst + ".prev")
        except OSError as exc:
            if is_disk_full_write(exc):
                raise
    _atomic_copy(src, dst)
    return "backed-up"


def do_restore(cache_dir, backup_dir, login_running, name=TOKEN_CACHE_NAME):
    """Restore the live cache from the backup when the live file is zeroed. Returns the action.

    Reads the live size, the backup size and whether a login process is running, then asks
    ``should_restore``. When it says yes, the backup is atomically copied onto the live file.
    Returns one of ``"restored"``, ``"skipped-live-ok"`` (live cache is not the 0-byte state),
    ``"skipped-login-running"``, or ``"skipped-no-backup"`` (no healthy copy to restore from).
    The caller logs the word; this performs only the file-level restore.
    """
    live = os.path.join(cache_dir, name)
    backup = os.path.join(backup_dir, name)
    live_size = _size_or_none(live)
    backup_size = _size_or_none(backup)
    if login_running:
        return "skipped-login-running"
    if live_size != 0:
        return "skipped-live-ok"
    if not should_restore(live_size, login_running, backup_size):
        return "skipped-no-backup"
    _atomic_copy(backup, live)
    return "restored"

"""Crash-safe, multi-process-safe storage for whole-file JSON knowledge stores.

Why this exists: a store that does "read the whole file, change one entry,
write the whole file" loses data in two ways when more than one process uses
it (an MCP server, fleet jobs and the auto-save hook all do):

  1. lost update   -- two writers read the same state; the later write drops
                      the earlier writer's entry;
  2. total wipe    -- a reader that hits a half-written file treated the read
                      failure as "the store is empty", and its next save
                      replaced the whole file with that one entry.

This module is the one place that fixes the mechanism:

  * writes are atomic (temp file in the same directory, flush + fsync,
    os.replace), so a reader never sees a half-written file;
  * the whole read-modify-write runs under a cross-process lock (an
    exclusively created ``<file>.lock`` with stale-lock detection by owner
    pid and age, and a bounded wait -- it can never hang forever);
  * a read failure is NEVER turned into an empty state: the read retries
    briefly, then falls back to the newest valid generation backup
    (``<file>.bak.1`` .. ``.bak.N``, rotated before every replace, with a log
    line when one is used), and if nothing valid exists it raises
    ``StateUnreadable`` so the caller refuses to save;
  * a save that would shrink the entry count by more than a small fraction is
    refused (``StateShrinkRefused``) unless the caller says it is an explicit
    delete/prune (``allow_shrink=True``).

The on-disk format is whatever the caller serialises (JSON, indent=2,
ensure_ascii=False -- the same as before), so existing files load unchanged.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

log = logging.getLogger("state_store")

BACKUP_GENERATIONS = 5
LOCK_WAIT_SECONDS = 15.0
LOCK_STALE_SECONDS = 120.0
READ_ATTEMPTS = 6
READ_RETRY_SLEEP = 0.1
REPLACE_ATTEMPTS = 40
REPLACE_RETRY_SLEEP = 0.05
# A save may lower the entry count by at most max(SHRINK_FLOOR, SHRINK_FRACTION * previous).
SHRINK_FRACTION = 0.10
SHRINK_FLOOR = 2


class StateStoreError(RuntimeError):
    pass


class StateUnreadable(StateStoreError):
    """The file exists but no valid generation (main or backup) could be read."""


class StateShrinkRefused(StateStoreError):
    """The save would drop too many entries and the caller did not ask for it."""


class StateLockTimeout(StateStoreError):
    """The cross-process lock could not be taken within the bounded wait."""


# --------------------------------------------------------------------------
# locking
# --------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            # access denied (5) means it exists; invalid parameter (87) means it does not
            return ctypes.get_last_error() == 5
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_owner_pid(lock: Path) -> Optional[int]:
    try:
        text = lock.read_text(encoding="utf-8").strip()
        return int(text.split()[0])
    except Exception:
        return None


def _lock_is_stale(lock: Path, stale_seconds: float) -> Optional[str]:
    """Return why the lock is stale, or None if it is held by a live owner."""
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        # gone, or delete-pending (Windows reports that as access denied)
        return None
    if age > stale_seconds:
        return f"older than {stale_seconds}s ({age:.1f}s)"
    pid = _lock_owner_pid(lock)
    # A lock file that has just been created may not hold its pid yet; only an
    # unreadable pid on an old-enough file counts as stale.
    if pid is None:
        return f"no readable owner pid and age {age:.1f}s" if age > 5.0 else None
    return None if _pid_alive(pid) else f"owner pid {pid} is not running"


_thread_lock_guard = threading.Lock()


@contextlib.contextmanager
def file_lock(
    path: Path,
    wait_seconds: float = LOCK_WAIT_SECONDS,
    stale_seconds: float = LOCK_STALE_SECONDS,
) -> Iterator[None]:
    """Exclusive cross-process lock for `path` (portable, no msvcrt/fcntl)."""
    path = Path(path)
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    token = f"{os.getpid()} {threading.get_ident()} {time.time():.3f}"
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            why = _lock_is_stale(lock, stale_seconds)
            if why:
                # Atomic take-over: only one contender's rename succeeds.
                grave = lock.with_name(lock.name + f".stale.{os.getpid()}.{time.time_ns()}")
                try:
                    os.replace(str(lock), str(grave))
                    log.warning("state_store: removed stale lock %s (%s)", lock, why)
                    with contextlib.suppress(OSError):
                        os.remove(str(grave))
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise StateLockTimeout(f"could not lock {path} within {wait_seconds}s")
            time.sleep(0.02)
            continue
        except PermissionError:
            # Windows reports a lock file pending deletion as access denied.
            if time.monotonic() >= deadline:
                raise StateLockTimeout(f"could not lock {path} within {wait_seconds}s")
            time.sleep(0.02)
            continue
        try:
            os.write(fd, token.encode("ascii"))
        finally:
            os.close(fd)
        break
    try:
        yield
    finally:
        # A contender may have the lock file open to read the owner pid, and on
        # Windows that blocks the delete: retry, never leak the lock.
        for _ in range(200):
            try:
                os.remove(str(lock))
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.01)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def backup_paths(path: Path, generations: int = BACKUP_GENERATIONS) -> list[Path]:
    path = Path(path)
    return [path.with_name(f"{path.name}.bak.{i}") for i in range(1, generations + 1)]


def _try_read(p: Path, validate: Callable[[Any], bool]) -> Any:
    """Parse + validate `p`. Raises on any failure."""
    data = json.loads(p.read_text(encoding="utf-8-sig"))  # tolerate a BOM
    if not validate(data):
        raise ValueError("unexpected shape")
    return data


def read_json(
    path: Path,
    *,
    validate: Callable[[Any], bool],
    default: Callable[[], Any],
    generations: int = BACKUP_GENERATIONS,
) -> Any:
    """Return the stored state, never an empty stand-in for an unreadable file.

    missing file + no backups, or a zero-byte file + no backups -> default()
    unreadable / invalid file -> retry, then newest valid backup, else raise
    StateUnreadable.
    """
    path = Path(path)
    baks = [b for b in backup_paths(path, generations) if b.exists()]
    last_err: Optional[BaseException] = None
    exists = False
    nonempty = False
    for attempt in range(READ_ATTEMPTS):
        try:
            exists = path.exists()
            if not exists:
                break
            nonempty = path.stat().st_size > 0
            if not nonempty and not baks:
                return default()
            return _try_read(path, validate)
        except Exception as e:  # torn read, sharing violation, bad shape
            last_err = e
            time.sleep(READ_RETRY_SLEEP)
    for b in baks:
        try:
            data = _try_read(b, validate)
        except Exception:
            continue
        log.warning(
            "state_store: %s unreadable (%s: %s); using backup %s",
            path, type(last_err).__name__ if last_err else "missing", last_err, b.name,
        )
        return data
    if not exists:
        return default()
    raise StateUnreadable(
        f"{path} exists but is unreadable and no valid backup exists "
        f"({type(last_err).__name__}: {last_err}); refusing to treat it as empty"
    )


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def _rotate_backups(path: Path, payload: str, generations: int) -> None:
    """Shift .bak.N-1 -> .bak.N ... and store `payload` (the state just committed,
    known valid) as .bak.1. So .bak.1 always equals the main file as last written
    by this module and .bak.2.. are the earlier generations; a foreign writer that
    damages the main file therefore costs at most nothing from .bak.1."""
    baks = backup_paths(path, generations)
    for i in range(len(baks) - 1, 0, -1):
        if baks[i - 1].exists():
            with contextlib.suppress(OSError):
                os.replace(str(baks[i - 1]), str(baks[i]))
    tmp = baks[0].with_name(baks[0].name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(str(tmp), str(baks[0]))
    except OSError as e:
        log.warning("state_store: could not write backup %s: %s", baks[0], e)
        with contextlib.suppress(OSError):
            tmp.unlink()


def atomic_write_json(path: Path, data: Any, *, generations: int = BACKUP_GENERATIONS) -> None:
    """Write `data` atomically, then rotate generation backups. Caller holds the lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        last: Optional[OSError] = None
        for _ in range(REPLACE_ATTEMPTS):
            try:
                os.replace(str(tmp), str(path))
                break
            except PermissionError as e:  # a reader has it open (Windows)
                last = e
                time.sleep(REPLACE_RETRY_SLEEP)
        else:
            raise last  # type: ignore[misc]
    finally:
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()
    _rotate_backups(path, payload, generations)


def update_json(
    path: Path,
    mutate: Callable[[Any], Any],
    *,
    validate: Callable[[Any], bool],
    count: Callable[[Any], int],
    default: Callable[[], Any],
    allow_shrink: bool = False,
    generations: int = BACKUP_GENERATIONS,
) -> Any:
    """Locked read-modify-write. Returns whatever `mutate(state)` returns.

    `mutate` edits the state in place. If it raises, nothing is written.
    """
    path = Path(path)
    with file_lock(path):
        state = read_json(path, validate=validate, default=default, generations=generations)
        before = count(state)
        result = mutate(state)
        after = count(state)
        if not allow_shrink and after < before - max(SHRINK_FLOOR, int(SHRINK_FRACTION * before)):
            raise StateShrinkRefused(
                f"refusing to save {path.name}: {before} -> {after} entries "
                "(use an explicit delete/prune to shrink)"
            )
        atomic_write_json(path, state, generations=generations)
        return result

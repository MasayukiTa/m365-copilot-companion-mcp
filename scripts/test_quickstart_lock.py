# -*- coding: utf-8 -*-
r"""quickstart_lock.ps1, run for real (INST-14, 2026-09-24 review: fail CLOSED, not open).

Exercises the actual .ps1 -- not a reimplementation of its logic -- against a throwaway
-LockPath (never this repo's own .setup\quickstart.lock). -OwnerPid (documented "tests only" in
the script's own header) stands in for the parent cmd.exe's PID, so two "windows" racing for the
same lock can be driven from one Python process using two real, long-lived dummy processes as
the stand-in owners, without needing two actual cmd.exe windows.

Covers:
  * two concurrent acquisitions: the first wins (0), the second is told IN USE (10) and named.
  * a stale lock (owner PID confirmed dead) is taken over automatically -- no file deleted by
    hand, exactly the "prefer automatic stale detection over telling the user to delete files"
    requirement.
  * a helper failure (the lock FILE ITSELF cannot be created at all -- an illegal path character)
    is a distinct code that is neither 0 (acquired) nor 10 (confirmed in use).
  * repeated, unresolvable contention (the lock file is held open exclusively by something that
    is not a recognised owner stamp for the whole retry window) fails CLOSED with exit 11, not
    the old "continuing without it" exit 0 this review flagged.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

LOCK_PS1 = os.path.join(REPO, "scripts", "quickstart_lock.ps1")

_POWERSHELL = (
    shutil.which("powershell")
    or shutil.which("powershell.exe")
    or (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") else None)
)

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not _POWERSHELL,
    reason="quickstart_lock.ps1 is Windows PowerShell (os.name=%r, powershell=%r)"
           % (os.name, bool(_POWERSHELL)),
)


def run_lock(action, lock_path, owner_pid=None, timeout=30):
    cmd = [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", LOCK_PS1,
           action, "-LockPath", str(lock_path)]
    if owner_pid is not None:
        cmd += ["-OwnerPid", str(owner_pid)]
    return childproc.run(cmd, timeout=timeout)


def spawn_dummy_process(seconds=30):
    """A real, long-lived process to stand in for "the cmd.exe running quickstart.bat"."""
    return subprocess.Popen(
        [_POWERSHELL, "-NoProfile", "-Command", "Start-Sleep -Seconds %d" % seconds],
        creationflags=childproc.headless_creationflags())


def _stop(*procs, timeout=10):
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    for p in procs:
        try:
            p.wait(timeout=timeout)
        except Exception:
            pass


def test_two_concurrent_acquisitions_one_wins_one_is_told_in_use(tmp_path):
    lock = tmp_path / "quickstart.lock"
    owner_a = spawn_dummy_process(20)
    owner_b = spawn_dummy_process(20)
    try:
        r1 = run_lock("acquire", lock, owner_pid=owner_a.pid)
        assert r1.returncode == 0, r1.stdout + r1.stderr

        r2 = run_lock("acquire", lock, owner_pid=owner_b.pid)
        assert r2.returncode == 10, r2.stdout + r2.stderr
        assert str(owner_a.pid) in r2.stdout, r2.stdout

        # Re-running with A's OWN pid (a re-run in the same console after an interrupt) is
        # recognised as ITS lock, not a second holder.
        r3 = run_lock("acquire", lock, owner_pid=owner_a.pid)
        assert r3.returncode == 0, r3.stdout + r3.stderr
    finally:
        _stop(owner_a, owner_b)


def test_stale_lock_with_dead_pid_is_taken_over_automatically(tmp_path):
    lock = tmp_path / "quickstart.lock"
    # A PID that is guaranteed to be dead by the time we use it, without ever needing to guess
    # or reuse a live one.
    dead = subprocess.Popen([_POWERSHELL, "-NoProfile", "-Command", "exit 0"])
    dead_pid = dead.pid
    dead.wait(timeout=10)
    lock.write_text("%d|123456789" % dead_pid, encoding="ascii")

    owner = spawn_dummy_process(20)
    try:
        r = run_lock("acquire", lock, owner_pid=owner.pid)
        assert r.returncode == 0, r.stdout + r.stderr
        # AUTOMATIC, not "ask the operator to delete the file": the stale stamp is gone,
        # replaced by the new, live owner's -- nobody had to intervene.
        content = lock.read_text(encoding="ascii")
        assert content.startswith(str(owner.pid) + "|"), content
    finally:
        _stop(owner)


def test_helper_failure_to_create_the_lock_file_is_neither_0_nor_10(tmp_path):
    # "|" is illegal in a Windows filename: [System.IO.File]::Open(..., CreateNew, ...) throws
    # ArgumentException, which Try-CreateLock's `catch [System.IO.IOException]` does NOT catch,
    # so this is a genuine helper crash -- not "another quickstart is running" (10) and not a
    # confirmed acquisition (0). quickstart.bat must fail closed on this too (see its own tests).
    lock = tmp_path / "lock|invalid.lock"
    owner = spawn_dummy_process(10)
    try:
        r = run_lock("acquire", lock, owner_pid=owner.pid)
        assert r.returncode not in (0, 10), "expected a distinct failure code, got %r\n%s%s" % (
            r.returncode, r.stdout, r.stderr)
    finally:
        _stop(owner)


def test_unconfirmable_contention_fails_closed_with_11_not_0(tmp_path):
    # Hold the lock file open EXCLUSIVELY (FileShare.None) from a process that never writes a
    # recognisable owner stamp into it: every attempt in the 3-try loop gets "file exists" from
    # Try-CreateLock (caught) and then "sharing violation" from Read-Lock (also caught, becomes
    # $null) -- so the loop can never decide "ours", "stale", or "a live PID holds it". INST-14:
    # this ambiguous case must exit 11 (fail closed), not the old 0 ("continuing without it").
    lock = tmp_path / "quickstart.lock"
    quoted = str(lock).replace("'", "''")
    holder_script = (
        "$fs = [System.IO.File]::Open('%s', [System.IO.FileMode]::OpenOrCreate, "
        "[System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None); "
        "Start-Sleep -Seconds 8; $fs.Close()" % quoted
    )
    holder = subprocess.Popen([_POWERSHELL, "-NoProfile", "-Command", holder_script])
    owner = spawn_dummy_process(10)
    try:
        time.sleep(1.5)  # let the holder actually open its exclusive handle first
        r = run_lock("acquire", lock, owner_pid=owner.pid, timeout=30)
        assert r.returncode == 11, "expected 11 (could not confirm), got %r\n%s%s" % (
            r.returncode, r.stdout, r.stderr)
    finally:
        _stop(owner)
        holder.wait(timeout=15)


def test_release_only_removes_our_own_lock(tmp_path):
    lock = tmp_path / "quickstart.lock"
    owner_a = spawn_dummy_process(20)
    owner_b = spawn_dummy_process(20)
    try:
        assert run_lock("acquire", lock, owner_pid=owner_a.pid).returncode == 0
        # B cannot release A's live lock.
        run_lock("release", lock, owner_pid=owner_b.pid)
        assert lock.exists()
        # A can release its own.
        run_lock("release", lock, owner_pid=owner_a.pid)
        assert not lock.exists()
    finally:
        _stop(owner_a, owner_b)

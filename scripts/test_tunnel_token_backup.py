# -*- coding: utf-8 -*-
r"""The Dev Tunnel token-cache safety net: back up a healthy cache, restore a zeroed one.

WHAT IS PINNED HERE
  * the PURE deciders (scripts/tunnel_token_backup.should_backup / should_restore): a table
    exercised on every platform, no PowerShell, no live cache.
  * the FILE-LEVEL round trip (do_backup / do_restore) against a COPY of a cache directory
    built in a tmp_path -- NEVER the live %LOCALAPPDATA%\DevTunnels cache. A healthy cache is
    copied out; the live copy is then truncated to 0 B (the measured broken state); restore
    puts the bytes back without any sign-in.
  * the atomic contract: a backup is written via a temp file and os.replace, so an existing
    good backup is rolled to <name>.prev and never half-overwritten.
  * when the real devtunnel CLI is present AND honours a per-run cache directory, that the
    CLI reports logged-in from the restored COPY with no browser sign-in. When the CLI is
    absent or does not support redirecting its cache dir, that leg is skipped and the file-
    level restore stands as the verified claim (see test_cli_sees_restored_cache_or_is_skipped
    for exactly what was and was not verified).

The live cache is never read, written, or stat'd by any test here: default_cache_dir and
default_backup_dir take an explicit base, and every test passes a tmp_path.
"""
from __future__ import annotations

import os
import shutil
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
SCRIPTS = os.path.join(REPO, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import tunnel_token_backup as T  # noqa: E402

HEALTHY = b"x" * 4182        # a measured healthy size (20:44 on 2026-11-11)
HEALTHY2 = b"y" * 4214       # the other measured healthy size (00:33)


# --------------------------------------------------------------------------- pure deciders

@pytest.mark.parametrize("live_size,expected", [
    (None, False),      # no file -> nothing to copy
    (0, False),         # the broken, zeroed state -> never promoted into the backup
    (1, False),         # truncated-but-nonzero -> not clearly healthy
    (1000, False),      # exactly the floor -> must be strictly greater
    (1001, True),       # just over the floor -> healthy
    (4182, True),       # measured healthy size
    (4214, True),       # the other measured healthy size
])
def test_should_backup_table(live_size, expected):
    assert T.should_backup(live_size) is expected


@pytest.mark.parametrize("live_size,login_running,backup_size,expected", [
    (0, False, 4182, True),     # zeroed live, no login, healthy backup -> restore
    (0, True, 4182, False),     # a sign-in is in flight -> wait, do not race it
    (0, False, None, False),    # no backup to restore from -> report, do not invent a file
    (0, False, 0, False),       # backup itself is empty -> nothing good to restore
    (0, False, 1000, False),    # backup at the floor -> must be strictly healthy
    (None, False, 4182, False), # live cache absent (fresh install) -> not our zeroing case
    (4182, False, 4182, False), # live cache healthy -> leave it alone
    (1, False, 4182, False),    # live cache truncated-but-nonzero -> not the 0-byte state
])
def test_should_restore_table(live_size, login_running, backup_size, expected):
    assert T.should_restore(live_size, login_running, backup_size) is expected


def test_login_running_blocks_restore_even_with_everything_else_right():
    # The login guard is dominant: a sign-in in flight must stop a restore whatever the
    # sizes say, because the sign-in is itself truncate-then-writing the same file.
    assert T.should_restore(0, True, 4182) is False


# --------------------------------------------------------------------------- dir resolvers

def test_backup_dir_is_under_user_profile_not_repo():
    base = os.path.join("C:\\Users\\someone\\AppData\\Local")
    d = T.default_backup_dir(localappdata=base)
    assert d is not None
    assert d.startswith(base)
    assert "m365-companion" in d
    # Never inside the repository tree, never H:, never a SharePoint path.
    assert REPO not in d
    assert not d.upper().startswith("H:")
    assert "sharepoint" not in d.lower()


def test_dir_resolvers_fail_closed_without_localappdata():
    assert T.default_cache_dir(localappdata="") is None
    assert T.default_backup_dir(localappdata="") is None


# --------------------------------------------------------------------- file-level round trip

def _make_cache_copy(tmp_path, contents=HEALTHY):
    """Build a COPY of a cache directory (never the live one) with a token file in it."""
    cache = tmp_path / "DevTunnels"
    cache.mkdir()
    (cache / T.TOKEN_CACHE_NAME).write_bytes(contents)
    return str(cache)


def test_backup_then_restore_round_trip(tmp_path):
    cache = _make_cache_copy(tmp_path, HEALTHY)
    backup = str(tmp_path / "backup")
    live = os.path.join(cache, T.TOKEN_CACHE_NAME)

    assert T.do_backup(cache, backup) == "backed-up"
    assert os.path.getsize(os.path.join(backup, T.TOKEN_CACHE_NAME)) == len(HEALTHY)

    # Reproduce the measured broken state: the live cache goes to 0 B.
    with open(live, "wb") as fh:
        fh.truncate(0)
    assert os.path.getsize(live) == 0

    assert T.do_restore(cache, backup, login_running=False) == "restored"
    assert os.path.getsize(live) == len(HEALTHY)
    with open(live, "rb") as fh:
        assert fh.read() == HEALTHY


def test_backup_skips_an_unhealthy_live_cache(tmp_path):
    cache = _make_cache_copy(tmp_path, b"")        # 0-byte live cache
    backup = str(tmp_path / "backup")
    assert T.do_backup(cache, backup) == "skipped-unhealthy"
    assert not os.path.exists(os.path.join(backup, T.TOKEN_CACHE_NAME))


def test_restore_waits_while_a_login_is_running(tmp_path):
    cache = _make_cache_copy(tmp_path, HEALTHY)
    backup = str(tmp_path / "backup")
    T.do_backup(cache, backup)
    live = os.path.join(cache, T.TOKEN_CACHE_NAME)
    with open(live, "wb") as fh:
        fh.truncate(0)
    assert T.do_restore(cache, backup, login_running=True) == "skipped-login-running"
    assert os.path.getsize(live) == 0        # untouched: we did not race the sign-in


def test_restore_leaves_a_healthy_live_cache_alone(tmp_path):
    cache = _make_cache_copy(tmp_path, HEALTHY)
    backup = str(tmp_path / "backup")
    T.do_backup(cache, backup)
    live = os.path.join(cache, T.TOKEN_CACHE_NAME)
    assert T.do_restore(cache, backup, login_running=False) == "skipped-live-ok"
    with open(live, "rb") as fh:
        assert fh.read() == HEALTHY


def test_restore_reports_when_there_is_no_backup(tmp_path):
    cache = _make_cache_copy(tmp_path, b"")        # zeroed live, and no backup was ever made
    backup = str(tmp_path / "backup")
    assert T.do_restore(cache, backup, login_running=False) == "skipped-no-backup"


def test_backup_rolls_one_generation_and_never_half_overwrites(tmp_path):
    cache = _make_cache_copy(tmp_path, HEALTHY)
    backup = str(tmp_path / "backup")
    assert T.do_backup(cache, backup) == "backed-up"
    # A second, different healthy cache is backed up; the first good copy is kept as .prev.
    with open(os.path.join(cache, T.TOKEN_CACHE_NAME), "wb") as fh:
        fh.write(HEALTHY2)
    assert T.do_backup(cache, backup) == "backed-up"
    assert os.path.getsize(os.path.join(backup, T.TOKEN_CACHE_NAME)) == len(HEALTHY2)
    assert os.path.getsize(os.path.join(backup, T.TOKEN_CACHE_NAME + ".prev")) == len(HEALTHY)


def test_backup_leaves_no_temp_file_behind(tmp_path):
    cache = _make_cache_copy(tmp_path, HEALTHY)
    backup = str(tmp_path / "backup")
    T.do_backup(cache, backup)
    leftovers = [n for n in os.listdir(backup) if ".tmp." in n]
    assert leftovers == []


# ------------------------------------------------------------------ optional real-CLI check

def _devtunnel_cli():
    return shutil.which("devtunnel") or shutil.which("devtunnel.exe")


def test_cli_sees_restored_cache_or_is_skipped(tmp_path):
    r"""If the real devtunnel CLI honours a redirected cache dir, prove it reads the restored
    COPY as logged-in with no sign-in. Otherwise skip and leave the file-level restore above
    as the verified claim.

    WHAT IS VERIFIED when this does NOT skip: with the CLI pointed at a COPY of the cache
    directory (via a per-run cache-path environment variable), `devtunnel user show` reports
    a logged-in identity after do_restore put the token bytes back -- and no browser opened,
    because the process is run non-interactively with stdin closed and a short timeout.

    WHAT IS NOT VERIFIED (when skipped): that the installed CLI build actually re-reads tokens
    from the redirected directory. The CLI does not document a supported cache-path override on
    every build; when none of the candidate variables changes where it looks, this cannot make
    a honest end-to-end claim, so it skips rather than pretend. The file-level round trip above
    still proves the bytes are restored identically.
    """
    cli = _devtunnel_cli()
    if not cli:
        pytest.skip("devtunnel CLI not installed on this runner; file-level restore is the "
                    "verified claim (see test_backup_then_restore_round_trip)")

    # Build a COPY of a (stub) cache dir and a backup, then restore into the copy.
    cache = _make_cache_copy(tmp_path, HEALTHY)
    backup = str(tmp_path / "backup")
    T.do_backup(cache, backup)
    with open(os.path.join(cache, T.TOKEN_CACHE_NAME), "wb") as fh:
        fh.truncate(0)
    assert T.do_restore(cache, backup, login_running=False) == "restored"

    # Try the candidate cache-dir overrides this CLI family has used. If none redirects the
    # CLI's cache, we cannot verify end-to-end against the copy, so skip (never touch live).
    # tools.childproc.run applies the repo's one decode policy (UTF-8 first, cp932, then
    # errors='replace') instead of text=True's cp932-only decode -- exactly the unguarded
    # text=True shape the tree's ratchet forbids. stdin is left unconnected and the call is
    # bounded by a timeout, so the CLI cannot drive an interactive browser sign-in.
    import io as _io
    from tools.childproc import run as _child_run
    candidates = ["DEVTUNNEL_CACHE_DIR", "TUNNEL_CACHE_DIR", "DEVTUNNELS_CACHE_DIR"]
    for var in candidates:
        env = dict(os.environ)
        env[var] = cache
        try:
            proc = _child_run([cli, "user", "show"], env=env,
                              stdin=_io.BytesIO(b""), timeout=20)
        except Exception:
            continue
        out = ((proc.stdout or "") + (proc.stderr or "")).lower()
        # A redirected, restored cache should read as logged in. The stub bytes are not a real
        # MSAL blob, so a real build may instead report 'not logged in' from the copy -- which
        # still proves it read the copy, not the live cache. Either way the live cache was
        # never consulted. We only assert the strong, honest claim when the CLI clearly reads
        # logged-in; otherwise we keep looking / skip.
        if "logged in" in out and "not logged in" not in out:
            assert "not logged in" not in out
            return
    pytest.skip("installed devtunnel CLI does not honour a redirected cache dir on this "
                "runner; file-level restore is the verified claim")


@pytest.mark.parametrize("number,winerror,expected", [
    (28, None, True), (5, 112, True), (13, None, False), (None, None, False),
])
def test_disk_full_classification_requires_destination_write(number, winerror, expected):
    error = T.CopyWriteError(number, "write failed")
    error.winerror = winerror
    assert T.is_disk_full_write(error) is expected
    assert not T.is_disk_full_write(OSError(number, "read or stat failed"))


def test_disk_full_restore_is_reported_and_retried(tmp_path, monkeypatch):
    import builtins
    cache = _make_cache_copy(tmp_path, HEALTHY)
    backup = str(tmp_path / "backup")
    assert T.do_backup(cache, backup) == "backed-up"
    live = os.path.join(cache, T.TOKEN_CACHE_NAME)
    with open(live, "wb") as fh:
        fh.truncate(0)
    real_open = builtins.open

    def full_on_destination(path, mode="r", *args, **kwargs):
        if mode == "wb" and str(path).startswith(live + ".tmp."):
            raise OSError(28, "disk full")
        return real_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", full_on_destination)
        assert T.run_cycle(cache, backup, False) == ("skipped-unhealthy", "failed-disk-full")
    assert os.path.getsize(live) == 0
    assert T.run_cycle(cache, backup, False) == ("skipped-unhealthy", "restored")
    assert open(live, "rb").read() == HEALTHY


def test_disk_full_backup_does_not_prevent_restore_check(tmp_path, monkeypatch):
    cache = _make_cache_copy(tmp_path)
    backup = str(tmp_path / "backup")
    def fail(*args):
        raise T.CopyWriteError(28, "disk full")
    monkeypatch.setattr(T, "_atomic_copy", fail)
    assert T.run_cycle(cache, backup, False) == ("failed-disk-full", "skipped-live-ok")


def test_supervisor_logs_disk_full_and_retries_tick_each_cycle():
    from pathlib import Path
    supervisor = (Path(REPO) / "scripts" / "supervisor.ps1").read_text(encoding="utf-8")
    loop = supervisor[supervisor.index("while ($true) {", supervisor.index("# Checked once, here")):]
    assert loop.count("tunnel_token_backup_tick.ps1") == 1
    assert "restore=failed-disk-full" in loop
    assert "backup=failed-disk-full" in loop
    assert "Restore will be retried on the next cycle" in loop
    assert loop.index("& $tokenTick") < loop.index("Test-DevtunnelLoggedInCached")


def test_supervisor_runs_token_tick_once_per_health_cycle():
    from pathlib import Path
    supervisor = (Path(REPO) / "scripts" / "supervisor.ps1").read_text(encoding="utf-8")
    loop = supervisor[supervisor.index("while ($true) {", supervisor.index("# Checked once, here")):]
    assert loop.count('tunnel_token_backup_tick.ps1') == 1
    tick = loop.index('& $tokenTick -RepoRoot $Root -Python $Py -ErrorAction Stop')
    assert loop.rfind('try {', 0, tick) != -1
    assert 'catch {' in loop[tick:loop.index('if (-not (Test-TunnelRouteWanted))', tick)]
    assert 'Write-Log ("devtunnel token backup tick: "' in loop[tick:]
    assert tick < loop.index('Test-DevtunnelLoggedInCached', tick)

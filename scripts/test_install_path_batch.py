# -*- coding: utf-8 -*-
"""setup.bat and quickstart.bat, RUN in throwaway trees with stubs (new-PC review 2026-09-24).

setup.bat:
  D8   a too-old Python on PATH is skipped (with a message) for uv's pinned 3.12, and uv is
       told --python 3.12; a new-enough one is used.
  D20  a truncated .setup\\bin\\uv.exe is executed, found dead, deleted and re-fetched; a .venv
       python.exe that cannot run is removed and the venv rebuilt.
  D23  a "!" in the install path is refused with the fix named; "'" and "(1)" work.
  D10  a proxy is announced and named in the failure text.
  D18  a Mark-of-the-Web on the scripts is detected, and SETUP_UNBLOCK removes it.
  D17  .gitattributes makes a core.autocrlf=false clone and `git archive` deliver CRLF .bat,
       and the cloned setup.bat's labels work.
quickstart.bat (setup.bat and setup_devtunnel.ps1 replaced by stubs; stops at STEP 4):
  D21  a second quickstart is refused while the first holds the lock.
  D25  a failed `git fetch` is reported, never "Up to date."
  D4   N and T remove MCP_TUNNEL_ALLOW_ANONYMOUS from .env and say STEP 4 revokes a grant; A
       writes it exactly once.
  D28  those .env edits go through env_file.py (atomic).

Stubs: a compiled uv.exe (csc) that logs argv and builds a pip-less venv with the Python
running these tests; a python.cmd that answers like Python 3.9; a bootstrap.py that only says
which interpreter launched it. PATH holds only System32 + the stubs, USERPROFILE/LOCALAPPDATA
are fake, so nothing on the machine's own profile is found or touched. Windows-only.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import _install_path_harness as H  # noqa: E402

pytestmark = pytest.mark.skipif(not H.IS_WINDOWS, reason="cmd.exe batch files; Windows only")

OLD_PYTHON_CMD = ('@echo off\r\n'
                  'echo %* | findstr /c:"version.split" >nul && (echo 3.9.1& exit /b 0)\r\n'
                  'exit /b 3\r\n')


def _stubbin(root: Path, old_python=False) -> Path:
    d = root / "stubbin"
    d.mkdir(exist_ok=True)
    if old_python:
        (d / "python.cmd").write_text(OLD_PYTHON_CMD, encoding="ascii")
    return d


def _with_uv(tree: Path, root: Path) -> Path:
    uv = H.uv_stub_exe(root)
    if uv is None:
        pytest.skip("csc.exe not available to build the uv stub")
    (tree / ".setup" / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(uv, tree / ".setup" / "bin" / "uv.exe")
    return root / "uv.log"


def _setup(tree, env, *args, stdin=""):
    r = H.run_cmd("setup.bat", tree, env, args=list(args) or ["--status"], stdin=stdin)
    return r, r.stdout + r.stderr


# ---- setup.bat -----------------------------------------------------------------------------

def test_a_too_old_python_is_skipped_for_uvs_pinned_312(tmp_path):
    tree = H.setup_tree(tmp_path, "repo (1)")
    log = _with_uv(tree, tmp_path)
    env = H.clean_env(tree, H.minimal_path(_stubbin(tmp_path, old_python=True)),
                      UV_STUB_LOG=log, UV_STUB_PYTHON=H.base_python(), SETUP_IGNORE_POLICY=1)
    r, out = _setup(tree, env)
    assert r.returncode == 0, out
    assert "older than 3.10" in out and "3.9.1" in out
    assert "STUB-BOOTSTRAP" in out and ".venv" in out.split("STUB-BOOTSTRAP", 1)[1]
    assert "venv --seed .venv --python 3.12" in log.read_text()


def test_a_new_enough_python_on_path_is_used(tmp_path):
    tree = H.setup_tree(tmp_path, "it's a repo")
    pydir = os.path.dirname(H.base_python())
    env = H.clean_env(tree, H.minimal_path(pydir), SETUP_IGNORE_POLICY=1)
    r, out = _setup(tree, env)
    assert r.returncode == 0, out
    assert "Using Python interpreter: python" in out and "STUB-BOOTSTRAP" in out
    assert "args=['--status']" in out


def test_a_truncated_uv_is_run_found_dead_and_replaced(tmp_path):
    tree = H.setup_tree(tmp_path)
    (tree / ".setup" / "bin").mkdir(parents=True)
    (tree / ".setup" / "bin" / "uv.exe").write_bytes(b"MZ\x90\x00trunc")
    env = H.clean_env(tree, H.minimal_path(_stubbin(tmp_path)), SETUP_IGNORE_POLICY=1,
                      UV_INSTALLER_URL="http://127.0.0.1:9/install.ps1")
    r, out = _setup(tree, env)
    assert r.returncode == 1, out
    assert "does not run" in out and "replacing it" in out
    assert "Attempting a no-admin install of 'uv'" in out, "the download was not retried"
    assert "ACTION NEEDED: Could not auto-install 'uv'" in out
    assert not (tree / ".setup" / "bin" / "uv.exe").exists()


def test_a_venv_python_that_cannot_run_is_rebuilt(tmp_path):
    tree = H.setup_tree(tmp_path)
    log = _with_uv(tree, tmp_path)
    broken = tree / ".venv" / "Scripts" / "python.exe"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not a program")
    env = H.clean_env(tree, H.minimal_path(_stubbin(tmp_path)),
                      UV_STUB_LOG=log, UV_STUB_PYTHON=H.base_python(), SETUP_IGNORE_POLICY=1)
    r, out = _setup(tree, env)
    assert r.returncode == 0, out
    assert "could not run" in out and "Removing the unusable .venv" in out
    assert "STUB-BOOTSTRAP python=" in out
    assert "venv --seed .venv --python 3.12" in log.read_text()


def test_a_bang_in_the_install_path_is_refused_with_the_fix(tmp_path):
    tree = H.setup_tree(tmp_path, "a!b")
    env = H.clean_env(tree, H.minimal_path(os.path.dirname(H.base_python())),
                      SETUP_IGNORE_POLICY=1)
    r, out = _setup(tree, env)
    assert r.returncode == 1, out
    assert 'contains a "!"' in out and "a!b" in out
    assert "STUB-BOOTSTRAP" not in out


def test_the_proxy_is_announced_and_named_when_the_download_fails(tmp_path):
    tree = H.setup_tree(tmp_path)
    env = H.clean_env(tree, H.minimal_path(_stubbin(tmp_path)), SETUP_IGNORE_POLICY=1,
                      UV_INSTALLER_URL="http://127.0.0.1:9/install.ps1",
                      HTTPS_PROXY="http://127.0.0.1:9")
    r, out = _setup(tree, env)
    assert r.returncode == 1, out
    assert "Using this PC's proxy for downloads: http://127.0.0.1:9" in out
    assert "uses a proxy (http://127.0.0.1:9)" in out


def _mark(path: Path):
    cmd = ("Set-Content -LiteralPath '%s' -Stream Zone.Identifier -Value \"[ZoneTransfer]`r`nZoneId=3\""
           % str(path).replace("'", "''"))
    assert H.childproc.run([H.POWERSHELL, "-NoProfile", "-Command", cmd], timeout=60).returncode == 0


def _has_mark(path: Path) -> bool:
    cmd = ("if (Get-Item -LiteralPath '%s' -Stream Zone.Identifier -ErrorAction SilentlyContinue) "
           "{ 'yes' } else { 'no' }" % str(path).replace("'", "''"))
    return H.childproc.run([H.POWERSHELL, "-NoProfile", "-Command", cmd], timeout=60).stdout.strip() == "yes"


def test_mark_of_the_web_is_found_and_removed_on_request(tmp_path):
    tree = H.setup_tree(tmp_path)
    target = tree / "scripts" / "detect_proxy.ps1"
    _mark(target)
    assert _has_mark(target)
    pydir = os.path.dirname(H.base_python())
    # Declined (no answer on stdin): continues, mark stays.
    env = H.clean_env(tree, H.minimal_path(pydir))
    r, out = _setup(tree, env)
    assert r.returncode == 0, out
    assert "Mark-of-the-Web" in out and "Left as it is." in out and _has_mark(target)
    # SETUP_UNBLOCK=1: removed, and setup continues.
    env = H.clean_env(tree, H.minimal_path(pydir), SETUP_UNBLOCK=1)
    r, out = _setup(tree, env)
    assert r.returncode == 0, out
    assert "Removing the mark" in out and "STUB-BOOTSTRAP" in out
    assert not _has_mark(target)


# ---- D17: line endings from .gitattributes -------------------------------------------------

def _git(*args, cwd):
    r = H.childproc.run(["git", *args], cwd=str(cwd), timeout=120)
    assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r.stdout


def test_gitattributes_delivers_crlf_batch_files_whatever_autocrlf_says(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not available")
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", cwd=src)
    _git("config", "user.email", "t@example.invalid", cwd=src)
    _git("config", "user.name", "t", cwd=src)
    _git("config", "core.autocrlf", "false", cwd=src)
    shutil.copyfile(H.REPO / ".gitattributes", src / ".gitattributes")
    # Committed as LF, which is what this repository's index holds for every .bat.
    (src / "setup.bat").write_bytes((H.REPO / "setup.bat").read_bytes().replace(b"\r\n", b"\n"))
    (src / "scripts").mkdir()
    for s in ("preflight_policy.ps1", "detect_proxy.ps1", "ca_bundle.ps1"):
        shutil.copyfile(H.REPO / "scripts" / s, src / "scripts" / s)
    (src / "scripts" / "bootstrap.py").write_text(H.STUB_BOOTSTRAP, encoding="ascii")
    _git("add", "-A", cwd=src)
    _git("commit", "-q", "-m", "x", cwd=src)
    assert "i/lf" in _git("ls-files", "--eol", "setup.bat", cwd=src)

    # core.eol=lf AS WELL AS autocrlf=false: that is a Linux machine -- and the server that builds
    # GitHub's "Download ZIP". On Windows core.eol defaults to crlf, so a plain `text` attribute
    # would pass here and still ship LF from GitHub; only eol=crlf survives both (the mutation
    # check of this test found exactly that, 2026-09-24).
    lf_machine = ["-c", "core.autocrlf=false", "-c", "core.eol=lf"]
    clone = tmp_path / "clone"
    r = H.childproc.run(["git", *lf_machine, "clone", "-q", str(src), str(clone)], timeout=120)
    assert r.returncode == 0, r.stderr
    data = (clone / "setup.bat").read_bytes()
    assert b"\r\n" in data and data.count(b"\n") == data.count(b"\r\n"), "LF-only .bat was delivered"

    arch = tmp_path / "a.zip"
    _git(*lf_machine, "archive", "--format=zip", "-o", str(arch), "HEAD", cwd=src)
    import zipfile
    zdata = zipfile.ZipFile(str(arch)).read("setup.bat")
    assert zdata.count(b"\n") == zdata.count(b"\r\n"), "the release ZIP would carry LF .bat files"

    # And the delivered file's labels work: the run reaches :have_python and the handoff.
    env = H.clean_env(clone, H.minimal_path(os.path.dirname(H.base_python())), SETUP_IGNORE_POLICY=1)
    r, out = _setup(clone, env)
    assert r.returncode == 0 and "STUB-BOOTSTRAP" in out, out


# ---- quickstart.bat ------------------------------------------------------------------------

STUB_SETUP = '@echo off\r\necho STUB-SETUP-RAN>"%~dp0setup_ran.txt"\r\nexit /b 0\r\n'
STUB_DEVTUNNEL = ("Add-Content -LiteralPath (Join-Path $PSScriptRoot '..\\devtunnel_args.txt') "
                  "-Value ($args -join ' ')\r\nexit 1\r\n")


def _quickstart_tree(root: Path, env_text: str) -> Path:
    tree = root / "qs repo"
    (tree / "scripts").mkdir(parents=True)
    H.crlf_copy(H.REPO / "quickstart.bat", tree / "quickstart.bat")
    (tree / "setup.bat").write_text(STUB_SETUP, encoding="ascii")
    for s in ("quickstart_lock.ps1", "detect_proxy.ps1", "env_file.py"):
        shutil.copyfile(H.REPO / "scripts" / s, tree / "scripts" / s)
    (tree / "scripts" / "setup_devtunnel.ps1").write_text(STUB_DEVTUNNEL, encoding="ascii")
    (tree / ".env").write_bytes(env_text.encode("utf-8"))
    H.make_venv(tree)
    # A git checkout whose upstream cannot be reached: `git fetch` fails, the stale
    # remote-tracking ref says "0 behind".
    _git("init", "-q", cwd=tree)
    _git("config", "user.email", "t@example.invalid", cwd=tree)
    _git("config", "user.name", "t", cwd=tree)
    (tree / "README").write_text("x", encoding="ascii")
    _git("add", "README", cwd=tree)
    _git("commit", "-q", "-m", "x", cwd=tree)
    branch = _git("symbolic-ref", "--short", "HEAD", cwd=tree).strip()
    _git("remote", "add", "origin", "https://127.0.0.1:9/nothing.git", cwd=tree)
    _git("update-ref", "refs/remotes/origin/" + branch, "HEAD", cwd=tree)
    _git("branch", "--set-upstream-to=origin/" + branch, cwd=tree)
    return tree


def _qs_env(tree):
    gitdir = os.path.dirname(shutil.which("git"))
    return H.clean_env(tree, H.minimal_path(gitdir))


def _needs_git():
    if not shutil.which("git"):
        pytest.skip("git not available")


def test_quickstart_n_removes_the_anonymous_opt_in_and_reports_a_failed_fetch(tmp_path):
    _needs_git()
    tree = _quickstart_tree(tmp_path, "MCP_API_KEY=k\r\n# — keep me\r\n"
                                      "MCP_TUNNEL_ALLOW_ANONYMOUS=1\r\nOTHER=1\r\n")
    r = H.run_cmd("quickstart.bat", tree, _qs_env(tree), stdin="N\r\n")
    out = r.stdout + r.stderr
    assert (tree / "setup_ran.txt").exists(), out
    # D25
    assert "COULD NOT CHECK FOR UPDATES" in out, out
    assert "Up to date." not in out
    # D4
    text = (tree / ".env").read_bytes().decode("utf-8")
    assert "MCP_TUNNEL_ALLOW_ANONYMOUS" not in text, text
    assert text == "MCP_API_KEY=k\r\n# — keep me\r\nOTHER=1\r\n", "other lines were disturbed"
    assert "Removed MCP_TUNNEL_ALLOW_ANONYMOUS" in out and "REVOKED" in out
    assert "no grant yet" not in out
    args = (tree / "devtunnel_args.txt").read_text(encoding="utf-8", errors="replace")
    assert "-ForceAnonymous" not in args
    # stopped at the stub's STEP 4 failure, and released the lock on the way out
    assert r.returncode == 1, out
    assert not (tree / ".setup" / "quickstart.lock").exists()


def test_quickstart_a_writes_the_opt_in_once(tmp_path):
    _needs_git()
    tree = _quickstart_tree(tmp_path, "MCP_TUNNEL_ALLOW_ANONYMOUS=0\r\nX=1\r\nMCP_TUNNEL_ALLOW_ANONYMOUS=0\r\n")
    r = H.run_cmd("quickstart.bat", tree, _qs_env(tree), stdin="A\r\n")
    out = r.stdout + r.stderr
    text = (tree / ".env").read_text(encoding="utf-8")
    assert text.count("MCP_TUNNEL_ALLOW_ANONYMOUS") == 1 and "MCP_TUNNEL_ALLOW_ANONYMOUS=1" in text, text
    assert "Recorded: anonymous access." in out
    assert "-ForceAnonymous" in (tree / "devtunnel_args.txt").read_text(encoding="utf-8", errors="replace")


def test_quickstart_t_removes_the_opt_in_and_selects_tenant_mode(tmp_path):
    """T asks for NO tenant id any more (D16: devtunnel's --tenant is a flag for the signed-in
    account's tenant). It must still select tenant mode in setup_devtunnel (non-empty
    -TenantId) and say which tenant that is."""
    _needs_git()
    tree = _quickstart_tree(tmp_path, "MCP_TUNNEL_ALLOW_ANONYMOUS=1\r\n")
    r = H.run_cmd("quickstart.bat", tree, _qs_env(tree), stdin="T\r\n")
    out = r.stdout + r.stderr
    assert "MCP_TUNNEL_ALLOW_ANONYMOUS" not in (tree / ".env").read_text(encoding="utf-8"), out
    assert "REVOKED" in out and "No id is needed" in out
    assert "tenant id (GUID)" not in out, "the useless tenant-id prompt is back"
    assert "access=tenant" in (tree / ".setup" / "tunnel_access_choice").read_text(encoding="ascii")
    args = (tree / "devtunnel_args.txt").read_text(encoding="utf-8", errors="replace")
    assert "-TenantId signed-in-account" in args and "-ForceAnonymous" not in args


def test_a_second_quickstart_is_refused_before_it_changes_anything(tmp_path):
    _needs_git()
    tree = _quickstart_tree(tmp_path, "X=1\r\n")
    holder = subprocess.Popen(["cmd", "/c", "ping -n 60 127.0.0.1 >nul"],
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        r = H.childproc.run([H.POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(tree / "scripts" / "quickstart_lock.ps1"), "acquire",
                             "-OwnerPid", str(holder.pid)], timeout=120)
        assert r.returncode == 0, r.stderr
        r = H.run_cmd("quickstart.bat", tree, _qs_env(tree), stdin="N\r\n")
        out = r.stdout + r.stderr
        assert r.returncode == 10, out
        assert "ALREADY RUNNING" in out
        assert not (tree / "setup_ran.txt").exists(), "STEP 1 ran despite the lock"
        assert (tree / ".setup" / "quickstart.lock").exists(), "the refused run dropped the lock"
    finally:
        holder.kill()

# -*- coding: utf-8 -*-
r"""setup.bat's uv installer policy, run for real (INST-10, 2026-09-24 review).

Two defects this fixed, both exercised against setup.bat's ACTUAL source lines (extracted by
anchor text, never reimplemented):

  1. UV_INSTALLER_URL let an environment variable point the installer fetch at ANY host, whose
     response then ran with this user's privileges (piped into Invoke-Expression). Fixed: a host
     other than astral.sh is ignored (falls back to the official URL) unless
     UV_INSTALLER_URL_ALLOW_UNTRUSTED=1 is also set. Tested hermetically -- no network needed --
     by extracting just that host check.

  2. The downloaded uv.exe was trusted once it merely existed and ran. Fixed: an Authenticode
     signature check first (uv.exe has shipped one since release 0.12.12, 2026-09-09), a SHA-256
     check against Astral's published checksum registry as the fallback for an unsigned build,
     and refusal (the file removed) if neither confirms it. Tested end-to-end: a local HTTP
     server stands in for UV_INSTALLER_URL, serving a fake "installer" that plants an UNSIGNED
     stub uv.exe (built by the same csc-based stub tests/_install_path_harness.py already uses
     for setup.bat's uv route) -- and the real download+verify block in setup.bat is run against
     it. The stub's fake version cannot appear in Astral's real checksum registry, so this
     refuses it regardless of whether the registry lookup itself succeeds or fails outright --
     both outcomes are "not verified", which is the fail-closed behavior being tested, not a
     bet on this sandbox's network reaching github.com.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

from tools import childproc  # noqa: E402
from _install_path_harness import uv_stub_exe, CSC  # noqa: E402

SETUP_BAT = os.path.join(REPO, "setup.bat")

SYSROOT = os.environ.get("SystemRoot", r"C:\Windows")
POWERSHELL = shutil.which("powershell") or os.path.join(
    SYSROOT, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not os.path.isfile(POWERSHELL),
    reason="setup.bat is a Windows cmd script (os.name=%r)" % os.name)

HOST_CHECK_START = 'if not exist ".setup\\bin" mkdir ".setup\\bin"'
HOST_CHECK_END = ')'  # the closing paren of the "if errorlevel 1 if not ... ( ... )" block
DOWNLOAD_BLOCK_END = (
    '"} catch { Write-Host (\'uv download failed: \' + $_.Exception.Message); exit 1 }"')


def _lines():
    return Path(SETUP_BAT).read_text(encoding="ascii").splitlines()


def _extract_host_check() -> str:
    lines = _lines()
    start = next(i for i, l in enumerate(lines) if HOST_CHECK_START in l)
    # The block is: mkdir line, "if not defined UV_INSTALLER_URL set ...", 5 comment lines,
    # the findstr line, the `if errorlevel 1 if not ... (` line, 4 body lines, then the lone `)`.
    end = next(i for i in range(start, start + 30) if lines[i].strip() == ')')
    return "\n".join(lines[start:end + 1])


def _extract_download_block() -> str:
    lines = _lines()
    start = next(i for i, l in enumerate(lines) if HOST_CHECK_START in l)
    end = next(i for i, l in enumerate(lines) if DOWNLOAD_BLOCK_END in l)
    return "\n".join(lines[start:end + 1])


def _write_driver(tree: Path, body: str, extra_tail: str = "") -> Path:
    driver = "\n".join([
        "@echo off",
        "setlocal EnableExtensions EnableDelayedExpansion",
        "cd /d \"%~dp0\"",
        body,
        extra_tail,
    ])
    p = tree / "driver.bat"
    p.write_bytes(driver.replace("\n", "\r\n").encode("ascii"))
    return p


#: Windows PowerShell 5.1's own documented default PSModulePath (user, all-users, system).
#: _run() below sets this for the driver.bat (and the "powershell" child it spawns internally
#: for the Authenticode check) instead of letting it inherit ours. This test process usually
#: runs under pwsh (PowerShell 7) -- GitHub Actions windows-latest's default shell for a `run:`
#: step -- and pwsh's PSModulePath (its own Modules dir first) makes 5.1 resolve
#: Get-AuthenticodeSignature to pwsh 7's own Microsoft.PowerShell.Security module (built for
#: .NET (Core), not the .NET Framework CLR 5.1 runs on) and fail to load it. Measured in CI,
#: 2026-09-24. `_extract_download_block()` above extracts only setup.bat's uv-download lines,
#: not the PSModulePath fix setup.bat now carries near its own top (a3415bf) -- that fix
#: protects a real run of the whole .bat, but not this narrower extracted snippet, so it is
#: repeated here for the same reason.
_PS51_DEFAULT_MODULE_PATH = os.pathsep.join([
    os.path.join(os.environ.get("UserProfile", ""), "Documents", "WindowsPowerShell", "Modules"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 "WindowsPowerShell", "Modules"),
    os.path.join(SYSROOT, "System32", "WindowsPowerShell", "v1.0", "Modules"),
])


def _run(tree: Path, env_extra: dict, driver_name="driver.bat"):
    env = dict(os.environ)
    # os.environ on Windows keeps whatever case the process inherited the name in (measured:
    # "PSMODULEPATH", all upper); env["PSModulePath"] below would otherwise ADD a second,
    # differently-cased entry rather than replace it, and CreateProcess would hand the child
    # BOTH -- silently undoing this override (measured against tests/_install_path_harness.py's
    # run_ps(), which had exactly this bug first).
    for _existing in [k for k in env if k.upper() == "PSMODULEPATH"]:
        del env[_existing]
    env["PSModulePath"] = _PS51_DEFAULT_MODULE_PATH
    env.update(env_extra)
    return childproc.run(["cmd", "/c", str(tree / driver_name)], cwd=str(tree), env=env, timeout=120)


# ---- 1. host restriction, hermetic (no network) ---------------------------------------------

def test_untrusted_host_is_ignored_without_opt_in(tmp_path):
    tree = tmp_path / "repo"
    tree.mkdir()
    body = _extract_host_check() + '\necho FINAL_URL=!UV_INSTALLER_URL!\n'
    _write_driver(tree, body)
    r = _run(tree, {"UV_INSTALLER_URL": "http://evil.example.com/install.ps1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOTE: UV_INSTALLER_URL is set to a host other than astral.sh" in r.stdout, r.stdout
    assert "FINAL_URL=https://astral.sh/uv/install.ps1" in r.stdout, r.stdout


def test_untrusted_host_is_used_with_explicit_opt_in(tmp_path):
    tree = tmp_path / "repo"
    tree.mkdir()
    body = _extract_host_check() + '\necho FINAL_URL=!UV_INSTALLER_URL!\n'
    _write_driver(tree, body)
    r = _run(tree, {"UV_INSTALLER_URL": "http://127.0.0.1:1/install.ps1",
                    "UV_INSTALLER_URL_ALLOW_UNTRUSTED": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOTE:" not in r.stdout, r.stdout
    assert "FINAL_URL=http://127.0.0.1:1/install.ps1" in r.stdout, r.stdout


def test_official_host_is_left_alone(tmp_path):
    tree = tmp_path / "repo"
    tree.mkdir()
    body = _extract_host_check() + '\necho FINAL_URL=!UV_INSTALLER_URL!\n'
    _write_driver(tree, body)
    r = _run(tree, {"UV_INSTALLER_URL": "https://astral.sh/uv/install.ps1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOTE:" not in r.stdout, r.stdout
    assert "FINAL_URL=https://astral.sh/uv/install.ps1" in r.stdout, r.stdout


# ---- 2. an unsigned uv.exe is refused, end to end --------------------------------------------

class _InstallerHandler(BaseHTTPRequestHandler):
    script_text = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = self.server.script_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(script_text: str):
    server = HTTPServer(("127.0.0.1", 0), _InstallerHandler)
    server.script_text = script_text
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.skipif(not os.path.isfile(CSC), reason="csc.exe not available to build the uv stub")
def test_an_unsigned_uv_exe_is_refused_end_to_end(tmp_path):
    stub = uv_stub_exe(tmp_path)
    if stub is None:
        pytest.skip("could not build the uv stub (csc.exe unavailable)")

    tree = tmp_path / "repo"
    (tree / ".setup" / "bin").mkdir(parents=True)

    # The fake "installer": a real .ps1, served over HTTP exactly like Astral's own, whose only
    # job is to plant the (unsigned) stub where setup.bat expects uv.exe to land.
    fake_installer = (
        "Copy-Item -LiteralPath '%s' -Destination (Join-Path $env:UV_INSTALL_DIR 'uv.exe') -Force\n"
        % str(stub).replace("'", "''")
    )
    server = _serve(fake_installer)
    try:
        url = "http://127.0.0.1:%d/install.ps1" % server.server_port
        body = _extract_download_block()
        _write_driver(tree, body)
        r = _run(tree, {"UV_INSTALLER_URL": url, "UV_INSTALLER_URL_ALLOW_UNTRUSTED": "1"})

        assert "carries no Authenticode signature" in r.stdout, r.stdout
        # Either the registry lookup could not confirm a match/hash for this stub version, or
        # the request itself failed -- both are refusals; the point being tested is that an
        # unsigned binary is never adopted just because it exists and runs.
        refused = ("Refusing this uv.exe" in r.stdout) or ("checksum" in r.stdout.lower())
        assert refused, r.stdout + r.stderr
        assert not (tree / ".setup" / "bin" / "uv.exe").exists(), (
            "the unsigned/unverified uv.exe was left in place instead of being removed")
    finally:
        server.shutdown()


NOTEPAD = os.path.join(SYSROOT, "System32", "notepad.exe")


@pytest.mark.skipif(not os.path.isfile(NOTEPAD), reason="notepad.exe not present on this machine")
def test_a_signed_binary_is_accepted_and_left_in_place(tmp_path):
    # notepad.exe stands in for a real, Authenticode-signed uv.exe (Status=Valid, Microsoft
    # signer) -- proving the fix does not also start refusing a GOOD download.
    tree = tmp_path / "repo"
    (tree / ".setup" / "bin").mkdir(parents=True)
    fake_installer = (
        "Copy-Item -LiteralPath '%s' -Destination (Join-Path $env:UV_INSTALL_DIR 'uv.exe') -Force\n"
        % NOTEPAD.replace("'", "''")
    )
    server = _serve(fake_installer)
    try:
        url = "http://127.0.0.1:%d/install.ps1" % server.server_port
        body = _extract_download_block()
        _write_driver(tree, body)
        r = _run(tree, {"UV_INSTALLER_URL": url, "UV_INSTALLER_URL_ALLOW_UNTRUSTED": "1"})

        assert "Authenticode signature: Valid" in r.stdout, r.stdout
        assert (tree / ".setup" / "bin" / "uv.exe").exists(), (
            "a validly-signed uv.exe was removed instead of accepted")
    finally:
        server.shutdown()

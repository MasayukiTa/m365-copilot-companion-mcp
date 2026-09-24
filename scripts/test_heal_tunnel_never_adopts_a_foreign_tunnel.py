# -*- coding: utf-8 -*-
r"""heal_tunnel.ps1 applies setup_devtunnel.ps1's rules (2026-09-24), run for real against a STUB devtunnel.

heal_tunnel.ps1 runs on every start_all. Until this change it kept an owned .env tunnel name, or
switched .env to the account's FIRST owned tunnel, with no "is another machine hosting it" check,
no privacy guard and no "did this .env come from another machine" check -- the likeliest way a
second PC with a copied .env hosted this PC's tunnel that day (57ad0d1's investigation).

Each case builds a THROWAWAY tree (heal_tunnel.ps1, tunnel_name_util.ps1, tools/env_portability.py,
a .env) with a devtunnel.cmd stub on PATH that answers --version / user show / list / show from
files and logs its argv. The real heal runs; the .env it leaves is the assertion. No real
devtunnel, account or tunnel is touched. HEAL_SRC_DIR points the copy at another scripts/
directory (the before-measurement and the mutation checks), never at the live files edited.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tests"))

from _install_path_harness import minimal_path, clean_env, _PS51_DEFAULT_MODULE_PATH  # noqa: E402

SYSROOT = os.environ.get("SystemRoot", r"C:\Windows")
POWERSHELL = shutil.which("powershell") or os.path.join(
    SYSROOT, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")

pytestmark = pytest.mark.skipif(os.name != "nt" or not os.path.isfile(POWERSHELL),
                                reason="heal_tunnel.ps1 is Windows PowerShell only")

STUB = r"""@echo off
echo %*>>"%~dp0calls.log"
if "%1"=="--version" ( echo Tunnel CLI version: 1.0.0-stub & exit /b 0 )
if "%1"=="user" ( echo Logged in as stub@example.invalid using Microsoft. & exit /b 0 )
if "%1"=="list" ( type "%~dp0list.txt" & exit /b 0 )
if "%1"=="show" ( if exist "%~dp0show_%2.txt" type "%~dp0show_%2.txt" & exit /b 0 )
exit /b 1
"""


def _src_dir() -> str:
    return os.environ.get("HEAL_SRC_DIR") or os.path.join(REPO, "scripts")


def _ps(env, text):
    r = subprocess.run([POWERSHELL, "-NoProfile", "-Command", text], env=env,
                       capture_output=True, text=True, timeout=120)
    return r.stdout.strip()


class Rig:
    def __init__(self, tmp: Path):
        self.root = tmp / ("h" + uuid.uuid4().hex[:6])
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "tools").mkdir()
        for f in ("heal_tunnel.ps1", "tunnel_name_util.ps1"):
            shutil.copyfile(os.path.join(_src_dir(), f), self.root / "scripts" / f)
        shutil.copyfile(os.path.join(REPO, "tools", "env_portability.py"),
                        self.root / "tools" / "env_portability.py")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "devtunnel.cmd").write_text(STUB, encoding="ascii")
        pydir = os.path.dirname(getattr(sys, "_base_executable", None) or sys.executable)
        self.env = clean_env(self.root, minimal_path(self.bin, pydir))
        # Windows PowerShell 5.1's own module path, whatever shell runs pytest (d0190f9).
        for k in [k for k in self.env if k.upper() == "PSMODULEPATH"]:
            del self.env[k]
        self.env["PSModulePath"] = _PS51_DEFAULT_MODULE_PATH
        util = str(Path(REPO, "scripts", "tunnel_name_util.ps1")).replace("'", "''")
        self.own = _ps(self.env, ". '%s'; 'm365-copilot-companion-' + (Get-MachineSuffix)" % util)
        self.this_host = _ps(self.env, ". '%s'; Get-ThisHost" % util)
        assert self.own.startswith("m365-copilot-companion-") and self.this_host

    def tunnels(self, **hosts):
        """name -> host connections; every name gets a list row and a `show` file."""
        rows = ["Found %d tunnels." % len(hosts), "",
                "Tunnel ID                              Host Connections   Labels  Ports  Expiration"]
        for name, hc in hosts.items():
            rows.append("%s.usw2%s%d                          1      30 days" % (name, " " * 20, hc))
            (self.bin / ("show_%s.txt" % name)).write_text(
                "Tunnel ID             : %s.usw2\nHost connections      : %d\nPorts                 : 1\n"
                "  8000  auto   https://%s-8000.usw2.devtunnels.ms/\n" % (name, hc, name), encoding="ascii")
        (self.bin / "list.txt").write_text("\n".join(rows) + "\n", encoding="ascii")

    def write_env(self, **kv):
        (self.root / ".env").write_text("".join("%s=%s\n" % (k, v) for k, v in kv.items())
                                        + "MCP_API_KEY=keep-me\n", encoding="utf-8")

    def heal(self):
        r = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(self.root / "scripts" / "heal_tunnel.ps1")], env=self.env,
                           capture_output=True, text=True, timeout=180)
        return r.stdout

    def env_values(self) -> dict:
        out = {}
        for l in (self.root / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in l and not l.lstrip().startswith("#"):
                k, v = l.split("=", 1)
                out.setdefault(k.strip(), v.strip())
        return out

    def env_text(self) -> str:
        return (self.root / ".env").read_text(encoding="utf-8")


@pytest.fixture()
def rig(tmp_path):
    return Rig(tmp_path)


def _custom():
    return "custom-" + uuid.uuid4().hex[:8]


def test_a_carried_env_is_set_aside_and_this_machines_own_tunnel_used(rig):
    c = _custom()
    rig.tunnels(**{c: 0, rig.own: 0})
    rig.write_env(MCP_TUNNEL_NAME=c, MCP_TUNNEL_URL="https://%s-8000.usw2.devtunnels.ms/" % c,
                  MCP_TUNNEL_HOST="some-other-pc")
    out = rig.heal()
    v = rig.env_values()
    assert v["MCP_TUNNEL_NAME"] == rig.own, out + rig.env_text()
    assert v["MCP_TUNNEL_URL"] == "https://%s-8000.usw2.devtunnels.ms/" % rig.own, rig.env_text()
    assert v["MCP_TUNNEL_HOST"] == rig.this_host, rig.env_text()
    assert "# MCP_TUNNEL_NAME=%s" % c in rig.env_text(), "the carried value is kept readable"
    assert v["MCP_API_KEY"] == "keep-me"


def test_a_carried_env_without_an_own_tunnel_is_set_aside_not_kept(rig):
    c = _custom()
    rig.tunnels(**{c: 0})
    rig.write_env(MCP_TUNNEL_NAME=c, MCP_TUNNEL_URL="https://%s-8000.usw2.devtunnels.ms/" % c,
                  MCP_TUNNEL_HOST="some-other-pc")
    out = rig.heal()
    v = rig.env_values()
    assert v.get("MCP_TUNNEL_NAME") == rig.own, out + rig.env_text()
    assert "MCP_TUNNEL_URL" not in v and "MCP_TUNNEL_HOST" not in v, rig.env_text()
    assert v["MCP_API_KEY"] == "keep-me"


def test_an_owned_name_another_machine_is_hosting_is_not_kept(rig):
    c = _custom()
    rig.tunnels(**{c: 1, rig.own: 0})
    rig.write_env(MCP_TUNNEL_NAME=c, MCP_TUNNEL_URL="https://%s-8000.usw2.devtunnels.ms/" % c,
                  MCP_TUNNEL_HOST=rig.this_host)
    out = rig.heal()
    v = rig.env_values()
    assert v["MCP_TUNNEL_NAME"] == rig.own, out + rig.env_text()
    assert v["MCP_TUNNEL_URL"] == "https://%s-8000.usw2.devtunnels.ms/" % rig.own


def test_an_owned_name_this_machine_is_hosting_is_kept(rig):
    c = _custom()
    rig.tunnels(**{c: 1, rig.own: 0})
    rig.write_env(MCP_TUNNEL_NAME=c, MCP_TUNNEL_URL="https://%s-8000.usw2.devtunnels.ms/" % c,
                  MCP_TUNNEL_HOST=rig.this_host)
    before = rig.env_text()
    # A process on THIS machine whose command line is `devtunnel host <c>` (it only sleeps).
    dummy = subprocess.Popen(["cmd", "/c", "ping -n 60 127.0.0.1 >nul & rem devtunnel host %s" % c],
                             stdout=subprocess.DEVNULL)
    try:
        out = rig.heal()
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(dummy.pid)], capture_output=True)
    assert rig.env_text() == before, out


def test_never_the_accounts_first_tunnel(rig):
    other = _custom()
    rig.tunnels(**{other: 0})
    rig.write_env(MCP_TUNNEL_NAME="gone-" + uuid.uuid4().hex[:6],
                  MCP_TUNNEL_URL="https://nomatch-8000.usw2.devtunnels.ms/", MCP_TUNNEL_HOST=rig.this_host)
    before = rig.env_text()
    out = rig.heal()
    assert rig.env_text() == before, out + rig.env_text()
    assert "setup_devtunnel" in out, out


def test_a_healthy_own_env_is_left_alone(rig):
    rig.tunnels(**{rig.own: 1})
    rig.write_env(MCP_TUNNEL_NAME=rig.own, MCP_TUNNEL_URL="https://%s-8000.usw2.devtunnels.ms/" % rig.own,
                  MCP_TUNNEL_HOST=rig.this_host)
    before = rig.env_text()
    out = rig.heal()
    assert rig.env_text() == before, out
    assert "nothing to do" in out, out

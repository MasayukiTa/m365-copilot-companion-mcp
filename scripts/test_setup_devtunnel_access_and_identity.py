# -*- coding: utf-8 -*-
r"""setup_devtunnel.ps1, run for real against a STUB devtunnel: access, identity, transient faults.

Defects from the 2026-09-24 new-PC review this executes (IDs are that review's):

  D4  Choosing N or T after an earlier A left the tunnel anonymous: the anonymous branch won over
      -TenantId, and nothing ever revoked an anonymous grant.
  D16 `devtunnel access create --help` (CLI 1.0.1516): "-t, --tenant  Allow or deny all users in
      the current Entra tenant" -- a FLAG. `--tenant <GUID>` could not succeed.
  D7  A .env carried from another machine kept MCP_TUNNEL_NAME, so with the same account both
      PCs hosted one tunnel. The set-aside now covers the name, classified by
      tools/env_portability.merge_for_new_machine.
  D19 An empty/garbled `devtunnel list` or a "Forbidden" create renamed the tunnel.
  D22 The host stamp and name suffix now match bootstrap.py (platform.node(), sha256[:8]); the
      old COMPUTERNAME stamp and SHA1[:6] name are still recognised as this machine.
  D29 A USERNAME that is a substring of the generated name ("pan" in "companion") made the name
      "identifying" and printed "The PUBLIC URL will change" on every run.
  D9  After winget installs devtunnel the CLI is found under WinGet\Links (function-level).

HOW IT RUNS WITHOUT TOUCHING A REAL TUNNEL. The script is copied into a throwaway repo under
tmp_path with tools/env_portability.py beside it. A `devtunnel.cmd` stub, FIRST on a PATH that
is built from scratch, logs every argv and answers from a JSON state file; LOCALAPPDATA points
into tmp_path so the WinGet\Links / direct-download lookups cannot find the machine's real CLI.
The script resolves the CLI once and uses that path for Start-Process too (section 1), so even
the hidden `host` child is the stub. The stub always reports "Logged in" and returns a URL from
`show`, so no sign-in or host polling path is taken.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from tools import childproc  # noqa: E402

SETUP_PS1 = os.path.join(REPO, "scripts", "setup_devtunnel.ps1")
DOCTOR_PS1 = os.path.join(REPO, "scripts", "doctor.ps1")

_POWERSHELL = (
    shutil.which("powershell")
    or shutil.which("powershell.exe")
    or (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") else None)
)

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not _POWERSHELL,
    reason="setup_devtunnel.ps1 is Windows PowerShell (os.name=%r, powershell=%r)"
           % (os.name, bool(_POWERSHELL)),
)

GUID = "11111111-2222-3333-4444-555555555555"

# ── the stub CLI ─────────────────────────────────────────────────────────────────────────────

_STUB_PY = r'''
import json, os, sys
state_path = os.environ["STUB_STATE"]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
st = json.load(open(state_path, encoding="utf-8"))
def save():
    json.dump(st, open(state_path, "w", encoding="utf-8"))
a = sys.argv[1:]
def opt(name_short, name_long):
    for i, x in enumerate(a):
        if x in (name_short, name_long) and i + 1 < len(a):
            return a[i + 1]
    return None
def positionals(start):
    out, i = [], start
    while i < len(a):
        x = a[i]
        if x in ("-p", "--port-number", "--protocol"):
            i += 2; continue
        if x.startswith("-"):
            i += 1; continue
        out.append(x); i += 1
    return out
owned = st.setdefault("owned", {})
if a[:1] == ["--version"]:
    print("Tunnel CLI version: 1.0.0-stub"); sys.exit(0)
if a[:2] == ["user", "show"]:
    print("Logged in as test@example.com using Microsoft."); sys.exit(0)
if a[:1] == ["list"]:
    if st.get("list_mode") == "garbled":
        sys.exit(0)
    print("Found %d tunnels." % len(owned)); print("")
    print("Tunnel ID                              Host Connections     Labels     Ports")
    for n in owned:
        print("%s.jpe1      0            1" % n)
    sys.exit(0)
if a[:1] == ["show"]:
    n = a[1]
    if n not in owned:
        print("Tunnel not found."); sys.exit(1)
    print("Tunnel ID             : %s.jpe1" % n)
    for p in owned[n]["ports"]:
        print("  %s  http  https://%s-%s.jpe1.devtunnels.ms/" % (p, n, p))
    sys.exit(0)
if a[:1] == ["create"]:
    n = positionals(1)[0]
    err = st.get("create_error")
    if err:
        print(err); sys.exit(1)
    if n in owned or n in st.get("foreign", []):
        print("Conflict: tunnel ID %s already exists" % n); sys.exit(1)
    owned[n] = {"tunnel": ["+Anonymous [connect]"] if "--allow-anonymous" in a else [], "ports": {}}
    save(); print("Tunnel ID : %s.jpe1" % n); sys.exit(0)
if a[:2] == ["port", "list"]:
    for p in owned.get(a[2], {}).get("ports", {}):
        print("%s  http" % p)
    sys.exit(0)
if a[:2] == ["port", "create"]:
    owned[a[2]]["ports"][opt("-p", "--port-number")] = []
    save(); sys.exit(0)
if a[0] == "access":
    n = a[2]
    if n not in owned:
        print("Tunnel not found."); sys.exit(1)
    p = opt("-p", "--port-number")
    acl = owned[n]["ports"].setdefault(p, []) if p else owned[n]["tunnel"]
    if a[1] == "list":
        if not acl:
            print("No access control entries.")
        for e in acl:
            print(e)
        sys.exit(0)
    if a[1] == "reset":
        acl[:] = []; save(); sys.exit(0)
    if a[1] == "create":
        extra = positionals(3)
        if extra:
            print("Unrecognized command or argument '%s'." % extra[0]); sys.exit(1)
        if "--anonymous" in a or "-a" in a:
            acl.append("+Anonymous [connect]")
        if "--tenant" in a or "-t" in a:
            acl.append("+Tenant [connect]")
        save(); sys.exit(0)
if a[:1] == ["host"]:
    sys.exit(0)
print("stub: unhandled %r" % (a,)); sys.exit(2)
'''

_STUB_CMD = '@echo off\r\n"%STUB_PY%" "%~dp0devtunnel_stub.py" %*\r\nexit /b %ERRORLEVEL%\r\n'


def _bootstrap():
    import bootstrap  # noqa: WPS433  (scripts/bootstrap.py, the identity this must match)
    return bootstrap


def _this_suffix(user=None):
    node = platform.node()
    if user is None:
        return _bootstrap()._machine_suffix()
    return hashlib.sha256(("%s|%s" % (node, user)).lower().encode("utf-8")).hexdigest()[:8]


def _legacy_suffix(computername, username):
    return hashlib.sha1(("%s|%s" % (computername, username)).encode("utf-8")).hexdigest()[:6]


class Rig:
    def __init__(self, tmp_path, env_text, state, env_over=None):
        self.root = tmp_path / "repo"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "tools").mkdir()
        shutil.copy(SETUP_PS1, self.root / "scripts" / "setup_devtunnel.ps1")
        # setup_devtunnel.ps1 dot-sources tunnel_name_util.ps1 (Test-GeneratedTunnelName moved
        # there 2026-09-24, out of this file) from $PSScriptRoot -- it must sit next to the
        # copy above or the dot-source resolves nothing and every call to
        # Test-GeneratedTunnelName inside Test-IdentifyingTunnelName is a command-not-found
        # error, which is terminating regardless of $ErrorActionPreference.
        shutil.copy(os.path.join(REPO, "scripts", "tunnel_name_util.ps1"),
                    self.root / "scripts" / "tunnel_name_util.ps1")
        shutil.copy(os.path.join(REPO, "tools", "env_portability.py"),
                    self.root / "tools" / "env_portability.py")
        self.env_path = self.root / ".env"
        self.env_path.write_bytes(env_text.encode("utf-8"))
        self.stub = tmp_path / "stub"
        self.stub.mkdir()
        (self.stub / "devtunnel_stub.py").write_text(_STUB_PY, encoding="utf-8")
        (self.stub / "devtunnel.cmd").write_bytes(_STUB_CMD.encode("ascii"))
        self.state_path = tmp_path / "state.json"
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.log_path = tmp_path / "argv.log"
        self.log_path.write_text("", encoding="utf-8")
        lad = tmp_path / "localappdata"
        lad.mkdir()
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        env = dict(os.environ)
        for k in ("MCP_TUNNEL_ALLOW_ANONYMOUS", "PSModulePath"):
            env.pop(k, None)
        env.update({
            # BUILT FROM SCRATCH: the stub first, then the Python that runs it (the classifier
            # needs one too), then only the system directories cmd/powershell need.
            "PATH": os.pathsep.join([str(self.stub), os.path.dirname(sys.executable),
                                     os.path.join(sysroot, "System32"), sysroot,
                                     os.path.join(sysroot, r"System32\WindowsPowerShell\v1.0")]),
            "LOCALAPPDATA": str(lad),
            "STUB_PY": sys.executable,
            "STUB_STATE": str(self.state_path),
            "STUB_LOG": str(self.log_path),
        })
        env.update(env_over or {})
        self.env = env

    def run(self, *args, timeout=180):
        proc = childproc.run(
            [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(self.root / "scripts" / "setup_devtunnel.ps1"), *args],
            env=self.env, timeout=timeout, cwd=str(self.root))
        self.out = proc.stdout + proc.stderr
        self.rc = proc.returncode
        return self

    @property
    def calls(self):
        return [json.loads(l) for l in self.log_path.read_text(encoding="utf-8").splitlines() if l]

    @property
    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    @property
    def env_lines(self):
        return self.env_path.read_bytes().decode("utf-8").splitlines()

    def live(self, key):
        for ln in self.env_lines:
            if ln.startswith(key + "="):
                return ln.split("=", 1)[1]
        return None


def _all_acl(state, name):
    t = state["owned"][name]
    return list(t["tunnel"]) + [e for p in t["ports"].values() for e in p]


def _anon_tunnel(name):
    return {"owned": {name: {"tunnel": ["+Anonymous [connect]"],
                             "ports": {"8000": ["+Anonymous [connect]"]}}}}


def _mine_env(name, extra=""):
    host = _bootstrap()._this_host()
    return ("MCP_API_KEY=k\n%sMCP_TUNNEL_NAME=%s\nMCP_TUNNEL_URL=https://%s-8000.jpe1.devtunnels.ms/\n"
            "MCP_TUNNEL_HOST=%s\n" % (extra, name, name, host))


# ── D4 / D16: the chosen access is what the tunnel ends with ────────────────────────────────

def test_tenant_after_anonymous_revokes_anonymous_and_grants_the_flag_form(tmp_path):
    """The evidence's reproduction: .env still says MCP_TUNNEL_ALLOW_ANONYMOUS=1, the run gets
    -TenantId. Before: `access create ... --anonymous` twice and no --tenant."""
    name = "m365-copilot-companion-" + _this_suffix()
    rig = Rig(tmp_path, _mine_env(name, "MCP_TUNNEL_ALLOW_ANONYMOUS=1\n"), _anon_tunnel(name))
    rig.run("-TenantId", GUID)
    assert rig.rc == 0, rig.out
    calls = rig.calls
    assert not [c for c in calls if "--anonymous" in c or "--allow-anonymous" in c], calls
    assert ["access", "reset", name] in calls and ["access", "reset", name, "-p", "8000"] in calls
    # D16: the flag alone -- the stub rejects a GUID after it exactly as a stray argument
    assert ["access", "create", name, "--tenant"] in calls, calls
    assert not [c for c in calls if GUID in c], "the tenant id was passed to devtunnel"
    acl = _all_acl(rig.state, name)
    assert "+Anonymous [connect]" not in acl and "+Tenant [connect]" in acl, acl
    assert "ACCESS: TENANT ONLY" in rig.out and "NOT VERIFIED" in rig.out, rig.out
    assert "IGNORED for this run" in rig.out


def test_none_after_anonymous_revokes_it_and_says_nothing_can_connect(tmp_path):
    """N: quickstart (other agent) removes MCP_TUNNEL_ALLOW_ANONYMOUS from .env and passes no
    tenant. The anonymous grant from the earlier A must go, and the screen must say so."""
    name = "m365-copilot-companion-" + _this_suffix()
    rig = Rig(tmp_path, _mine_env(name), _anon_tunnel(name)).run("-TenantId", "")
    assert rig.rc == 0, rig.out
    assert _all_acl(rig.state, name) == [], rig.state
    assert "has an ANONYMOUS grant from an earlier choice" in rig.out
    assert "ACCESS: NONE" in rig.out, rig.out
    assert not [c for c in rig.calls if c[:2] == ["access", "create"]]


def test_anonymous_is_still_granted_when_it_is_the_choice(tmp_path):
    rig = Rig(tmp_path, "MCP_API_KEY=k\n", {"owned": {}}).run("-ForceAnonymous")
    assert rig.rc == 0, rig.out
    name = "m365-copilot-companion-" + _this_suffix()
    assert ["create", name, "--allow-anonymous"] in rig.calls, rig.calls
    assert "+Anonymous [connect]" in rig.state["owned"][name]["ports"]["8000"]
    assert "ACCESS: ANONYMOUS" in rig.out
    assert not [c for c in rig.calls if c[:2] == ["access", "reset"]]


# ── D7: a .env from another machine does not keep the other machine's tunnel ────────────────

def test_a_foreign_stamp_sets_aside_the_name_url_and_stamp(tmp_path):
    """Same account on both PCs = the carried name is in THIS account's list. Before the fix it
    was reused and both machines hosted it."""
    old = "m365-copilot-companion-deadbeef"
    text = ("MCP_API_KEY=k\nMCP_TUNNEL_NAME=%s\nMCP_TUNNEL_URL=https://%s-8000.jpe1.devtunnels.ms/\n"
            "MCP_TUNNEL_HOST=some-other-pc\n" % (old, old))
    rig = Rig(tmp_path, text, _anon_tunnel(old)).run("-ForceAnonymous")
    assert rig.rc == 0, rig.out
    mine = "m365-copilot-companion-" + _this_suffix()
    assert rig.live("MCP_TUNNEL_NAME") == mine, rig.env_lines
    assert rig.live("MCP_TUNNEL_HOST") == _bootstrap()._this_host()
    assert "# MCP_TUNNEL_NAME=" + old in rig.env_lines
    assert "# MCP_TUNNEL_HOST=some-other-pc" in rig.env_lines
    assert ["create", mine, "--allow-anonymous"] in rig.calls
    assert not [c for c in rig.calls if old in c and c[:1] in (["host"], ["port"], ["access"])]
    assert "MCP_TUNNEL_NAME, MCP_TUNNEL_URL, MCP_TUNNEL_HOST" in rig.out


def test_a_foreign_env_without_the_classifier_stops_instead_of_hosting_it(tmp_path):
    """No classifier = the carried tunnel cannot be set aside by the rules; carrying on would
    host the other machine's tunnel (D7 itself). It stops, nothing written, nothing created."""
    old = "m365-copilot-companion-deadbeef"
    text = ("MCP_API_KEY=k\nMCP_TUNNEL_NAME=%s\nMCP_TUNNEL_URL=https://%s-8000.jpe1.devtunnels.ms/\n"
            "MCP_TUNNEL_HOST=some-other-pc\n" % (old, old))
    rig = Rig(tmp_path, text, _anon_tunnel(old))
    os.remove(rig.root / "tools" / "env_portability.py")
    rig.run("-ForceAnonymous")
    assert rig.rc == 1, rig.out
    assert rig.calls == [], rig.calls
    assert rig.env_path.read_bytes().decode("utf-8") == text
    assert "could not be run" in rig.out


def test_an_unstamped_generated_name_from_another_machine_is_set_aside(tmp_path):
    old = "m365-copilot-companion-deadbeef"
    rig = Rig(tmp_path, "MCP_API_KEY=k\nMCP_TUNNEL_NAME=%s\n" % old, _anon_tunnel(old))
    rig.run("-ForceAnonymous")
    assert rig.rc == 0, rig.out
    assert rig.live("MCP_TUNNEL_NAME") == "m365-copilot-companion-" + _this_suffix()


def test_an_unstamped_custom_name_is_kept(tmp_path):
    """Nothing proves it foreign, and dropping a name this machine owns changes a working URL."""
    rig = Rig(tmp_path, "MCP_API_KEY=k\nMCP_TUNNEL_NAME=teamtunnel\n", _anon_tunnel("teamtunnel"))
    rig.run("-ForceAnonymous")
    assert rig.rc == 0, rig.out
    assert rig.live("MCP_TUNNEL_NAME") == "teamtunnel"
    assert not [c for c in rig.calls if c[:1] == ["create"]]


# ── D19: a transient CLI fault does not rename ──────────────────────────────────────────────

def test_an_unreadable_list_stops_without_creating_or_writing(tmp_path):
    name = "m365-copilot-companion-" + _this_suffix()
    state = _anon_tunnel(name)
    state["list_mode"] = "garbled"
    text = _mine_env(name)
    rig = Rig(tmp_path, text, state).run("-ForceAnonymous")
    assert rig.rc == 1, rig.out
    assert not [c for c in rig.calls if c[:1] == ["create"]], rig.calls
    assert rig.env_path.read_bytes().decode("utf-8") == text
    assert "did not return a readable list" in rig.out


def test_a_forbidden_create_fails_instead_of_renaming(tmp_path):
    state = {"owned": {}, "create_error": "Forbidden: the request was denied by policy"}
    text = "MCP_API_KEY=k\nMCP_TUNNEL_NAME=foo\n"
    rig = Rig(tmp_path, text, state).run("-ForceAnonymous")
    assert rig.rc == 1, rig.out
    creates = [c for c in rig.calls if c[:1] == ["create"]]
    assert creates == [["create", "foo", "--allow-anonymous"]], creates
    assert rig.env_path.read_bytes().decode("utf-8") == text
    assert "Nothing was renamed" in rig.out


def test_a_real_collision_reuses_the_suffixed_name_an_earlier_run_made(tmp_path):
    """B16: the suffixed name exists in the account, the recorded one belongs to someone else."""
    sfx = _this_suffix()
    state = {"owned": {"foo-" + sfx: {"tunnel": [], "ports": {}}}, "foreign": ["foo"]}
    rig = Rig(tmp_path, "MCP_API_KEY=k\nMCP_TUNNEL_NAME=foo\n", state).run("-ForceAnonymous")
    assert rig.rc == 0, rig.out
    assert rig.live("MCP_TUNNEL_NAME") == "foo-" + sfx
    assert ["create", "foo-" + sfx, "--allow-anonymous"] not in rig.calls


# ── D22: one identity, and the old one still counts as this machine ─────────────────────────

def test_a_legacy_computername_stamp_and_sha1_name_are_this_machine(tmp_path):
    user = os.environ.get("USERNAME", "")
    legacy_name = "m365-copilot-companion-" + _legacy_suffix("LEGACYBOX", user)
    text = ("MCP_API_KEY=k\nMCP_TUNNEL_URL=https://x-8000.jpe1.devtunnels.ms/\n"
            "MCP_TUNNEL_HOST=legacybox\n")
    rig = Rig(tmp_path, text, _anon_tunnel(legacy_name), {"COMPUTERNAME": "LEGACYBOX"})
    rig.run("-ForceAnonymous")
    assert rig.rc == 0, rig.out
    assert "set aside" not in rig.out, rig.out
    assert rig.live("MCP_TUNNEL_NAME") == legacy_name
    assert not [c for c in rig.calls if c[:1] == ["create"]]
    assert rig.live("MCP_TUNNEL_HOST") == _bootstrap()._this_host()


# ── D29: a short USERNAME inside the generated name is not "identifying" ────────────────────

def test_a_username_inside_the_generated_name_does_not_rename(tmp_path):
    over = {"USERNAME": "pan", "LOGNAME": "", "USER": "", "LNAME": ""}
    name = "m365-copilot-companion-" + _this_suffix(user="pan")
    rig = Rig(tmp_path, _mine_env(name), _anon_tunnel(name), over).run("-ForceAnonymous")
    assert rig.rc == 0, rig.out
    assert "URL will change" not in rig.out and "identifying" not in rig.out, rig.out
    assert rig.live("MCP_TUNNEL_NAME") == name


# ── function-level: extracted from the live file, run in an isolated PowerShell ─────────────

def _extract(text, marker):
    idx = text.index(marker)
    start = text.index("{", idx)
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[idx:i + 1]
    raise AssertionError("unbalanced braces at %r" % marker)


def _run_ps(tmp_path, body, env_over=None):
    p = tmp_path / "driver.ps1"
    p.write_text(body, encoding="utf-8")
    env = dict(os.environ)
    env.update(env_over or {})
    proc = childproc.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)],
                         env=env, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_the_machine_identity_is_bootstraps(tmp_path):
    src = open(SETUP_PS1, encoding="utf-8").read()
    fns = "\n".join(_extract(src, "function " + f) for f in
                    ("Get-ThisHost", "Get-ThisUser", "Get-MachineSuffix"))
    long_node = "A-Very-Long-HostName-20"
    body = fns + "\nWrite-Output (Get-ThisHost)\nWrite-Output (Get-MachineSuffix)\n" \
                 "Write-Output (Get-MachineSuffix '%s' 'Some.User')\n" % long_node
    host, sfx, long_sfx = _run_ps(tmp_path, body).split()
    b = _bootstrap()
    assert host == b._this_host()
    assert sfx == b._machine_suffix()
    assert long_sfx == hashlib.sha256(("%s|%s" % (long_node, "Some.User")).lower()
                                      .encode("utf-8")).hexdigest()[:8]
    assert host == socket.gethostname().lower()


def test_winget_links_is_found_after_install(tmp_path):
    """D9: PATH of this process predates the install; the Links shim is found anyway."""
    src = open(SETUP_PS1, encoding="utf-8").read()
    fn = _extract(src, "function Find-DevTunnelCli")
    lad = tmp_path / "lad"
    links = lad / "Microsoft" / "WinGet" / "Links"
    body = fn + ("\n$env:Path = ''\n$env:LOCALAPPDATA = '%s'\n"
                 "Write-Output (\"before=\" + (Find-DevTunnelCli))\n"
                 "New-Item -ItemType Directory -Force '%s' | Out-Null\n"
                 "Set-Content -LiteralPath '%s' -Value 'x'\n"
                 "Write-Output (\"after=\" + (Find-DevTunnelCli))\n"
                 % (lad, links, links / "devtunnel.exe"))
    out = _run_ps(tmp_path, body)
    assert "before=\r\n" in out or out.startswith("before=\n") or "before=" in out.splitlines()
    assert ("after=" + str(links / "devtunnel.exe")) in out.splitlines(), out


# ── D15: doctor agrees with setup_devtunnel on what a listing means ─────────────────────────

LISTINGS = {
    "+Anonymous [connect]": "anonymous",
    "Found 1 access control entry\n  +Anonymous [connect]\n  +Tenant [connect]": "anonymous",
    "+Tenant [connect]": "tenant",
    "No access control entries.": "none",
    "-Anonymous [connect]": "none",
}


def test_doctor_and_setup_read_an_access_listing_the_same_way(tmp_path):
    fns = ("Test-AnonymousInListing", "Test-TenantInListing", "Get-AccessGrantFromListing")
    for path in (SETUP_PS1, DOCTOR_PS1):
        src = open(path, encoding="utf-8").read()
        body = "\n".join(_extract(src, "function " + f) for f in fns) + "\n"
        for i, text in enumerate(LISTINGS):
            (tmp_path / ("l%d.txt" % i)).write_text(text, encoding="utf-8")
            body += "Write-Output (Get-AccessGrantFromListing (Get-Content -Raw '%s'))\n" % (
                tmp_path / ("l%d.txt" % i))
        got = _run_ps(tmp_path, body).split()
        assert got == list(LISTINGS.values()), (path, got)


@pytest.mark.parametrize("acl,expect", [
    (["+Anonymous [connect]"], "anonymous|True|False|0|0"),
    (["+Tenant [connect]"], "tenant|False|True|0|1"),
    ([], "none|False|False|1|0"),
    (None, "|False|True|0|1"),            # tunnel unknown to the CLI: exit 1 -> could not be read
])
def test_doctor_tunnel_access_check_runs_against_the_stub(tmp_path, acl, expect):
    """The doctor block itself (section 3c.1, top-level code), not only its parser: the stub CLI
    answers `access list` through doctor's own Start-Job runner, and the result row and the
    counters are what quickstart reads."""
    src = open(DOCTOR_PS1, encoding="utf-8").read()
    block = src[src.index("# 3c.1 The tunnel's ACCESS GRANT"):src.index("# 3c.2 The public URL")]
    add_result = _extract(src, "function Add-Result")
    state = {"owned": {}}
    if acl is not None:
        state["owned"]["tun"] = {"tunnel": acl, "ports": {"8000": []}}
    rig = Rig(tmp_path, "", state)
    body = (add_result + "\n$script:results = @(); $script:ok = 0; $script:bad = 0; $script:warn = 0;"
            " $script:unknown = 0\n$script:tunnelChainBroken = $false\n$tname = 'tun'\n"
            "$DevTunnel = '%s'\n" % (rig.stub / "devtunnel.cmd")) + block + (
            "\n$r = $script:results[-1]\n"
            "Write-Output ('' + $script:tunnelAccess + '|' + $r.ok + '|' + $r.indeterminate + '|'"
            " + $script:bad + '|' + $script:unknown)\n")
    out = _run_ps(tmp_path, body, rig.env)
    assert out.strip().splitlines()[-1] == expect, out


class _H(BaseHTTPRequestHandler):
    mode = "json"

    def log_message(self, *a):
        pass

    def do_GET(self):
        m = self.server.mode
        if m == "json":
            self._send(200, b'{"status": "ok", "server_pid": 4242}', "application/json")
        elif m == "html":
            self._send(200, b"<html><body>Sign in to continue to the dev tunnel</body></html>",
                       "text/html")
        elif m == "401":
            self._send(401, b"unauthorized", "text/plain")
        elif m == "redirect":
            self.send_response(302)
            # To ANOTHER host name (localhost, not 127.0.0.1) that answers 200 HTML, the way a
            # relay hands an unauthenticated caller to a sign-in page.
            self.send_header("Location", "http://localhost:%d/login" % self.server.redirect_to)
            self.end_headers()

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _server(mode):
    s = HTTPServer(("127.0.0.1", 0), _H)
    s.mode = mode
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def test_doctor_tunnel_probe_requires_this_servers_health_json(tmp_path):
    src = open(DOCTOR_PS1, encoding="utf-8").read()
    fn = _extract(src, "function Test-TunnelHealthAnswer")
    servers = {m: _server(m) for m in ("json", "html", "401", "redirect")}
    servers["redirect"].redirect_to = servers["html"].server_port
    try:
        body = fn + "\n"
        cases = [("json", "4242"), ("json", "999"), ("json", ""), ("html", "4242"),
                 ("401", "4242"), ("redirect", "4242")]
        for mode, pid in cases:
            url = "http://127.0.0.1:%d/mcp" % servers[mode].server_port
            body += ("$r = Test-TunnelHealthAnswer '%s' '%s' 5\n"
                     "Write-Output ('%s|%s|' + $r.Ok + '|' + $r.Why)\n" % (url, pid, mode, pid))
        out = _run_ps(tmp_path, body, {"NO_PROXY": "127.0.0.1,localhost"})
    finally:
        for s in servers.values():
            s.shutdown()
    rows = {tuple(l.split("|")[:2]): l.split("|", 3) for l in out.splitlines() if "|" in l}
    assert rows[("json", "4242")][2] == "True", out
    assert rows[("json", "")][2] == "True", out           # no local server: cannot compare pids
    assert rows[("json", "999")][2] == "False" and "another machine" in rows[("json", "999")][3]
    assert rows[("html", "4242")][2] == "False" and "not the server's /health JSON" in rows[("html", "4242")][3]
    assert rows[("401", "4242")][2] == "False" and "HTTP 401" in rows[("401", "4242")][3]
    assert rows[("redirect", "4242")][2] == "False" and "redirected to localhost" in rows[("redirect", "4242")][3], out

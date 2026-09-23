# -*- coding: utf-8 -*-
r"""start_all.ps1 brings the venv up to date with requirements.txt ITSELF (D5, owner's rule).

WHAT CHANGED AND WHY. start_all used to call `bootstrap.py --check-deps`, which compared the
stamp in .setup\state.json against requirements.txt and nothing else, and on a mismatch counted
"requirements.txt has changed since the last setup ... run setup.bat". A state.json written
before the stamp existed has no stamp, so every PC installed before it got that line on EVERY
start, forever -- and the remedy it named was one the owner does not accept: the only remedy
start_all may give for its own environment is running start_all.bat again.

Now (scripts/start_all.ps1, Invoke-DependencySync):
  * --check-deps asks whether the venv SATISFIES requirements.txt; a venv that does is recorded
    (stamp written) and nothing is reported;
  * one that does not is installed by bootstrap.py --sync-deps (its own install_deps step, under
    an OS lock, pip's output to .setup\logs), with the proxy/CA environment setup.bat gives pip;
  * a failure is counted with pip's own error line, the log path and "start_all.bat";
  * pip never runs while anything of this checkout runs from the venv: with nothing live, the
    ONE server rule (stale_server_check.py --server-action, requirements.txt as the changed
    path) lets start_all stop its supervisor, server and bridge first (the rest of start_all
    starts them again); with a fleet run or bridge turn live the install is put off and the
    summary says it will happen at the next start_all when nothing is running.

HOW. The functions are cut out of start_all.ps1 (balanced braces, as the other start_all tests
do) and run in a separate powershell against a temp checkout. bootstrap.py is the REAL module,
driven by a small script that stubs only pip and the installed-metadata probe (and records the
environment pip was given). ca_bundle.ps1 / detect_proxy.ps1 are stubs, so nothing reads this
machine's certificate store or proxy settings. Nothing touches the live server or .venv.
Windows-only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

START_ALL = os.path.join(HERE, "start_all.ps1")
_POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")

pytestmark = pytest.mark.skipif(os.name != "nt" or not _POWERSHELL,
                                reason="start_all.ps1 is Windows PowerShell only")

#: Tracked files the server-swap rule needs in the temp checkout (see test_start_all_install_path
#: for why every entry must be tracked).
_COPY = ["scripts/stale_server_check.py", "tools/__init__.py", "tools/childproc.py",
         "tools/deploy_freshness.py", "relay/fleet_reaper.py"]

_FUNCS = ["Get-ThisCheckoutServerProcesses", "Get-ServerStartEpoch", "ConvertTo-ServerActionResult",
          "Get-ServerAction", "Invoke-ServerAction", "Get-ThisCheckoutBridgeProcesses",
          "Stop-Bridge-Processes", "Get-ThisCheckoutFleetCoordinatorPids",
          "Get-ThisCheckoutSupervisorProcesses", "ConvertFrom-DepsSyncOutput",
          "Get-DepsProblemSummary", "Test-DepsInstallInProgress", "Set-PipNetworkEnvironment",
          "Restore-ProcessEnvironment", "Invoke-DependencySync", "Enter-StartAllLock",
          "Exit-StartAllLock", "Write-StartupSummary"]


def _extract_braced_block(text: str, start_marker: str) -> str:
    idx = text.index(start_marker)
    depth = 0
    for i in range(text.index("{", idx), len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[idx:i + 1]
    raise AssertionError("unbalanced braces at %r" % start_marker)


@pytest.fixture(scope="module")
def source() -> str:
    with open(START_ALL, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def functions(source) -> str:
    out = []
    for name in _FUNCS:
        m = re.search(r"(?m)^function %s\b" % re.escape(name), source)
        assert m, "start_all.ps1 no longer defines %s" % name
        out.append(_extract_braced_block(source, m.group(0)))
    return "\n\n".join(out)


#: The REAL bootstrap.main, with pip and the metadata probe stubbed. "Installing" makes the
#: probe report satisfied afterwards, as a real install does. Each pip run records the network
#: environment it was given -- the proxy/CA variables are the point of Set-PipNetworkEnvironment.
_DRIVER = r'''
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(os.environ["DEPS_ROOT"])
cfg = json.loads((root / "deps_cfg.json").read_text())
sys.path.insert(0, os.environ["DEPS_REPO"])
sys.path.insert(0, os.path.join(os.environ["DEPS_REPO"], "scripts"))
import bootstrap as B
B.STATE_FILE = root / ".setup" / "state.json"
B.DEPS_LOG_DIR = root / ".setup" / "logs"
B.TRANSCRIPT = root / ".setup" / "bootstrap.log"
B.REQUIREMENTS = root / "requirements.txt"
flag = root / "installed.flag"
B.venv_unsatisfied = lambda req=None: ([] if (cfg.get("satisfied") or flag.exists())
                                        else ["no-such-package-xyz is not installed"])
def _alive(pid):
    import psutil
    return psutil.pid_exists(pid)
def fake_pip(cmd, **kw):
    seen = {k: os.environ.get(k) for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                                           "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE")}
    # Which of the watched processes (the dummy server/supervisor/bridge) were still running
    # at the moment pip ran: the answer must be "none".
    watched = [int(p) for p in os.environ.get("WATCH_PIDS", "").split(",") if p]
    with open(root / "pip_runs.jsonl", "a") as fh:
        fh.write(json.dumps({"cmd": list(cmd), "env": seen,
                             "alive": [p for p in watched if _alive(p)]}) + "\n")
    if cfg.get("pip_fail"):
        kw["stdout"].write("Collecting no-such-package-xyz>=1\n"
                           "ERROR: Could not find a version that satisfies the requirement "
                           "no-such-package-xyz>=1 (from versions: none)\n"
                           "ERROR: No matching distribution found for no-such-package-xyz>=1\n")
        return 1
    flag.touch()
    return 0
B.subprocess.call = fake_pip
B._broken_distributions = lambda py: []
import tools.childproc
tools.childproc.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, "42", "")   # main.py: 42 tools
sys.exit(B.main(sys.argv[1:]))
'''

_STUB_CA = "\r\n".join([
    "param([string]$OutFile, [string]$ExtraPem)",
    "Add-Content -Path (Join-Path $PSScriptRoot 'ca.called') -Value $OutFile",
    "Set-Content -Path $OutFile -Value 'stub bundle' -Encoding ASCII",
    "Write-Output $OutFile", ""])
_STUB_PROXY = "Write-Output 'http://proxy.test:3128'\r\n"


@pytest.fixture()
def checkout(tmp_path):
    root = tmp_path / "co"
    for rel in _COPY:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(os.path.join(REPO, rel), dst)
    for d in (".fleet", ".setup"):
        (root / d).mkdir(exist_ok=True)
    (root / "scripts" / "bootstrap_driver.py").write_text(_DRIVER, encoding="utf-8")
    (root / "scripts" / "ca_bundle.ps1").write_text(_STUB_CA, encoding="ascii")
    (root / "scripts" / "detect_proxy.ps1").write_text(_STUB_PROXY, encoding="ascii")
    (root / "requirements.txt").write_text("no-such-package-xyz>=1\n", encoding="utf-8")
    (root / ".setup" / "state.json").write_text(json.dumps({"done": {"install_deps": True}}),
                                                encoding="utf-8")
    return root


def _cfg(root, **kw):
    (root / "deps_cfg.json").write_text(json.dumps(kw), encoding="utf-8")


def _q(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _refused_url():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d/status" % port


def _driver(functions, root, body):
    pre = "\n".join([
        '$ErrorActionPreference = "Continue"',
        "$root = %s" % _q(root),
        "$scriptDir = %s" % _q(os.path.join(str(root), "scripts")),
        "$script:venvPy = %s" % _q(sys.executable),
        "$script:bootstrapPy = %s" % _q(os.path.join(str(root), "scripts", "bootstrap_driver.py")),
        "$script:bridgeStatusUrl = %s" % _q(_refused_url()),
        "$script:startupFailures = @()",
        "$script:splash = $null",
        "function Set-SplashStatus($s, [string]$t) { }",
        "function Hide-Secrets([string]$text) { return $text }",
    ])
    return pre + "\n\n" + functions + "\n\n" + body + "\n"


def _env(root):
    env = dict(os.environ, DEPS_ROOT=str(root), DEPS_REPO=REPO)
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy",
              "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"):
        env.pop(k, None)
    return env


def _ps(tmp_path, text, env, timeout=240):
    p = tmp_path / ("drv_%s.ps1" % uuid.uuid4().hex[:8])
    p.write_text(text, encoding="utf-8-sig")
    return childproc.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)],
                         env=env, timeout=timeout)


def _result(r):
    for line in reversed(r.stdout.splitlines()):
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:"):])
    raise AssertionError("no RESULT line.\nstdout:\n%s\nstderr:\n%s" % (r.stdout[-3000:], r.stderr[-3000:]))


_SYNC = r"""
$verdict = Invoke-DependencySync $script:venvPy $script:bootstrapPy
"RESULT:" + (@{ verdict = $verdict; failures = @($script:startupFailures);
               proxyAfter = [string]$env:HTTPS_PROXY; caAfter = [string]$env:REQUESTS_CA_BUNDLE } | ConvertTo-Json -Compress)
"""


def _pip_runs(root):
    p = root / "pip_runs.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def _state(root):
    return json.loads((root / ".setup" / "state.json").read_text(encoding="utf-8"))


def test_a_satisfied_venv_without_a_stamp_is_recorded_and_silent(tmp_path, checkout, functions):
    """(a) The owner's daily line: no stamp, venv fine -> no failure, no install, stamp written."""
    _cfg(checkout, satisfied=True)
    r = _result(_ps(tmp_path, _driver(functions, checkout, _SYNC), _env(checkout)))
    assert r["verdict"] == "ok" and r["failures"] == [], r
    assert _pip_runs(checkout) == []
    assert _state(checkout).get("install_deps_requirements_sha256"), "the stamp was not recorded"
    assert not (checkout / "scripts" / "ca.called").exists(), "network setup ran with nothing to install"


def test_an_unsatisfied_venv_is_installed_with_setup_bats_network_environment(tmp_path, checkout, functions):
    """(b) start_all's half: it runs the install itself, pip sees the CA bundle and the proxy
    setup.bat would give it, and none of that leaks into what start_all launches afterwards."""
    _cfg(checkout)
    r = _result(_ps(tmp_path, _driver(functions, checkout, _SYNC), _env(checkout)))
    assert r["verdict"] == "installed" and r["failures"] == [], r
    runs = _pip_runs(checkout)
    assert len(runs) == 1 and runs[0]["cmd"][-2:] == ["-r", str(checkout / "requirements.txt")]
    seen = runs[0]["env"]
    bundle = str(checkout / ".setup" / "ca-bundle.pem")
    assert seen["HTTPS_PROXY"] == "http://proxy.test:3128" and seen["HTTP_PROXY"] == "http://proxy.test:3128"
    assert seen["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert seen["REQUESTS_CA_BUNDLE"] == bundle and seen["SSL_CERT_FILE"] == bundle
    assert r["proxyAfter"] == "" and r["caAfter"] == "", "pip's network settings leaked into start_all"
    assert _state(checkout).get("install_deps_requirements_sha256")
    logs = list((checkout / ".setup" / "logs").glob("deps_install_*.log"))
    assert len(logs) == 1, "a hidden install left no evidence"


def test_a_failed_install_is_counted_with_what_failed_and_start_all(tmp_path, checkout, functions):
    """(c) The summary line names pip's own error and the log, says start_all.bat, and contains
    no setup.bat -- checked in the summary FILE the operator reads, not just the array."""
    _cfg(checkout, pip_fail=True)
    summary = checkout / ".setup" / "logs" / "start_all_summary.txt"
    body = _SYNC.replace('"RESULT:"', "$null = Write-StartupSummary %s $script:startupFailures 'full'\n\"RESULT:\"" % _q(summary), 1)
    r = _result(_ps(tmp_path, _driver(functions, checkout, body), _env(checkout)))
    assert r["verdict"] == "failed", r
    assert len(r["failures"]) == 1, r
    line = r["failures"][0]
    assert "No matching distribution found for no-such-package-xyz>=1" in line
    assert "deps_install_" in line and str(checkout / ".setup" / "logs") in line
    assert "start_all.bat" in line
    text = summary.read_text(encoding="utf-8")
    assert "setup.bat" not in text, text
    assert "No matching distribution found" in text
    assert "install_deps_requirements_sha256" not in _state(checkout)


def _dummy_server(root):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(180)",
                             os.path.join(str(root), "main.py")])


def _dummy_supervisor(root):
    """A powershell whose command line names THIS checkout's supervisor.ps1 -- what the scoped
    scan looks for. It would restart the server by itself, so it has to be stopped too."""
    sup = os.path.join(str(root), "scripts", "supervisor.ps1")
    return subprocess.Popen([_POWERSHELL, "-NoProfile", "-Command", "Start-Sleep 180 # " + sup])


def _dummy_bridge(root):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(180)",
                             os.path.join(str(root), "bridge", "copilot_bridge.py")])


def _wait_dead(proc, sec=10):
    for _ in range(int(sec * 2)):
        if proc.poll() is not None:
            return True
        time.sleep(0.5)
    return proc.poll() is not None


def test_nothing_running_from_the_venv_while_pip_runs(tmp_path, checkout, functions):
    """Revised rule (2026-09-24, after an install under a running server and bridge broke both):
    with this checkout's supervisor, server and bridge up and NOTHING live, the one server rule
    (stale_server_check.py --server-action) allows the stop; all three are stopped BEFORE pip
    runs (pip's stub records which were still alive: none), and the install goes ahead."""
    (checkout / "main.py").write_text("# server\n", encoding="utf-8")
    _cfg(checkout)
    procs = [_dummy_supervisor(checkout), _dummy_server(checkout), _dummy_bridge(checkout)]
    try:
        time.sleep(3)
        assert all(p.poll() is None for p in procs), "a dummy died on its own; nothing below would be evidence"
        env = dict(_env(checkout), WATCH_PIDS=",".join(str(p.pid) for p in procs))
        run = _ps(tmp_path, _driver(functions, checkout, _SYNC), env)
        r = _result(run)
        assert r["verdict"] == "installed" and r["failures"] == [], r
        runs = _pip_runs(checkout)
        assert len(runs) == 1 and runs[0]["alive"] == [], "pip ran while these were up: %r" % runs
        # Stopped BY the one rule, not by chance: Invoke-ServerAction says so under the [deps] tag.
        assert re.search(r"\[deps\] server code is newer .* stopped it", run.stdout), run.stdout[-2000:]
        assert all(_wait_dead(p) for p in procs)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()


@pytest.mark.parametrize("what", ["fleet run marker", "fleet coordinator"])
def test_a_live_run_puts_the_install_off_in_plain_words(tmp_path, checkout, functions, what):
    """Something live -> nothing is stopped, pip does not run, and the summary says in plain
    words that the dependencies will be updated at the next start_all when nothing is running.
    No setup.bat, no "re-run it yourself now"."""
    (checkout / "main.py").write_text("# server\n", encoding="utf-8")
    _cfg(checkout)
    server = _dummy_server(checkout)
    extra = None
    try:
        if what == "fleet run marker":
            (checkout / ".fleet" / "fleet_run_active.json").write_text(
                json.dumps({"pid": os.getpid()}), encoding="utf-8")
        else:
            extra = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(180)",
                                      "-m", "relay.fleet_runner", str(checkout)])
        time.sleep(3)
        r = _result(_ps(tmp_path, _driver(functions, checkout, _SYNC), _env(checkout)))
        assert r["verdict"] == "deferred", r
        assert _pip_runs(checkout) == [], "installed while a run was live"
        assert server.poll() is None, "a live run's server was stopped"
        assert len(r["failures"]) == 1, r
        line = r["failures"][0]
        assert "will be updated automatically the next time start_all.bat runs while nothing is running" in line
        assert "no-such-package-xyz" in line
        assert "setup.bat" not in line.replace("start_all.bat", "")
        assert "install_deps_requirements_sha256" not in _state(checkout)
    finally:
        for p in (server, extra):
            if p is not None and p.poll() is None:
                p.kill()


_HOLD_INSTALL_LOCK = r'''
import sys, time
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/scripts")
import bootstrap as B
with B.install_lock(sys.argv[2]):
    print("held", flush=True)
    time.sleep(float(sys.argv[3]))
'''


def test_a_waiting_start_all_keeps_waiting_while_an_install_runs(tmp_path, checkout, functions):
    """(d) start_all's side of the serialisation: Test-DepsInstallInProgress sees bootstrap's
    lock, and a second start_all waiting on the Global mutex does not give up at its timeout
    (and count "close it in Task Manager") while the first is installing."""
    hold = tmp_path / "hold.py"
    hold.write_text(_HOLD_INSTALL_LOCK, encoding="utf-8")
    probe = '"RESULT:" + (@{ busy = (Test-DepsInstallInProgress) } | ConvertTo-Json -Compress)'
    holder = subprocess.Popen([sys.executable, str(hold), REPO, str(checkout / ".setup"), "30"],
                              stdout=subprocess.PIPE)
    try:
        assert holder.stdout.readline().strip() == b"held"
        assert _result(_ps(tmp_path, _driver(functions, checkout, probe), _env(checkout)))["busy"] is True
    finally:
        holder.kill()
        holder.wait()
    assert _result(_ps(tmp_path, _driver(functions, checkout, probe), _env(checkout)))["busy"] is False

    name = "Global\\m365-test-start-all-deps-%s" % uuid.uuid4().hex
    hold_mutex = r"""
$got = Enter-StartAllLock -Name %s -TimeoutSec 60
Start-Sleep -Seconds 5
Exit-StartAllLock
"RESULT:{}"
""" % _q(name)
    p = tmp_path / "mutex_holder.ps1"
    p.write_text(_driver(functions, checkout, hold_mutex), encoding="utf-8-sig")
    mholder = subprocess.Popen([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(2)
        wait = r"""
$a = Enter-StartAllLock -Name %s -TimeoutSec 1 -KeepWaitingWhile { $true } -MaxExtraSec 60
"RESULT:" + (@{ got = $a } | ConvertTo-Json -Compress)
""" % _q(name)
        assert _result(_ps(tmp_path, _driver(functions, checkout, wait), _env(checkout)))["got"] is True
    finally:
        mholder.communicate(timeout=120)


def test_wired_before_the_supervisor_and_never_says_setup_bat(source):
    """Order: after the update check (a pull that changes requirements.txt is installed on the
    same start) and before the supervisor -- and so the server -- is started. And no code line
    of the dependency path tells anyone to run setup.bat."""
    body = _extract_braced_block(source, "function Invoke-Startup")
    upd = body.index("Check-ForUpdates")
    dep = body.index("Invoke-DependencySync $script:venvPy $script:bootstrapPy")
    sup = body.index("Start-FreshSupervisor $envTn")
    assert upd < dep < sup
    assert "-KeepWaitingWhile { Test-DepsInstallInProgress }" in body
    assert "Test-DependenciesAreStale" not in source
    for fn in ("Invoke-DependencySync", "ConvertFrom-DepsSyncOutput", "Set-PipNetworkEnvironment"):
        code = "\n".join(l for l in _extract_braced_block(source, "function " + fn).splitlines()
                         if not l.strip().startswith("#"))
        assert "setup.bat" not in code, fn


def test_the_verdict_parser_reads_only_the_last_verdict_line(tmp_path, checkout, functions):
    body = r"""
$r = @{
  inst  = (ConvertFrom-DepsSyncOutput @('Transcript: x', 'deps: installed (pip output: C:\l.log)'));
  fail  = (ConvertFrom-DepsSyncOutput @('deps: ok', 'deps: failed: pip exploded -- re-run start_all.bat'));
  junk  = (ConvertFrom-DepsSyncOutput @('Traceback (most recent call last):', 'KeyError: x'));
  none  = (ConvertFrom-DepsSyncOutput @());
  need  = (Get-DepsProblemSummary @('The .venv does not satisfy requirements.txt: a 1 is installed, a>=2 is required; b is not installed; c; d; e'));
}
"RESULT:" + ($r | ConvertTo-Json -Compress -Depth 4)
"""
    r = _result(_ps(tmp_path, _driver(functions, checkout, body), _env(checkout)))
    assert r["inst"] == {"Verdict": "installed", "Detail": "(pip output: C:\\l.log)"}
    assert r["fail"]["Verdict"] == "failed" and r["fail"]["Detail"].startswith("pip exploded")
    assert r["junk"] == {"Verdict": "unknown", "Detail": "KeyError: x"}
    assert r["none"]["Verdict"] == "unknown"
    assert r["need"] == "a 1 is installed, a>=2 is required; b is not installed; c; and 2 more"

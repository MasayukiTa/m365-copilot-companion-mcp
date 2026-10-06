# -*- coding: utf-8 -*-
r"""The bridge's own restart decision, fixed to match the server's (new-PC analysis, the
bridge half of D12/D30).

commit 1f4588a wired the SERVER'S daily "is the running process older than its code" check
through stale_server_check.py --server-action: a RECURSIVE scan of tools/ and relay/
(tools.deploy_freshness.newer_than walks the whole tree, not just the top level) and a
decide_post_update_action call over fleet_is_running, which withholds the swap while a fleet
run, a review run or a bridge turn is live.

The BRIDGE'S daily check -- Bridge-Is-Outdated in start_all.ps1 -- was left exactly as it
always was: a plain PowerShell Get-ChildItem with no -Recurse over the top level of bridge/,
tools/ and relay/ only, and no question at all about whether a chat turn was in progress. A
change inside a subpackage (relay/selfimprove/, tools/auto/) was invisible to it, and a
manual `git pull` plus a double-click of start_all could kill the bridge in the middle of a
turn.

This file proves the fix two ways:

  * PORTABLE (runs on the ubuntu job too): stale_server_check.py --bridge-action, the new
    CLI form, against a real temp directory tree (a stale file two levels down, so the
    RECURSIVE part is actually exercised) and a real loopback /status server (a dummy HTTP
    server on a free port, answering the busy and idle shapes). No Windows API, no real
    bridge, no real repo.

  * WINDOWS-ONLY: the PowerShell side that asks that CLI -- Get-ThisCheckoutBridgeProcesses,
    Get-BridgeStartEpoch and Get-BridgeAction -- extracted out of scripts/start_all.ps1 by
    balanced-brace parsing (as scripts/test_a_silent_death_leaves_its_exit_code.py does) and
    run for real against a dummy process whose command line names copilot_bridge.py inside a
    throwaway checkout, so the "which process, since when" half of the fix -- not just the
    CLI it asks -- is exercised too.

Neither talks to a real bridge: the /status endpoint is a stub HTTP server this file starts
and stops, and the page-touching endpoints (/stream, /goal, /new, /switch, /history,
/upload) are never named anywhere in this file, by design -- see stale_server_check.py's
_bridge_state docstring for why only GET /status is ever asked.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

CLI = os.path.join(HERE, "stale_server_check.py")
START_ALL = os.path.join(HERE, "start_all.ps1")
_POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")


def _run(args, timeout=120):
    r = childproc.run([sys.executable, CLI] + list(args), timeout=timeout)
    assert r.returncode == 0, r.stderr[-800:]
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    return lines[-1], [l[5:] for l in lines if l.startswith("why: ")]


class _Bridge:
    """A loopback /status that answers with a fixed body, exactly like the one
    scripts/test_one_rule_stops_the_server.py uses for --server-action's own bridge probe --
    the two CLI forms must be indistinguishable to whatever is listening on the other end."""

    def __init__(self, body: bytes):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                                   # noqa: N802
                outer.paths.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.paths = []
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/status" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def _refused_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                       # nothing listens there any more
    return "http://127.0.0.1:%d/status" % port


def _tree(tmp_path):
    """A repo-shaped tree with a file TWO LEVELS DEEP under bridge/, plus tools/ and relay/
    and main.py -- the layout the old Get-ChildItem (no -Recurse) scan could not see into."""
    root = tmp_path / "repo"
    for rel in ("main.py", "bridge/copilot_bridge.py", "bridge/sub/deep.py",
                "tools/__init__.py", "tools/auto/forged.py", "relay/test_y.py"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# x\n", encoding="utf-8")
        os.utime(p, (1_000_000, 1_000_000))
    return root


# =================================================================== portable: --bridge-action

def test_a_subpackage_change_is_seen(tmp_path):
    """The bug: the old PowerShell scan read bridge/, tools/, relay/ without -Recurse."""
    root = _tree(tmp_path)
    os.utime(root / "bridge" / "sub" / "deep.py", (3_000_000, 3_000_000))
    verdict, why = _run(["--bridge-action", "--started-epoch", "2000000",
                         "--repo", str(root), "--bridge-status", _refused_url()])
    assert verdict == "swap-needed"
    assert why == ["newer than the running bridge: bridge/sub/deep.py"]


def test_a_server_only_change_does_not_restart_the_bridge(tmp_path):
    """main.py is the server's own entrypoint; copilot_bridge.py never imports it."""
    root = _tree(tmp_path)
    os.utime(root / "main.py", (3_000_000, 3_000_000))
    verdict, why = _run(["--bridge-action", "--started-epoch", "2000000",
                         "--repo", str(root), "--bridge-status", _refused_url()])
    assert verdict == "noop"
    assert why == []


def test_tests_do_not_count(tmp_path):
    root = _tree(tmp_path)
    os.utime(root / "relay" / "test_y.py", (3_000_000, 3_000_000))
    verdict, _ = _run(["--bridge-action", "--started-epoch", "2000000",
                       "--repo", str(root), "--bridge-status", _refused_url()])
    assert verdict == "noop"


@pytest.mark.parametrize("body,expected", [
    (b'{"turn_running": true, "busy": false}', "report-only"),
    (b'{"turn_running": false, "busy": true}', "report-only"),
    (b'{"turn_running": false, "busy": false}', "swap-needed"),
    (b'<html>proxy error</html>', "report-only"),          # unreadable is busy
])
def test_a_bridge_turn_withholds_its_own_restart(tmp_path, body, expected):
    root = _tree(tmp_path)
    os.utime(root / "tools" / "auto" / "forged.py", (3_000_000, 3_000_000))
    b = _Bridge(body)
    try:
        verdict, why = _run(["--bridge-action", "--started-epoch", "2000000",
                             "--repo", str(root), "--bridge-status", b.url])
    finally:
        b.close()
    assert verdict == expected
    assert b.paths == ["/status"], "only /status may be asked; the page endpoints are off limits"
    if expected == "report-only":
        assert any("bridge turn is live" in w for w in why)


def test_no_bridge_status_url_is_not_busy(tmp_path):
    """Nothing listening -> (False, False): the ordinary state right after a reboot must not
    block the restart the daily check exists to make."""
    root = _tree(tmp_path)
    os.utime(root / "bridge" / "sub" / "deep.py", (3_000_000, 3_000_000))
    verdict, _ = _run(["--bridge-action", "--started-epoch", "2000000",
                       "--repo", str(root), "--bridge-status", _refused_url()])
    assert verdict == "swap-needed"


def test_an_unchanged_bridge_is_never_probed(tmp_path):
    """decide_post_update_action's own first rule: nothing changed, so /status is not even
    asked -- an unchanged bridge is a no-op whatever a turn is doing."""
    root = _tree(tmp_path)
    b = _Bridge(b'{"turn_running": true}')
    try:
        verdict, _ = _run(["--bridge-action", "--started-epoch", "2000000",
                           "--repo", str(root), "--bridge-status", b.url])
    finally:
        b.close()
    assert verdict == "noop"
    assert b.paths == [], "a bridge that changed nothing must not be probed at all"


def test_a_failure_prints_no_verdict():
    """start_all reads a missing verdict as 'leave the bridge alone'; a crash must not print
    one -- the same contract --server-action's own equivalent test pins."""
    r = childproc.run([sys.executable, CLI, "--bridge-action"], timeout=60)
    assert r.returncode != 0
    assert not [l for l in r.stdout.splitlines()
               if l.strip() in ("noop", "report-only", "swap-needed")]


# =================================================================== Windows-only: start_all.ps1

_FUNCS = ["ConvertTo-ServerActionResult", "Get-ThisCheckoutBridgeProcesses",
          "Get-BridgeStartEpoch", "Get-BridgeAction"]

_COPY = ["scripts/stale_server_check.py", "tools/__init__.py", "tools/childproc.py",
         "tools/deploy_freshness.py"]


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
def bridge_functions() -> str:
    src = open(START_ALL, encoding="utf-8").read()
    out = []
    for name in _FUNCS:
        m = re.search(r"(?m)^function %s\b" % re.escape(name), src)
        assert m, "start_all.ps1 no longer defines %s" % name
        out.append(_extract_braced_block(src, m.group(0)))
    return "\n\n".join(out)


def _q(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


@pytest.fixture()
def checkout(tmp_path):
    root = tmp_path / "co"
    for rel in _COPY:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(os.path.join(REPO, rel), dst)
    for d in ("bridge", "tools", "relay"):
        (root / d).mkdir(exist_ok=True)
    return root


def _driver(functions, root, body, venv_py=None, bridge=None):
    pre = "\n".join([
        '$ErrorActionPreference = "Continue"',
        "$root = %s" % _q(root),
        "$scriptDir = %s" % _q(os.path.join(str(root), "scripts")),
        "$script:venvPy = %s" % _q(venv_py or sys.executable),
        "$script:bridgeStatusUrl = %s" % _q(bridge or _refused_url()),
    ])
    return pre + "\n\n" + functions + "\n\n" + body + "\n"


def _ps(tmp_path, text, timeout=120):
    p = tmp_path / ("drv_%s.ps1" % uuid.uuid4().hex[:8])
    p.write_text(text, encoding="utf-8-sig")
    r = childproc.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)],
                      timeout=timeout)
    return r


def _result(r):
    for line in reversed(r.stdout.splitlines()):
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:"):])
    raise AssertionError("no RESULT line.\nstdout:\n%s\nstderr:\n%s" % (r.stdout[-3000:], r.stderr[-3000:]))


def _dummy_bridge(root):
    """A process whose command line names copilot_bridge.py inside THIS checkout -- what
    Get-ThisCheckoutBridgeProcesses' scan looks for. scripts/ is in the path only so the
    match text matches production's command line shape; the interpreter never imports it."""
    marker = os.path.join(str(root), "bridge", "copilot_bridge.py")
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(180)", marker])


_ASK = r"""
$e = Get-BridgeStartEpoch
$a = Get-BridgeAction
"RESULT:" + (@{ epoch = $e; verdict = $a.Verdict; why = @($a.Why) } | ConvertTo-Json -Compress)
"""


@pytest.mark.skipif(os.name != "nt" or not _POWERSHELL,
                    reason="the PowerShell side (Get-BridgeAction) is Windows PowerShell only")
def test_no_bridge_process_is_a_noop(tmp_path, checkout, bridge_functions):
    r = _result(_ps(tmp_path, _driver(bridge_functions, checkout, _ASK)))
    assert r["epoch"] == 0
    assert r["verdict"] == "noop"
    assert any("no bridge process" in w for w in r["why"])


@pytest.mark.skipif(os.name != "nt" or not _POWERSHELL,
                    reason="the PowerShell side (Get-BridgeAction) is Windows PowerShell only")
def test_a_live_turn_withholds_the_bridge_restart_end_to_end(tmp_path, checkout, bridge_functions):
    """The mechanism the fix exists for: a real bridge-shaped process, a real stale file two
    levels deep under bridge/, and a real dummy /status -- busy keeps it up, idle restarts."""
    proc = _dummy_bridge(checkout)
    try:
        time.sleep(2)
        deep = checkout / "bridge" / "sub" / "deep.py"
        deep.parent.mkdir(parents=True, exist_ok=True)
        deep.write_text("# newer\n", encoding="utf-8")
        os.utime(deep, (time.time() + 30, time.time() + 30))

        busy = _Bridge(b'{"turn_running": true, "busy": false}')
        try:
            r = _result(_ps(tmp_path, _driver(bridge_functions, checkout, _ASK, bridge=busy.url)))
        finally:
            busy.close()
        assert r["epoch"] > 0, "the dummy bridge process of this checkout was not found"
        assert r["verdict"] == "report-only", r
        assert proc.poll() is None, "a live turn's bridge was not left running by the check"

        idle = _Bridge(b'{"turn_running": false, "busy": false}')
        try:
            r = _result(_ps(tmp_path, _driver(bridge_functions, checkout, _ASK, bridge=idle.url)))
        finally:
            idle.close()
        assert r["verdict"] == "swap-needed", r
        assert any("bridge/sub/deep.py" in w for w in r["why"])
    finally:
        if proc.poll() is None:
            proc.kill()

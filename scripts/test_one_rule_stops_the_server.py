# -*- coding: utf-8 -*-
"""One rule decides whether start_all may stop the running server -- and it sees every run.

new-PC analysis D12/D30. start_all.ps1 had two server-stopping paths with two rules: the
post-update tail re-implemented decide_post_update_action by hand, and the daily "server is
older than its code" check had no live-run test at all and only looked at the TOP LEVEL of
tools/ and relay/. Both now ask `stale_server_check.py --server-action`. These tests run that
CLI for real (a subprocess, the way start_all runs it) against temporary fleet directories, a
temporary repository tree and a stub bridge, so what is checked is what start_all executes.

They also pin two readers that were wrong: the review run's marker was never read (a swap could
land in a live review pipeline), and `_normalize` replaced a DOUBLE backslash, so a Windows path
with single separators was not recognised as server code (D29).

Portable: stdlib, temp dirs and a loopback HTTP server. Runs on the ubuntu job too.
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

import stale_server_check as S  # noqa: E402
from tools import childproc  # noqa: E402

CLI = os.path.join(HERE, "stale_server_check.py")


def _run(args, stdin=""):
    r = childproc.run([sys.executable, CLI] + list(args), input=stdin, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    return lines[-1], [l[5:] for l in lines if l.startswith("why: ")]


def _dead_pid():
    """A pid that belonged to a process which has exited."""
    p = childproc.run([sys.executable, "-c", "import os; print(os.getpid())"], timeout=60)
    return int(p.stdout.strip())


def _marker(d, name, pid):
    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
        json.dump({"pid": pid, "started": time.time()}, fh)


def _refused_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                       # nothing listens there any more
    return "http://127.0.0.1:%d/status" % port


class _Bridge:
    """A loopback /status that answers with a fixed body."""

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


# ---------------------------------------------------------------- the post-update form (stdin)

def test_a_server_change_with_nothing_running_is_a_swap(tmp_path):
    verdict, why = _run(["--server-action", "--fleet-dir", str(tmp_path),
                         "--bridge-status", _refused_url()], "tools/file_ops.py\ndocs/x.md\n")
    assert verdict == "swap-needed"
    assert any("tools/file_ops.py" in w for w in why)


def test_a_docs_only_update_is_a_noop_even_during_a_run(tmp_path):
    _marker(str(tmp_path), "fleet_run_active.json", os.getpid())
    verdict, _ = _run(["--server-action", "--fleet-dir", str(tmp_path)], "docs/x.md\nui/A.cs\n")
    assert verdict == "noop"


def test_a_live_fleet_run_withholds_the_swap(tmp_path):
    _marker(str(tmp_path), "fleet_run_active.json", os.getpid())
    verdict, why = _run(["--server-action", "--fleet-dir", str(tmp_path)], "relay/fleet_runner.py\n")
    assert verdict == "report-only"
    assert any("fleet run" in w for w in why)


def test_a_live_review_run_withholds_the_swap(tmp_path):
    """review_run_active.json was not read at all before."""
    _marker(str(tmp_path), "review_run_active.json", os.getpid())
    verdict, why = _run(["--server-action", "--fleet-dir", str(tmp_path)], "tools/x.py\n")
    assert verdict == "report-only"
    assert any("review run" in w for w in why)


def test_dead_markers_do_not_hold_the_server_hostage(tmp_path):
    dead = _dead_pid()
    _marker(str(tmp_path), "fleet_run_active.json", dead)
    _marker(str(tmp_path), "review_run_active.json", dead)
    # a dead fleet marker is authoritative even over a status.json still saying running,
    # exactly as _run_appears_live always ruled
    (tmp_path / "status.json").write_text('{"running": true}', encoding="utf-8")
    verdict, _ = _run(["--server-action", "--fleet-dir", str(tmp_path)], "tools/x.py\n")
    assert verdict == "swap-needed"


def test_an_unreadable_pid_counts_as_live(tmp_path):
    (tmp_path / "fleet_run_active.json").write_text('{"pid": "?"}', encoding="utf-8")
    verdict, _ = _run(["--server-action", "--fleet-dir", str(tmp_path)], "tools/x.py\n")
    assert verdict == "report-only"


def test_status_running_counts_when_there_is_no_marker(tmp_path):
    (tmp_path / "status.json").write_text('{"running": true}', encoding="utf-8")
    verdict, _ = _run(["--server-action", "--fleet-dir", str(tmp_path)], "tools/x.py\n")
    assert verdict == "report-only"


# ---------------------------------------------------------------- the bridge half of "idle"

@pytest.mark.parametrize("body,expected", [
    (b'{"turn_running": true, "busy": false}', "report-only"),
    (b'{"turn_running": false, "busy": true}', "report-only"),
    (b'{"turn_running": false, "busy": false}', "swap-needed"),
    (b'<html>proxy error</html>', "report-only"),          # unreadable is busy
])
def test_a_bridge_turn_withholds_the_swap(tmp_path, body, expected):
    b = _Bridge(body)
    try:
        verdict, _ = _run(["--server-action", "--fleet-dir", str(tmp_path),
                           "--bridge-status", b.url], "tools/x.py\n")
    finally:
        b.close()
    assert verdict == expected
    assert b.paths == ["/status"], "only /status may be asked; the page endpoints are off limits"


def test_no_bridge_listening_is_not_busy(tmp_path):
    verdict, _ = _run(["--server-action", "--fleet-dir", str(tmp_path),
                       "--bridge-status", _refused_url()], "tools/x.py\n")
    assert verdict == "swap-needed"


def test_a_proxy_in_the_environment_is_not_used_for_loopback(tmp_path, monkeypatch):
    b = _Bridge(b'{"turn_running": false, "busy": false}')
    # NO no_proxy exemption either: the machine this was written on sets NO_PROXY, which hid
    # the difference -- the mutation check passed with the proxy handler removed until these
    # were cleared.
    for k in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTTP_PROXY", _refused_url().replace("/status", ""))
    try:
        assert S._bridge_state(b.url) == (True, False)
    finally:
        b.close()


# ---------------------------------------------------------------- the daily form (file times)

def _tree(tmp_path):
    root = tmp_path / "repo"
    for rel in ("main.py", "tools/__init__.py", "tools/auto/forged.py", "relay/selfimprove/x.py",
                "relay/test_y.py", "docs/readme.md"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# x\n", encoding="utf-8")
        os.utime(p, (1_000_000, 1_000_000))
    return root


def test_a_subpackage_change_is_seen(tmp_path):
    """The PowerShell scan read tools/ and relay/ without -Recurse."""
    root = _tree(tmp_path)
    os.utime(root / "tools/auto/forged.py", (3_000_000, 3_000_000))
    verdict, why = _run(["--server-action", "--fleet-dir", str(tmp_path / "fleet"),
                         "--bridge-status", _refused_url(),
                         "--started-epoch", "2000000", "--repo", str(root)])
    assert verdict == "swap-needed"
    assert why == ["newer than the running server: tools/auto/forged.py"]


def test_tests_and_docs_do_not_count(tmp_path):
    root = _tree(tmp_path)
    for rel in ("relay/test_y.py", "docs/readme.md"):
        os.utime(root / rel, (3_000_000, 3_000_000))
    verdict, _ = _run(["--server-action", "--fleet-dir", str(tmp_path / "fleet"),
                       "--started-epoch", "2000000", "--repo", str(root)])
    assert verdict == "noop"


def test_a_newer_file_during_a_live_run_is_report_only(tmp_path):
    """The evidence file's reproduction for D12, minus start_all: a live run, a newer file."""
    root = _tree(tmp_path)
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    _marker(str(fleet), "fleet_run_active.json", os.getpid())
    os.utime(root / "main.py", (3_000_000, 3_000_000))
    verdict, _ = _run(["--server-action", "--fleet-dir", str(fleet),
                       "--started-epoch", "2000000", "--repo", str(root)])
    assert verdict == "report-only"


def test_a_failure_prints_no_verdict(tmp_path):
    """start_all reads a missing verdict as 'leave the server alone'; a crash must not print one."""
    r = childproc.run([sys.executable, CLI, "--server-action"], timeout=60)
    assert r.returncode != 0
    assert not [l for l in r.stdout.splitlines() if l.strip() in ("noop", "report-only", "swap-needed")]


# ---------------------------------------------------------------- UI exes vs their sources (D27)

def _ui(tmp_path):
    ui = tmp_path / "ui"
    ui.mkdir()
    for f in ("A.cs", "Shared.cs", "B.cs", "app.manifest"):
        (ui / f).write_text("//\n", encoding="utf-8")
        os.utime(ui / f, (1_000_000, 1_000_000))
    return ui, [("AppA", ["A.cs", "Shared.cs"]), ("AppB", ["B.cs"])]


def test_ui_missing_empty_and_older_are_all_rebuilt(tmp_path):
    ui, targets = _ui(tmp_path)
    assert S.stale_ui_targets(str(ui), targets) == [("AppA", "missing"), ("AppB", "missing")]
    (ui / "AppA.exe").write_bytes(b"")
    (ui / "AppB.exe").write_bytes(b"MZ")
    os.utime(ui / "AppB.exe", (2_000_000, 2_000_000))
    assert S.stale_ui_targets(str(ui), targets) == [("AppA", "empty")]
    (ui / "AppA.exe").write_bytes(b"MZ")
    os.utime(ui / "AppA.exe", (2_000_000, 2_000_000))
    assert S.stale_ui_targets(str(ui), targets) == []
    os.utime(ui / "Shared.cs", (3_000_000, 3_000_000))
    assert S.stale_ui_targets(str(ui), targets) == [("AppA", "older-than:Shared.cs")]
    os.utime(ui / "app.manifest", (4_000_000, 4_000_000))
    assert [n for n, _ in S.stale_ui_targets(str(ui), targets)] == ["AppA", "AppB"]


def test_ui_cli_reads_the_real_build_lines():
    """No list here: the names come from ui/rebuild_ui.ps1 through bench/ui_build_check."""
    from bench.ui_build_check import targets_from_rebuild_script
    r = childproc.run([sys.executable, CLI, "--ui-stale"], timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    names = [l.split()[0] for l in r.stdout.splitlines() if l.strip()]
    assert names == [n for n, _ in targets_from_rebuild_script()]
    assert all(l.split()[1] in ("ok", "rebuild") for l in r.stdout.splitlines() if l.strip())


# ---------------------------------------------------------------- _normalize (D29)

def test_a_single_backslash_path_is_server_code():
    assert S.is_server_code_path("tools\\file_ops.py")
    assert S.is_server_code_path("relay\\sub\\y.py")
    assert not S.is_server_code_path("ui\\FleetCockpit.cs")

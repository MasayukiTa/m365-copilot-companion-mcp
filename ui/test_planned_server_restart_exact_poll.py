# -*- coding: utf-8 -*-
"""Execute FleetCockpit's real server-health poll without interrupting the production server.

The harness compiles the shipping FleetCockpit sources plus a tiny alternate Main into one
assembly. It runs with --selftest, so the ordinary constructor never starts probes/stack repair.
The only seam is an internal selftest-only override for the server HTTP result and transition
file path; PollHealthOnce() itself, ReadPlannedServerTransition(), owner PID/birth checks and
SetDot() are the production implementations.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bench import ui_build_check as B  # noqa: E402
from tools import childproc  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "nt", reason="WPF/.NET Framework exact-poll harness is Windows-only")

HARNESS = r'''
using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Windows;

class HealthPollHarness
{
    static double Unix(DateTime utc)
    {
        return new DateTimeOffset(utc.ToUniversalTime()).ToUnixTimeSeconds();
    }

    static void WriteMarker(string path, string mode)
    {
        if (File.Exists(path)) File.Delete(path);
        if (mode == "none") return;
        var p = Process.GetCurrentProcess();
        int pid = p.Id;
        double born = Unix(p.StartTime);
        if (mode == "dead") pid = 999999;
        if (mode == "reused") born -= 10000.0;
        double now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        string json = "{\"state\":\"planned_restart\",\"reason\":\"exact-poll-test\","
                    + "\"started\":" + now.ToString(CultureInfo.InvariantCulture) + ","
                    + "\"expires\":" + (now + 240.0).ToString(CultureInfo.InvariantCulture) + ","
                    + "\"supervisor_pid\":" + pid.ToString(CultureInfo.InvariantCulture) + ","
                    + "\"supervisor_started\":" + born.ToString(CultureInfo.InvariantCulture) + "}";
        Directory.CreateDirectory(Path.GetDirectoryName(path));
        File.WriteAllText(path, json, new UTF8Encoding(false));
    }

    [STAThread]
    public static int Main(string[] args)
    {
        if (args.Length < 3 || args[0] != "--selftest") return 9;
        string marker = args[1];
        string mode = args[2];
        WriteMarker(marker, mode);
        var app = new Application();
        app.ShutdownMode = ShutdownMode.OnExplicitShutdown;
        string status = Path.Combine(Path.GetDirectoryName(marker), "status.json");
        var w = new CockpitWindow(status);
        string result = w.SelfTestServerHealthPoll(marker, false);
        Console.WriteLine(result);
        w.Close();
        return 0;
    }
}
'''


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    if not os.path.isfile(B.CSC):
        if os.environ.get("REQUIRE_CSC", "") == "1":
            pytest.fail("csc missing: " + B.CSC)
        pytest.skip("csc missing: " + B.CSC)
    targets = dict(B.targets_from_rebuild_script())
    srcs = targets["FleetCockpit"]
    root = tmp_path_factory.mktemp("healthpoll")
    ui = root / "ui"
    ui.mkdir()
    shutil.copytree(Path(B.UI) / "assets", ui / "assets")
    h = root / "HealthPollHarness.cs"
    h.write_text(HARNESS, encoding="utf-8")
    exe = ui / "HealthPollHarness.exe"
    cmd = [B.CSC, "/nologo", "/target:exe", "/main:HealthPollHarness", "/out:" + str(exe)]
    manifest = Path(B.UI) / "app.manifest"
    if manifest.is_file():
        cmd.append("/win32manifest:" + str(manifest))
    cmd += ["/r:" + x for x in B.refs]
    cmd += [str(Path(B.UI) / x) for x in srcs]
    cmd.append(str(h))
    r = childproc.run(cmd, timeout=180)
    assert r.returncode == 0 and exe.is_file(), (r.stdout, r.stderr)
    return exe


def _run(harness, tmp_path, mode):
    marker = tmp_path / "server_transition.json"
    env = dict(os.environ)
    env["TEMP"] = env["TMP"] = str(tmp_path)
    r = childproc.run([str(harness), "--selftest", str(marker), mode], env=env, timeout=120,
                      creationflags=childproc.headless_creationflags())
    assert r.returncode == 0, (r.stdout, r.stderr)
    return [x.strip() for x in r.stdout.splitlines() if x.strip()][-1]


def test_exact_poll_valid_owned_planned_restart_is_yellow(harness, tmp_path):
    out = _run(harness, tmp_path, "valid")
    assert out.startswith("Yellow|"), out
    assert "exact-poll-test" in out


@pytest.mark.parametrize("mode", ["none", "dead", "reused"])
def test_exact_poll_without_a_live_matching_owner_is_red(harness, tmp_path, mode):
    out = _run(harness, tmp_path, mode)
    assert out.startswith("Red|"), (mode, out)


def test_selftest_seam_is_gated_and_poll_body_is_not_reimplemented():
    src = (Path(B.UI) / "FleetCockpit.cs").read_text(encoding="utf-8-sig")
    assert "SelfTestServerHealthPoll" in src
    assert "WindowSelfTest.Active" in src[src.index("SelfTestServerHealthPoll"):src.index("SelfTestServerHealthPoll") + 1800]
    poll = src[src.index("void PollHealthOnce()"):src.index("// 1) Tunnel:")]
    assert "ReadPlannedServerTransition()" in poll
    assert "HealthState.Yellow" in poll and "HealthState.Red" in poll

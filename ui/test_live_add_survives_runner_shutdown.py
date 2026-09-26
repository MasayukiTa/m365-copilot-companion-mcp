# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UI = REPO / "ui"
FW = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319")
CSC = FW / "csc.exe"

pytestmark = pytest.mark.skipif(os.name != "nt" or not CSC.is_file(), reason="requires Windows csc")

HARNESS = r'''
using System;
using System.Collections.Generic;
class H {
  static int Main(string[] a) {
    var item = new Dictionary<string,object>(); item["text"] = "shutdown-race";
    var xs = new System.Collections.Generic.List<object>(); xs.Add(item);
    var p = new Dictionary<string,object>(); p["add_goal"] = xs;
    string path;
    if (!FleetCommands.WriteTracked(a[0], p, out path)) return 4;
    Console.WriteLine(path);
    return System.IO.File.Exists(path) ? 0 : 5;
  }
}
'''


def test_shared_writer_can_return_the_exact_file_it_atomically_landed(tmp_path):
    h = tmp_path / "H.cs"
    h.write_text(HARNESS, encoding="utf-8")
    exe = tmp_path / "H.exe"
    r = subprocess.run([str(CSC), "/nologo", "/target:exe", "/out:" + str(exe),
                        "/r:" + str(FW / "System.Web.Extensions.dll"),
                        str(UI / "FleetCommands.cs"), str(h)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and exe.is_file(), (r.stdout, r.stderr)
    state = tmp_path / "fleet"
    q = subprocess.run([str(exe), str(state)], capture_output=True, text=True, timeout=30)
    assert q.returncode == 0, (q.stdout, q.stderr)
    landed = Path(q.stdout.strip().splitlines()[-1])
    assert landed.is_file()
    assert landed.parent.name == "commands.d"


def test_live_composer_tracks_receipt_and_rescues_with_adopt_command():
    src = (UI / "FleetCockpit.cs").read_text(encoding="utf-8")
    i = src.index("void TryAddGoalsToLiveFleet()")
    block = src[i:i + 5500]
    assert 'patch["ack"] = ackPath' in block
    assert "SendTrackedCommand(patch, out commandPath)" in block
    assert "WatchLiveAddHandoff(commandPath, ackPath)" in block

    watcher = src[src.index("void WatchLiveAddHandoff("):]
    watcher = watcher[:watcher.index("\n    void ", 20)]
    assert "File.Exists(ackPath)" in watcher
    assert "if (RunIsLive()) return;" in watcher
    assert "SpawnFleetAdoptCommand(commandPath)" in watcher
    assert "lastRescue" in watcher, "the watcher must retry a rescue that lost the state-dir race"

    adopter = src[src.index("bool SpawnFleetAdoptCommand("):]
    adopter = adopter[:adopter.index("\n    ", 80) if "\n    " in adopter[80:] else len(adopter)]
    assert "--adopt-command" in src[src.index("bool SpawnFleetAdoptCommand("):src.index("bool SpawnFleetAdoptCommand(") + 2600]
    assert "CreateNoWindow = true" in src[src.index("bool SpawnFleetAdoptCommand("):src.index("bool SpawnFleetAdoptCommand(") + 2600]


def test_plain_control_commands_keep_using_untracked_compatibility_writer():
    src = (UI / "FleetCockpit.cs").read_text(encoding="utf-8")
    assert "return FleetCommands.Write(_fleetDir, patch);" in src
    assert "return FleetCommands.WriteTracked(_fleetDir, patch, out path);" in src

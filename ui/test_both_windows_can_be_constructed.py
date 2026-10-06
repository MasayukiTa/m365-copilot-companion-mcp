# -*- coding: utf-8 -*-
"""Both UI binaries, built from the real Build lines, can construct their main window.

## The crash class nothing else here catches

Every other UI test reads C# source, or compiles one file alone (the command writer, the send
path). None of them would notice the failure the operator meets first: a window whose
constructor throws, so that double-clicking CopilotChat.exe or FleetCockpit.exe shows nothing
at all. A text assertion cannot see it and a standalone compile of a piece cannot reach it.

So this builds BOTH exes exactly as they ship -- the source lists parsed out of
ui/rebuild_ui.ps1's `Build` lines by bench/ui_build_check.py (not a copy of them), the same csc
references, app.manifest embedded -- into a temp directory laid out like ui/, and runs each
with `--selftest` (ui/WindowSelfTest.cs): construct the main window through its ordinary
constructor, run one dispatcher cycle, exit 0; or print the exception and exit non-zero.

## What it does NOT prove

* Show / layout / render / Loaded. The selftest never shows the window, because the chat's
  Loaded handler writes settings.txt on a first run and a test must not change the operator's
  settings. A crash that only happens once the window is on screen is not caught here.
* That the window is usable, or that anything it would fetch over the network works: under
  --selftest the chat's bridge probe and HttpGet refuse, the cockpit's health poll never
  starts, and both windows get an empty scratch directory as their `.fleet`.

## Environment

Skips only on a non-Windows host. On Windows, a missing csc skips unless `REQUIRE_CSC=1`
(set in CI's Windows step), and then it fails. A build failure always fails.
CI (windows-latest) has not yet run this: whether a WPF Window can be constructed there
without an interactive desktop is a question about that runner. If it cannot, the failure
message below carries the exe's own exception text, which is the answer.
"""
from __future__ import annotations

import os
import shutil
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

UI = os.path.join(REPO, "ui")

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="the binaries under test are WPF exes built by the .NET Framework csc; there is "
           "nothing to construct off Windows")

#: What each binary's main window is called -- the selftest prints it on success.
WINDOW = {"CopilotChat": "ChatWindow", "FleetCockpit": "CockpitWindow"}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    from bench import ui_build_check as B

    if not os.path.isfile(B.CSC):
        msg = "csc.exe is not at %s" % B.CSC
        if os.environ.get("REQUIRE_CSC", "").strip() == "1":
            pytest.fail(msg + " and REQUIRE_CSC=1")
        pytest.skip(msg)
    targets = B.targets_from_rebuild_script()
    assert {n for n, _ in targets} == set(WINDOW), (
        "rebuild_ui.ps1 builds %r; this test knows how to smoke %r -- teach it the new one"
        % (sorted(n for n, _ in targets), sorted(WINDOW)))
    root = tmp_path_factory.mktemp("uibuild")
    # Laid out like the repository: <root>/ui/<exe>, with the glyph assets the windows load
    # from beside the exe, so the constructor takes the branch it takes in production.
    ui = root / "ui"
    ui.mkdir()
    shutil.copytree(os.path.join(UI, "assets"), str(ui / "assets"))
    exes = {}
    for name, srcs in targets:
        r = B.build(name, srcs, str(ui))
        exe = str(ui / (name + ".exe"))
        assert r.returncode == 0 and os.path.isfile(exe), (
            "%s did not build from its Build line (rc=%s):\n%s\n%s"
            % (name, r.returncode, r.stdout, r.stderr))
        exes[name] = exe
    return root, exes


@pytest.mark.parametrize("name", sorted(WINDOW))
def test_the_main_window_can_be_constructed(built, name, tmp_path):
    root, exes = built
    temp = tmp_path / "temp"
    temp.mkdir()
    env = dict(os.environ)
    env["TEMP"] = env["TMP"] = str(temp)
    r = childproc.run([exes[name], "--selftest"], env=env, timeout=120,
                      creationflags=childproc.headless_creationflags())
    assert r.returncode == 0, (
        "%s --selftest exited %s -- constructing its main window failed.\nstdout:\n%s\n"
        "stderr:\n%s" % (name, r.returncode, r.stdout, r.stderr))
    assert "selftest ok: " + WINDOW[name] in r.stdout, (
        "%s exited 0 without saying it built %s -- the selftest path did not run: %r"
        % (name, WINDOW[name], r.stdout))

    # It stayed off the fleet and off the settings: the exe's parent stands for the repository
    # root, where the real .fleet and .config/settings.txt live, and nothing new appeared there.
    # The scratch directory it was given instead is where its fleet state went.
    assert sorted(os.listdir(str(root))) == ["ui"], (
        "%s --selftest wrote beside itself: %r" % (name, sorted(os.listdir(str(root)))))
    scratch = [n for n in os.listdir(str(temp)) if n.startswith("ui-selftest-")]
    assert scratch, "%s --selftest did not use a scratch .fleet: %r" % (name, os.listdir(str(temp)))

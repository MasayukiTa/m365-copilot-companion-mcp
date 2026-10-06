# -*- coding: utf-8 -*-
"""A test run must not put a console window on the operator's desktop -- and must not do it by
making the product guard blind.

conftest.py::_test_children_get_no_console_window sets CREATE_NO_WINDOW on every child this
process starts whose caller did not decide `creationflags`. These tests pin the three things that
make that safe to rely on:

  * an undecided launch gets the flag, whichever door it came through (Popen, a direct
    _winapi.CreateProcess as multiprocessing makes, os.system);
  * a DECIDED launch is left exactly as the caller wrote it -- including an explicit 0, which is
    how tools/test_a_windowless_launch_really_has_no_window.py tests the real thing;
  * the source guard still sees an undecided product site, because a runtime default cannot add
    a keyword to a call in a file.

And one measurement, run every time: a `cmd -> chcp -> powershell -> cmd` chain started with
CREATE_NO_WINDOW by a parent that has NO console puts up no visible console window anywhere in
the chain. That is why one flag at the first hop is enough.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="console windows are a Windows mechanism")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


class _Refused(Exception):
    pass


@pytest.fixture
def spy(monkeypatch):
    """Record the flags a launch reaches CreateProcess with, and start nothing."""
    import _winapi
    assert getattr(_winapi.CreateProcess, "_a_test_run_decides_the_console", False), \
        "conftest's console default is not installed, so nothing below tests anything"
    seen = []

    def fake(app, cmd, pa, ta, inherit, flags, env, cwd, si):
        seen.append(flags)
        raise _Refused()

    monkeypatch.setattr(_winapi.CreateProcess, "__wrapped__", fake)
    return seen


def _launch(**kw):
    with pytest.raises(_Refused):
        subprocess.Popen([sys.executable, "-c", "pass"], **kw)


def test_an_undecided_launch_gets_no_window(spy):
    _launch()
    assert spy[-1] & NO_WINDOW


def test_an_explicit_zero_is_a_decision_and_is_left_alone(spy):
    _launch(creationflags=0)
    assert spy[-1] == 0


def test_a_decided_launch_is_not_given_extra_flags(spy):
    group = subprocess.CREATE_NEW_PROCESS_GROUP
    _launch(creationflags=group)
    assert spy[-1] == group


def test_the_decision_does_not_leak_into_the_next_undecided_launch(spy):
    _launch(creationflags=0)
    _launch()
    assert spy == [0, NO_WINDOW]


def test_a_launch_that_bypasses_popen_is_covered_too(spy):
    """multiprocessing's spawn calls _winapi.CreateProcess itself with flags 0."""
    import _winapi
    with pytest.raises(_Refused):
        _winapi.CreateProcess(sys.executable, "python -c pass", None, None, False, 0, None,
                              None, None)
    assert spy[-1] & NO_WINDOW


def test_os_system_goes_through_the_same_default(spy):
    with pytest.raises(_Refused):
        os.system("ver")
    assert spy[-1] & NO_WINDOW


def test_the_product_guard_still_sees_an_undecided_site(tmp_path):
    """The runtime default is installed in THIS process right now, and the scanner still
    reports a bare subprocess.run as undecided -- it reads source, not behaviour."""
    from tools import launch_sites as L
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(
        "import subprocess\n\ndef go():\n    subprocess.run(['git', 'status'])\n",
        encoding="utf-8")
    sites = L.scan_file("pkg/mod.py", str(tmp_path))
    assert [s["decided"] for s in sites] == [False]


_CHAIN_PARENT = r"""
import ctypes, ctypes.wintypes as W, json, subprocess, sys, threading, time
u = ctypes.windll.user32
CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow"}
def visible():
    out = set(); buf = ctypes.create_unicode_buffer(256)
    @ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def cb(h, _):
        if u.IsWindowVisible(h):
            u.GetClassNameW(h, buf, 256)
            if buf.value in CLASSES:
                out.add(int(h))
        return True
    u.EnumWindows(cb, 0)
    return out
before = visible(); seen = set(); stop = []
def sample():
    while not stop:
        seen.update(visible() - before); time.sleep(0.03)
t = threading.Thread(target=sample); t.start()
cmd = ('cmd /d /s /c "chcp 65001 >nul & powershell -NoProfile -Command '
       'Start-Sleep -Milliseconds 800; cmd /c ver"')
r = subprocess.run(cmd, capture_output=True, creationflags=int(sys.argv[2]))
time.sleep(0.3); stop.append(1); t.join()
json.dump({"parent_has_console": int(ctypes.windll.kernel32.GetConsoleWindow() != 0),
           "rc": r.returncode, "ver": r.stdout.decode("utf-8", "replace"),
           "new_windows": len(seen)}, open(sys.argv[1], "w"))
"""


def test_one_flag_at_the_first_hop_covers_the_whole_chain(tmp_path):
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pythonw):
        pytest.skip("no pythonw.exe to build a console-less parent with")
    script = tmp_path / "chain_parent.py"
    script.write_text(_CHAIN_PARENT, encoding="utf-8")
    out = tmp_path / "out.json"
    subprocess.run([pythonw, str(script), str(out), str(NO_WINDOW)], timeout=120,
                   creationflags=0)
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["parent_has_console"] == 0, "the parent had a console, so this proved nothing"
    assert res["rc"] == 0 and "Windows" in res["ver"], res
    assert res["new_windows"] == 0, res

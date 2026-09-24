"""The flash watcher must see a console-class window that lives for a fraction of a second, and
must name the process that put it up.

It is proven against a window this test makes itself: a child process registers a top-level
window of class ConsoleWindowClass, shows it WITHOUT activation at an off-screen position for
~150 ms, and exits. That is the shape of the owner's complaint (a console window that comes and
goes) with none of its cost -- it never takes the keyboard and it is never on screen.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows desktop mechanism")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from scripts.win import console_flash_watch as W  # noqa: E402

_FAKE = r"""
import ctypes, ctypes.wintypes as W, sys, time
u = ctypes.windll.user32; k = ctypes.windll.kernel32
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
u.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
u.DefWindowProcW.restype = ctypes.c_ssize_t
proc = WNDPROC(lambda h, m, w, l: u.DefWindowProcW(h, m, w, l))
class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
                ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH),
                ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]
wc = WNDCLASS(); wc.lpfnWndProc = proc; wc.lpszClassName = "ConsoleWindowClass"
wc.hInstance = k.GetModuleHandleW(None)
u.RegisterClassW(ctypes.byref(wc))
u.CreateWindowExW.restype = W.HWND
WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_POPUP = 0x08000000, 0x80, 0x80000000
h = u.CreateWindowExW(WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW, "ConsoleWindowClass", "flash-probe",
                      WS_POPUP, -32000, -32000, 10, 10, None, None, wc.hInstance, None)
time.sleep(float(sys.argv[1]))
u.ShowWindow(h, 4)  # SW_SHOWNOACTIVATE
msg = W.MSG(); end = time.time() + 0.15
while time.time() < end:
    while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
        u.DispatchMessageW(ctypes.byref(msg))
    time.sleep(0.01)
u.DestroyWindow(h)
"""


def test_a_window_shorter_than_the_poll_is_caught_and_named(tmp_path):
    script = tmp_path / "fake_flash.py"
    script.write_text(_FAKE, encoding="utf-8")
    result = {}
    ready = threading.Event()

    def run():
        result["flashes"] = W.watch(0.08, 1.0, str(tmp_path / "f.jsonl"), emit=lambda *_: None,
                                    on_ready=ready.set)

    t = threading.Thread(target=run)
    t.start()
    assert ready.wait(120), "the watcher never finished its first process-table sample"
    child = subprocess.run([sys.executable, str(script), "0.5"], capture_output=True,
                           timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert child.returncode == 0, child.stderr
    t.join(60)
    mine = [f for f in result["flashes"] if f["title"] == "flash-probe"]
    assert mine, "a 150 ms console-class window went unseen: %r" % result["flashes"]
    ev = mine[0]
    # The poll interval here is a full second, so only the hook can have caught a 150 ms window.
    assert ev["via"] == "hook"
    assert ev["class"] == "ConsoleWindowClass"
    assert ev["owner_chain"] and "fake_flash.py" in ev["owner_chain"][0]["cmdline"]
    assert ev["under_repo"], "the owner runs this repo's interpreter, so it is the repo's"


def test_a_chain_survives_its_members_exiting():
    t = W.ProcTable()
    t.add({"pid": 10, "ppid": 1, "name": "python.exe", "created": 100.0, "exe": "",
           "cmdline": os.path.join(REPO, "main.py")})
    t.add({"pid": 20, "ppid": 10, "name": "cmd.exe", "created": 101.0, "exe": "", "cmdline": "cmd"})
    t.add({"pid": 30, "ppid": 20, "name": "powershell.exe", "created": 102.0, "exe": "",
           "cmdline": "powershell -Command x"})
    t.live.clear()                                   # all three are gone
    ev = W.attribute(t, 30, 102.2)
    assert [r["pid"] for r in ev["owner_chain"]] == [30, 20, 10]
    assert ev["under_repo"]


def test_a_recycled_pid_does_not_splice_a_younger_process_in_as_a_parent():
    t = W.ProcTable()
    t.add({"pid": 20, "ppid": 5, "name": "old.exe", "created": 50.0, "exe": "", "cmdline": "old"})
    t.add({"pid": 30, "ppid": 20, "name": "child.exe", "created": 60.0, "exe": "", "cmdline": "c"})
    t.add({"pid": 20, "ppid": 7, "name": "new.exe", "created": 70.0, "exe": "", "cmdline": "new"})
    names = [r["name"] for r in t.chain(30)]
    assert names[:2] == ["child.exe", "old.exe"]

# -*- coding: utf-8 -*-
"""OS-level thread-count regression for the OCR path, child-process edition (2026-11-12).

WHAT CHANGED. The leak this file used to measure came from torch's native CPU thread pool
being attached, once per OS thread, to whichever gateway thread ran readtext. The fix on
2026-11-12 moved EasyOCR/torch OUT OF THE GATEWAY PROCESS entirely, into a short-lived
child (tools/_easyocr_child.py). So torch now attaches its native pool to a CHILD process
that then exits -- the parent (gateway) process never grows at all.

WHAT THIS TEST ASSERTS. After a burst of OCR calls arriving on many different worker
threads, the GATEWAY process's OS thread count returns to its pre-burst baseline. Each OCR
call spawns a child, reads it, and the child exits; no native pool is ever attached to a
parent thread, so there is nothing to leak. A regression that moved torch back in-process
would reattach a native pool per worker thread and this would grow.

WHY A STUB CHILD, NOT REAL TORCH. Loading torch under this host's test launcher fails
(WinError 1114) and a real Reader needs a model download. The stub child only prints a JSON
line and exits -- it never imports easyocr/torch -- which is exactly the parent-side
behaviour under test: spawn, read, child exits, parent unchanged.

WHY toolhelp, NOT threading.enumerate(). The historical leak was NATIVE threads with no
Python frame, invisible to threading.enumerate() and py-spy; only an OS-level (Win32
toolhelp) count sees them. The gauge is kept so a regression to in-process torch -- whose
leak is native -- would still be caught. The gauge is Windows-only, so the whole module is
skipped off Windows (the leak's only platform); on Linux CI it collects zero items.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

if os.name != "nt":
    pytest.skip("OS-thread gauge is Windows-only (the leak's platform)", allow_module_level=True)

# Resolve ocr_ops from THIS worktree, never the live checkout. The live checkout root may
# sit on sys.path (it carries a competing `tools` package); keep its .venv site-packages but
# drop the root so ocr_ops comes from the clone under test.
_CLONE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLONE not in sys.path:
    sys.path.insert(0, _CLONE)

import ctypes  # noqa: E402
import ctypes.wintypes as w  # noqa: E402

from tools import ocr_ops  # noqa: E402

_k = ctypes.windll.kernel32


def _os_thread_count() -> int:
    snap = _k.CreateToolhelp32Snapshot(0x4, 0)
    if snap == -1:
        pytest.skip("toolhelp snapshot unavailable")

    class TE(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ThreadID", w.DWORD),
                    ("th32OwnerProcessID", w.DWORD), ("tpBasePri", ctypes.c_long),
                    ("tpDeltaPri", ctypes.c_long), ("dwFlags", w.DWORD)]

    te = TE(); te.dwSize = ctypes.sizeof(TE); pid = os.getpid(); n = 0
    try:
        if _k.Thread32First(snap, ctypes.byref(te)):
            while True:
                if te.th32OwnerProcessID == pid:
                    n += 1
                if not _k.Thread32Next(snap, ctypes.byref(te)):
                    break
    finally:
        _k.CloseHandle(snap)
    return n


class _FakeImage:
    def convert(self, mode):
        return self

    def save(self, path, format=None):
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n")


def _iters() -> int:
    try:
        n = int(os.environ.get("MCP_LEAK_REPRO_ITERS", "40"))
    except ValueError:
        n = 40
    return max(1, n)


def _write_stub_child(tmp_path) -> str:
    p = tmp_path / "stub_child.py"
    p.write_text(
        "import json, sys\n"
        "sys.stdout.write(json.dumps({'ok': True, 'text': 'ok'}))\n"
        "sys.stdout.write('\\n')\n",
        encoding="utf-8",
    )
    return str(p)


def test_ocr_burst_returns_gateway_thread_count_to_baseline(tmp_path, monkeypatch):
    """Many OCR calls on distinct worker threads must leave the GATEWAY process's OS thread
    count where it started: torch runs in child processes that exit, so nothing attaches a
    native pool to a parent thread."""
    child = _write_stub_child(tmp_path)
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)
    im = _FakeImage()

    # Warm-up: first call spawns and reaps one child; afterwards the parent is steady.
    assert ocr_ops._easyocr_image_text(im, "eng") == "ok"
    time.sleep(0.3)
    base = _os_thread_count()

    iters = _iters()
    for _ in range(iters):
        t = threading.Thread(target=lambda: ocr_ops._easyocr_image_text(im, "eng"))
        t.start()
        t.join()

    deadline = time.time() + 10
    while time.time() < deadline and _os_thread_count() > base:
        time.sleep(0.2)
    grew = _os_thread_count() - base

    assert grew <= 2, (
        "GATEWAY OS thread count grew by %d after %d OCR calls on distinct worker threads "
        "-- torch work is no longer isolated in a child process and a native pool is leaking "
        "onto parent threads again" % (grew, iters)
    )

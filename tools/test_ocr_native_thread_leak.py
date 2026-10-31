# -*- coding: utf-8 -*-
"""OS-level native-thread-leak regression for the OCR path (2026-10-31).

tools/test_ocr_thread_leak.py pins the STRUCTURAL property (OCR work runs on the single
dedicated ocr-easyocr thread). THIS test measures what the 22h leak was actually made of:
the PROCESS's OS thread count before and after a burst of OCR calls arriving on many
different worker threads -- and asserts it returns to baseline.

THE LEAK. EasyOCR runs on PyTorch. PyTorch's CPU backend builds an OpenMP/MKL intra-op
NATIVE thread pool the first time a torch op runs ON A GIVEN OS THREAD, ties it to that
thread, and never releases it when the thread returns. FastMCP runs every tool on a fresh
anyio worker thread, so OCR on the calling thread attached a fresh native pool per distinct
worker thread. Those threads carry no Python frame -> invisible to py-spy and absent from
threading.enumerate() (matching the live finding: py-spy saw 24 Python threads while
Windows reported 524 OS threads, ~500 native and parked in Wait).

WHY toolhelp, NOT threading.enumerate(). The leaked threads are NATIVE, so only an OS-level
count (Win32 toolhelp snapshot) sees them -- the very reason the live 500 were py-spy- and
enumerate()-invisible.

WHY A CreateThread FAKE, NOT REAL TORCH. Loading torch's c10.dll fails under this host's
test subprocess launcher (WinError 1114), and a real EasyOCR Reader needs a model download.
The fake reader reproduces the exact leak SHAPE -- un-joined native OS threads attached, once
per OS thread, to whichever thread runs readtext -- using CreateThread. If ocr_ops funnels
readtext onto its one dedicated executor, the fake attaches its native threads exactly once;
if readtext runs on the caller, the fake attaches a fresh batch per distinct worker thread.
So this test fails (RED) against calling-thread execution and passes (GREEN) against the fix.
Measured: 40 workers -> RED +200 / GREEN +0; MCP_LEAK_REPRO_ITERS=200 -> RED +1000 / GREEN +0.

ITERATIONS. Default is small for quick CI; set MCP_LEAK_REPRO_ITERS=200 for the full
200-call reproduction the investigation called for. The assertion is the same either way:
after the burst the OS thread count is back at its pre-burst baseline.
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
_k.CreateThread.restype = w.HANDLE
_k.CreateThread.argtypes = [w.LPVOID, ctypes.c_size_t, w.LPVOID, w.LPVOID, w.DWORD, w.LPDWORD]


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


_THREADPROC = ctypes.WINFUNCTYPE(w.DWORD, w.LPVOID)
_kept = []  # keep handles/callback alive


@_THREADPROC
def _native_worker(p):
    _k.Sleep(0xFFFFFFFF)
    return 0


_wp = ctypes.cast(_native_worker, w.LPVOID)
_NATIVE_PER_POOL = 5
_tls = threading.local()


def _attach_native_pool_once():
    """One native pool per OS thread, created on first use and never released -- the torch
    CPU-backend behaviour. threading.local keys it to the running OS thread, so a second
    call on the SAME OS thread adds nothing (pool reused) while a NEW OS thread adds one."""
    if not getattr(_tls, "warm", False):
        for _ in range(_NATIVE_PER_POOL):
            tid = w.DWORD(0)
            _kept.append(_k.CreateThread(None, 0, _wp, None, 0, ctypes.byref(tid)))
        _tls.warm = True


class _FakeReader:
    def readtext(self, arr, detail=0):
        _attach_native_pool_once()
        return ["ok"]


class _FakeImage:
    def convert(self, mode):
        return self


def _iters() -> int:
    try:
        n = int(os.environ.get("MCP_LEAK_REPRO_ITERS", "40"))
    except ValueError:
        n = 40
    return max(1, n)


def test_ocr_burst_returns_os_thread_count_to_baseline(monkeypatch):
    import importlib
    if importlib.util.find_spec("numpy") is None:
        pytest.skip("numpy not installed")
    import numpy as np
    # The fake image already yields the array; avoid needing real pixels.
    monkeypatch.setattr(np, "array", lambda x: x)

    monkeypatch.setitem(ocr_ops._EASYOCR_READERS, "en", _FakeReader())
    im = _FakeImage()

    # Warm-up: first call builds the one dedicated executor and attaches its single native
    # pool. After this, a correctly-funnelled implementation attaches nothing further.
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

    # Funnelled onto one thread => native pool built once, reused => zero net growth.
    # A regression to calling-thread execution adds _NATIVE_PER_POOL per distinct worker
    # (e.g. +200 over 40 iters, +1000 over 200), so a small ceiling cleanly separates them.
    assert grew <= 2, (
        "OS thread count grew by %d after %d OCR calls on distinct worker threads -- the "
        "native (torch-shaped) pool is leaking per worker thread; OCR work is not pinned to "
        "the single dedicated ocr-easyocr thread" % (grew, iters)
    )

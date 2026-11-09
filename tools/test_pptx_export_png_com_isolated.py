# -*- coding: utf-8 -*-
"""pptx_export_png must isolate PowerPoint COM so a native COM/RPC crash
cannot take down the MCP server process.

Root cause (faulthandler.log.1, 2026-11-07): every faulthandler "Current thread"
crash frame is tools/pptx_ops.py:pptx_export_png <- main.py:call_tool, with native
exceptions 0x80010108 (RPC_E_DISCONNECTED), 0x800706ba/0x800706be (RPC server
unavailable / call failed) and access violations. Those are SEH/native exceptions
raised by the shared PowerPoint COM server when it disconnects mid-call; Python's
`try/except Exception` cannot catch them, so the whole python.exe (the MCP server,
main.py on port 8000) dies and restarts.

The fix runs all COM work on a dedicated daemon worker thread and joins it with a
timeout, so:
  (1) the COM calls never run directly on the anyio call_tool worker thread, and
  (2) if the worker raises, the caller still returns a normal error string instead
      of letting the exception unwind through call_tool.

These tests stub out pythoncom/win32com so they run on Linux CI (no PowerPoint),
and prove the isolation contract without ever touching real COM.
"""
from __future__ import annotations

import sys
import types
import threading
from pathlib import Path

import pytest

from tools import pptx_ops  # noqa: E402


def _unlock(monkeypatch):
    monkeypatch.setattr(pptx_ops, "require_unlocked", lambda: None)


def _install_fake_com(monkeypatch, *, dispatch):
    """Install fake pythoncom + win32com.client modules.

    `dispatch` is called as dispatch() inside the COM worker to produce the fake
    PowerPoint.Application (or to raise, simulating a COM/RPC failure).
    """
    fake_pythoncom = types.ModuleType("pythoncom")
    fake_pythoncom.CoInitialize = lambda: None
    fake_pythoncom.CoUninitialize = lambda: None

    fake_win32com = types.ModuleType("win32com")
    fake_client = types.ModuleType("win32com.client")
    fake_client.DispatchEx = lambda _progid: dispatch()
    fake_win32com.client = fake_client

    monkeypatch.setitem(sys.modules, "pythoncom", fake_pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client)


def _make_pptx(tmp_path: Path) -> Path:
    p = tmp_path / "deck.pptx"
    p.write_bytes(b"PK\x03\x04 not a real pptx, COM is faked")
    return p


def test_com_runs_off_the_caller_thread(tmp_path, monkeypatch):
    """The COM work must execute on a DIFFERENT thread than the caller.

    This is the heart of the fix: call_tool hands pptx_export_png an anyio worker
    thread, and a native COM crash on that very thread kills the server. Running
    COM on a dedicated worker thread is what contains the blast radius.
    """
    _unlock(monkeypatch)
    caller_tid = threading.get_ident()
    seen = {}

    class _Pres:
        class PageSetup:
            SlideHeight = 7.5
            SlideWidth = 10.0
        def __init__(self):
            self.PageSetup = _Pres.PageSetup()
        def Export(self, out_dir, fmt, w, h):
            seen["com_tid"] = threading.get_ident()
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            (Path(out_dir) / "Slide1.PNG").write_bytes(b"\x89PNG\r\n")
        def Close(self):
            pass

    class _Presentations:
        Count = 0
        def Open(self, _path, WithWindow=False):
            return _Pres()
        def __call__(self, _i):
            raise AssertionError("no open deck in this test")

    class _App:
        def __init__(self):
            self.Presentations = _Presentations()
        def Quit(self):
            pass

    _install_fake_com(monkeypatch, dispatch=_App)
    p = _make_pptx(tmp_path)

    out = pptx_ops.pptx_export_png(str(p), str(tmp_path / "preview"))

    assert "com_tid" in seen, "COM Export never ran"
    assert seen["com_tid"] != caller_tid, (
        "COM ran on the caller thread; a native COM crash there would kill the "
        "MCP server. It must run on a dedicated worker thread."
    )
    assert "Exported" in out, out


def test_com_worker_failure_returns_error_not_raise(tmp_path, monkeypatch):
    """If the COM worker raises (stand-in for an RPC/COM disconnect), the caller
    returns an error STRING and the main thread keeps running -- it must not
    propagate out of pptx_export_png.
    """
    _unlock(monkeypatch)

    def _boom():
        raise OSError("-2147417848 the object invoked has disconnected "
                      "from its clients (0x80010108)")

    _install_fake_com(monkeypatch, dispatch=_boom)
    p = _make_pptx(tmp_path)

    out = pptx_ops.pptx_export_png(str(p), str(tmp_path / "preview"))

    assert isinstance(out, str)
    assert "error" in out.lower(), out
    # main thread still alive and able to run more work
    assert threading.main_thread().is_alive()

# -*- coding: utf-8 -*-
"""
MCP client for the Companion Secure Desktop Helper service -- the piece
that can see the Winlogon desktop (lock screen, UAC prompt), which no
in-session tool can reach.

The heavy half is a SYSTEM service in secure_desktop/
(secure_desktop_service.py + install.ps1). This module only talks to
its named pipe (\\\\.\\pipe\\companion-secure-desktop) with two fixed
commands, 'status' and 'capture', and reports plainly when the service
is not installed, so the tools are safe to call any time.

Install (one-time, needs an elevated PowerShell and a UAC approval):
    secure_desktop\\install.ps1
Uninstall:
    secure_desktop\\uninstall.ps1

A successful capture lands as a PNG under
C:\\ProgramData\\CompanionSecureDesktop\\captures\\ -- display it with
the usual read_image tool.
"""
from __future__ import annotations

import json
from pathlib import Path

_PIPE_NAME = r"\\.\pipe\companion-secure-desktop"
_INSTALL_HINT = (
    "The Secure Desktop Helper service is not installed, so the Winlogon "
    "desktop (lock screen / UAC) cannot be captured yet. One-time fix: "
    "run secure_desktop\\install.ps1 from an elevated PowerShell "
    "(it registers the service and starts it). Service folder: "
)


def _service_folder() -> str:
    try:
        # this file is tools/auto/desktop_secure.py; the service folder
        # sits at the repository root, two levels up
        return str(Path(__file__).resolve().parents[2] / "secure_desktop")
    except Exception:
        return "secure_desktop"


def _call_pipe(payload: dict, timeout_ms: int = 30000) -> dict:
    import time

    import pywintypes
    import win32file
    import win32pipe

    # WaitNamedPipe fails INSTANTLY with ERROR_FILE_NOT_FOUND when no
    # pipe instance exists at that moment -- and the server closes its
    # instance between requests, so there is a brief gap after every
    # reply where the name does not exist at all. The first live test
    # rode over the gap for 'status' and fell into it for 'capture'.
    # Retry for a few seconds before concluding the service is absent.
    handle = None
    deadline = time.time() + 5.0
    while True:
        try:
            win32pipe.WaitNamedPipe(_PIPE_NAME, 500)
            handle = win32file.CreateFile(
                _PIPE_NAME,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None)
            break
        except pywintypes.error as exc:
            if exc.winerror in (2, 121, 231) and time.time() < deadline:
                time.sleep(0.25)
                continue
            if exc.winerror in (2, 121, 231):
                raise FileNotFoundError("pipe absent") from exc
            raise
    try:
        win32pipe.SetNamedPipeHandleState(
            handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        win32file.WriteFile(handle, json.dumps(payload).encode("utf-8"))
        _hr, data = win32file.ReadFile(handle, 65536)
        return json.loads(data.decode("utf-8", errors="replace"))
    finally:
        handle.Close()


def screen_secure_status() -> str:
    """Report whether the Secure Desktop Helper service answers.

    Returns the service version, the active console session and the
    session list when installed; a plain 'not installed' report with
    the install path when it is not.
    """
    try:
        resp = _call_pipe({"cmd": "status"}, timeout_ms=3000)
    except FileNotFoundError:
        return _INSTALL_HINT + _service_folder()
    except Exception as exc:
        return f"Secure Desktop Helper did not answer cleanly: {exc}"
    if not resp.get("ok"):
        return f"Secure Desktop Helper answered with an error: {resp}"
    return (f"Secure Desktop Helper v{resp.get('version')} is running. "
            f"Console session: {resp.get('console_session')}. "
            f"Sessions: {resp.get('sessions')}")


def screen_secure_capture() -> str:
    """Capture the console session's Winlogon desktop via the service.

    Use this when the machine is locked or a UAC prompt is up and the
    in-session capture tools refuse: the SYSTEM service starts a worker
    inside the console session, captures its Winlogon desktop (falling
    back to that session's Default desktop), and returns the PNG path
    under C:\\ProgramData\\CompanionSecureDesktop\\captures\\. View it
    with read_image.
    """
    try:
        resp = _call_pipe({"cmd": "capture"}, timeout_ms=35000)
    except FileNotFoundError:
        return _INSTALL_HINT + _service_folder()
    except Exception as exc:
        return f"Secure Desktop Helper did not answer cleanly: {exc}"
    if not resp.get("ok"):
        return f"secure capture failed: {resp.get('error')}"
    return (f"captured desktop '{resp.get('desktop')}' of session "
            f"{resp.get('session')} -> {resp.get('png')} "
            f"({resp.get('width')}x{resp.get('height')}). "
            "View it with read_image.")

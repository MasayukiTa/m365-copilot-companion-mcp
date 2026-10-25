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

AUDIT-FIX NOTES (see FIXLOG.md): this revision makes timeout_ms real
(findings 1 and 9) and separates a WaitNamedPipe timeout (winerror 121)
from genuine pipe-absence (finding 5).
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

# Separate the 'service may be busy or starting' story from 'service is
# not installed' (finding 5). This is returned instead of the install
# hint when the only failures we saw were WaitNamedPipe timeouts (121),
# which mean a pipe exists but no instance was free in time.
_BUSY_HINT = (
    "The Secure Desktop Helper service did not answer within the allotted "
    "time. It may be busy or still starting (not necessarily uninstalled). "
    "Retry shortly; if it never answers, check the service in: "
)


class _PipeTimeout(Exception):
    """Raised when the pipe never became available before the deadline,
    but the failures were WaitNamedPipe timeouts (busy), not absence."""


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
    # Retry until the caller's deadline before concluding anything.
    #
    # findings 1/9: honour timeout_ms instead of a hardcoded 5.0 s so
    # that 'status' (short) and 'capture' (long) behave as declared.
    if timeout_ms is None or timeout_ms <= 0:
        timeout_ms = 30000
    deadline = time.time() + (timeout_ms / 1000.0)

    # finding 5: track whether every failure was a WaitNamedPipe timeout
    # (121). If so, the pipe is busy/starting, not absent, and we raise
    # _PipeTimeout so the caller can give a neutral message.
    saw_only_timeouts = True
    saw_any_failure = False

    handle = None
    while True:
        # finding 6 partial: evaluate the deadline at the top and size
        # the WaitNamedPipe wait from the remaining time so the overall
        # wait tracks timeout_ms instead of overshooting by a fixed 500.
        remaining = deadline - time.time()
        if remaining <= 0:
            if saw_any_failure and saw_only_timeouts:
                raise _PipeTimeout("pipe busy (WaitNamedPipe timed out)")
            raise FileNotFoundError("pipe absent")
        wait_ms = int(min(500, remaining * 1000.0))
        if wait_ms < 1:
            wait_ms = 1
        try:
            win32pipe.WaitNamedPipe(_PIPE_NAME, wait_ms)
            handle = win32file.CreateFile(
                _PIPE_NAME,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None)
            break
        except pywintypes.error as exc:
            # 2   = ERROR_FILE_NOT_FOUND  -> no pipe instance right now
            # 231 = ERROR_PIPE_BUSY       -> all instances busy
            # 121 = ERROR_SEM_TIMEOUT     -> WaitNamedPipe timed out (busy)
            if exc.winerror in (2, 121, 231):
                saw_any_failure = True
                if exc.winerror != 121:
                    saw_only_timeouts = False
                if time.time() < deadline:
                    # short sleep, but never past the deadline
                    nap = min(0.25, max(0.0, deadline - time.time()))
                    if nap > 0:
                        time.sleep(nap)
                    continue
                if saw_only_timeouts:
                    raise _PipeTimeout(
                        "pipe busy (WaitNamedPipe timed out)") from exc
                raise FileNotFoundError("pipe absent") from exc
            raise
    try:
        win32pipe.SetNamedPipeHandleState(
            handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        win32file.WriteFile(handle, json.dumps(payload).encode("utf-8"))
        _hr, data = win32file.ReadFile(handle, 65536)
        chunks = [data]
        while _hr == 234:
            # ERROR_MORE_DATA: the reply (a capture carries an inlined
            # base64 PNG) exceeds one read; keep pulling message chunks.
            _hr, data = win32file.ReadFile(handle, 65536)
            chunks.append(data)
        return json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
    finally:
        # finding 8 partial: guard the close and swallow a close error
        # so it cannot mask an in-flight exception.
        if handle is not None:
            try:
                handle.Close()
            except Exception:
                pass


def screen_secure_status() -> str:
    """Report whether the Secure Desktop Helper service answers.

    Returns the service version, the active console session and the
    session list when installed; a plain 'not installed' report with
    the install path when it is not; a neutral 'busy or starting' note
    when the pipe exists but never freed up in time.
    """
    try:
        resp = _call_pipe({"cmd": "status"}, timeout_ms=3000)
    except _PipeTimeout:
        return _BUSY_HINT + _service_folder()
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
    except _PipeTimeout:
        return _BUSY_HINT + _service_folder()
    except FileNotFoundError:
        return _INSTALL_HINT + _service_folder()
    except Exception as exc:
        return f"Secure Desktop Helper did not answer cleanly: {exc}"
    if not resp.get("ok"):
        return f"secure capture failed: {resp.get('error')}"
    png_ref = resp.get("png")
    if resp.get("png_base64"):
        # Hardened service (finding 10): the PNG is inlined as base64
        # and the service-side file is deleted. Materialise it locally
        # so callers still get a viewable path.
        import base64 as _b64
        import time as _time
        from pathlib import Path as _Path
        _dir = _Path.home() / "Desktop" / "screenshots"
        _dir.mkdir(parents=True, exist_ok=True)
        png_ref = str(_dir / ("secure-" + _time.strftime("%Y%m%d-%H%M%S") + ".png"))
        _Path(png_ref).write_bytes(_b64.b64decode(resp["png_base64"]))
    return (f"captured desktop '{resp.get('desktop')}' of session "
            f"{resp.get('session')} -> {png_ref} "
            f"({resp.get('width')}x{resp.get('height')}). "
            "View it with read_image.")

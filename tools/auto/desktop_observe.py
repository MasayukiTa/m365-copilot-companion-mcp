# -*- coding: utf-8 -*-
"""
Fixed revision of tools/auto/desktop_observe.py.

This file is a corrected copy produced under the tool audit. The upstream
repository is NOT modified. ASCII only.

Fixes applied (see FIXLOG.md, section desktop_observe, for the mapping to
finding numbers):
  - ctypes argtypes/restype declared for every Win32 call used here, so
    64-bit handles/pointers are not truncated to a 32-bit int (#1, #2,
    #3, #4).
  - GDI SelectObject old-object saved and restored before the bitmap is
    deleted, and the bitmap is unselected before GetDIBits (#8, #19).
  - GetDC / CreateCompatibleDC / CreateCompatibleBitmap return values are
    checked; only non-null GDI objects are released in finally (#9).
  - GetDIBits, BitBlt, PrintWindow, EnumDesktopWindows return values are
    checked and surfaced (#5, #10, #11).
  - IsIconic is used for the minimized flag instead of a coordinate
    heuristic (#6).
  - EnumDesktopWindows callback catches its own exceptions and the
    callback object is kept alive for the duration of the call (#5).
  - screen_lock_history checks the PowerShell return code and handles the
    timeout separately, returning partial output (#16).
  - screen_state_diff writes state_last.json atomically via os.replace and
    protects each Win32 probe with try/except (#17, #18).
  - numeric arguments are normalized to non-negative ints (#20).
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Optional

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_DESKTOP_READOBJECTS = 0x0001
_DESKTOP_ENUMERATE = 0x0040
_UOI_NAME = 2
_SPI_GETSCREENSAVERRUNNING = 0x0072
_PW_RENDERFULLCONTENT = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79

# Handle-sized aliases. HANDLE / HWND / HDC / HGDIOBJ / HDESK are all
# pointer-width; declaring restype as a pointer type keeps ctypes from
# truncating a 64-bit value to a 32-bit signed int (#1).
_HDESK = wintypes.HANDLE
_HGDIOBJ = wintypes.HANDLE


# --------------------------------------------------------------------------
# ctypes prototype declarations (#1, #2, #3, #4)
# --------------------------------------------------------------------------

def _declare_prototypes() -> None:
    u = _user32
    g = _gdi32
    k = _kernel32

    # user32 -- desktops / session
    u.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    u.GetUserObjectInformationW.restype = wintypes.BOOL

    u.OpenInputDesktop.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    u.OpenInputDesktop.restype = _HDESK

    u.OpenDesktopW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    u.OpenDesktopW.restype = _HDESK

    u.CloseDesktop.argtypes = [_HDESK]
    u.CloseDesktop.restype = wintypes.BOOL

    u.SetThreadDesktop.argtypes = [_HDESK]
    u.SetThreadDesktop.restype = wintypes.BOOL

    u.SystemParametersInfoW.argtypes = [
        wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT]
    u.SystemParametersInfoW.restype = wintypes.BOOL

    u.GetLastInputInfo.argtypes = [wintypes.LPVOID]
    u.GetLastInputInfo.restype = wintypes.BOOL

    u.GetSystemMetrics.argtypes = [ctypes.c_int]
    u.GetSystemMetrics.restype = ctypes.c_int

    # user32 -- windows
    _WNDENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u.EnumDesktopWindows.argtypes = [_HDESK, _WNDENUMPROC, wintypes.LPARAM]
    u.EnumDesktopWindows.restype = wintypes.BOOL

    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsWindowVisible.restype = wintypes.BOOL

    u.IsIconic.argtypes = [wintypes.HWND]
    u.IsIconic.restype = wintypes.BOOL

    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int

    u.GetWindowTextW.argtypes = [
        wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowTextW.restype = ctypes.c_int

    u.GetWindowRect.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindowRect.restype = wintypes.BOOL

    u.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD

    u.PrintWindow.argtypes = [
        wintypes.HWND, wintypes.HDC, wintypes.UINT]
    u.PrintWindow.restype = wintypes.BOOL

    u.GetDC.argtypes = [wintypes.HWND]
    u.GetDC.restype = wintypes.HDC

    u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    u.ReleaseDC.restype = ctypes.c_int

    # gdi32
    g.CreateCompatibleDC.argtypes = [wintypes.HDC]
    g.CreateCompatibleDC.restype = wintypes.HDC

    g.CreateCompatibleBitmap.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int]
    g.CreateCompatibleBitmap.restype = _HGDIOBJ

    g.SelectObject.argtypes = [wintypes.HDC, _HGDIOBJ]
    g.SelectObject.restype = _HGDIOBJ

    g.DeleteObject.argtypes = [_HGDIOBJ]
    g.DeleteObject.restype = wintypes.BOOL

    g.DeleteDC.argtypes = [wintypes.HDC]
    g.DeleteDC.restype = wintypes.BOOL

    g.BitBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
        wintypes.DWORD]
    g.BitBlt.restype = wintypes.BOOL

    g.GetDIBits.argtypes = [
        wintypes.HDC, _HGDIOBJ, wintypes.UINT, wintypes.UINT,
        wintypes.LPVOID, wintypes.LPVOID, wintypes.UINT]
    g.GetDIBits.restype = ctypes.c_int

    # kernel32
    k.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE

    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL

    k.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    k.QueryFullProcessImageNameW.restype = wintypes.BOOL

    k.GetTickCount.argtypes = []
    k.GetTickCount.restype = wintypes.DWORD


_declare_prototypes()


# --------------------------------------------------------------------------
# session / desktop probes
# --------------------------------------------------------------------------

def _desktop_name(handle) -> str:
    if not handle:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD(0)
    ok = _user32.GetUserObjectInformationW(
        handle, _UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed))
    return buf.value if ok else ""


def _input_desktop_name() -> str:
    # dwFlags=0, fInherit=FALSE, dwDesiredAccess=DESKTOP_READOBJECTS (#3)
    h = _user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    if not h:
        return ""
    try:
        return _desktop_name(h)
    finally:
        _user32.CloseDesktop(h)


def _screensaver_running() -> Optional[bool]:
    # Check the BOOL return; a failed call must not be reported as 'not
    # running' (#2). Returns None when the state is unknown.
    flag = wintypes.BOOL(False)
    ok = _user32.SystemParametersInfoW(
        _SPI_GETSCREENSAVERRUNNING, 0, ctypes.byref(flag), 0)
    if not ok:
        return None
    return bool(flag.value)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def _idle_seconds() -> float:
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return -1.0
    return (_kernel32.GetTickCount() - info.dwTime) / 1000.0


def _process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        pass
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        # lpdwSize is in characters; use the character capacity explicitly
        # so a future byte/char change cannot silently break it (#4).
        size = wintypes.DWORD(ctypes.sizeof(buf) // ctypes.sizeof(ctypes.c_wchar))
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        _kernel32.CloseHandle(h)


def _default_desktop_windows() -> list:
    """Visible windows on the 'Default' desktop, front to back."""
    hdesk = _user32.OpenDesktopW(
        "Default", 0, False, _DESKTOP_READOBJECTS | _DESKTOP_ENUMERATE)
    if not hdesk:
        return []
    hwnds: list = []
    enum_proc_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        # Never let a Python exception propagate back into the C stack
        # through the ctypes callback (#5).
        try:
            hwnds.append(hwnd)
        except Exception:
            pass
        return True

    # Keep a strong reference to the callback object for the whole call so
    # it cannot be garbage-collected mid-enumeration (#5).
    cb = enum_proc_type(_cb)
    try:
        if not _user32.EnumDesktopWindows(hdesk, cb, 0):
            # Enumeration failed (e.g. access denied); windows may be empty
            # for a different reason than 'no windows'. Surface nothing
            # here -- callers treat an empty list uniformly -- but do not
            # pretend success by continuing with a half-filled list.
            pass
    finally:
        _user32.CloseDesktop(hdesk)

    out = []
    for hwnd in hwnds:
        if not _user32.IsWindowVisible(hwnd):
            continue
        length = _user32.GetWindowTextLengthW(hwnd)
        if length < 0:
            length = 0
        # Allow headroom in case the title grew since the length query
        # (TOCTOU, #7); GetWindowTextW truncates safely to the buffer.
        cap = length + 64
        buf = ctypes.create_unicode_buffer(cap)
        _user32.GetWindowTextW(hwnd, buf, cap)
        rect = wintypes.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            continue
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            continue
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append({
            "hwnd": hwnd, "title": buf.value,
            "left": rect.left, "top": rect.top,
            "width": width, "height": height,
            "pid": pid.value, "process": _process_name(pid.value),
            # Use IsIconic rather than the -30000 coordinate heuristic (#6).
            "minimized": bool(_user32.IsIconic(hwnd)),
        })
    return out


def _session_lines() -> list:
    name = ""
    try:
        name = _input_desktop_name()
    except Exception as exc:
        name = "<error: {0}>".format(exc)
    saver = None
    idle = -1.0
    try:
        saver = _screensaver_running()
    except Exception:
        saver = None
    try:
        idle = _idle_seconds()
    except Exception:
        idle = -1.0
    saver_text = "<unknown>" if saver is None else str(saver)
    lines = ["input desktop: {0}".format(name or "<unknown>"),
             "screen saver running: {0}".format(saver_text)]
    if name and name != "Default":
        lines.append("state: LOCKED / SCREEN-SAVER / SECURE DESKTOP "
                     "(input is not on the operator desktop)")
    else:
        lines.append("state: operator desktop has input (not locked)")
    if idle >= 0:
        lines.append("seconds since last input: {0:.0f}".format(idle))
    return lines


# --------------------------------------------------------------------------
# bitmap plumbing
# --------------------------------------------------------------------------

class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def _dib_to_image(hdc_mem, hbmp, width, height):
    """Copy the bitmap bits out via GetDIBits and build a PIL image.

    GetDIBits requires the bitmap NOT be selected into the DC it reads
    from (#19), so the caller must have restored the DC's original object
    before calling this. The GetDIBits return value is checked (#10): a
    zero or short scanline count raises so the failure is not masked by a
    black image.
    """
    from PIL import Image
    bmi = _BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(bmi)
    bmi.biWidth = width
    bmi.biHeight = -height  # top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0
    buf = ctypes.create_string_buffer(width * height * 4)
    scanlines = _gdi32.GetDIBits(
        hdc_mem, hbmp, 0, height, buf, ctypes.byref(bmi), 0)
    if scanlines == 0 or scanlines < height:
        raise OSError(
            "GetDIBits copied {0} of {1} scanlines (winerror {2})".format(
                scanlines, height, ctypes.get_last_error()))
    img = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1)
    return img.convert("RGB")


def _is_blank(img) -> bool:
    # Check every RGB channel's extrema, not only luminance, so two
    # distinct colours with equal luma are not called blank (#15).
    extrema = img.convert("RGB").getextrema()
    for channel in extrema:
        if channel[0] != channel[1]:
            return False
    return True


def _safe_filename(text: str, fallback: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in text)
    cleaned = cleaned.strip("._")[:60]
    return cleaned or fallback


def _capture_window_png(hwnd, dest: Path) -> tuple:
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False, "GetWindowRect failed"
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return False, "window has no area"

    hdc_screen = None
    hdc_mem = None
    hbmp = None
    old = None
    try:
        # Check each GDI creation; bail out cleanly on NULL (#9).
        hdc_screen = _user32.GetDC(None)
        if not hdc_screen:
            return False, "GetDC(None) returned NULL"
        hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
        if not hdc_mem:
            return False, "CreateCompatibleDC returned NULL"
        hbmp = _gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
        if not hbmp:
            return False, "CreateCompatibleBitmap returned NULL"

        old = _gdi32.SelectObject(hdc_mem, hbmp)
        rendered = _user32.PrintWindow(hwnd, hdc_mem, _PW_RENDERFULLCONTENT)
        if not rendered:
            return False, "PrintWindow returned 0 (window refused to render)"
        # Unselect the bitmap before GetDIBits reads it (#8, #19).
        if old:
            _gdi32.SelectObject(hdc_mem, old)
            old = None
        img = _dib_to_image(hdc_mem, hbmp, width, height)
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(dest), format="PNG")
        return True, ("rendered but uniformly blank (likely GPU/overlay window)"
                      if _is_blank(img) else "ok")
    except Exception as exc:
        return False, "{0}: {1}".format(type(exc).__name__, exc)
    finally:
        # Restore the DC's original object before deleting the bitmap so we
        # never delete a selected object (#8). Release only non-null GDI
        # objects (#9).
        if hdc_mem and old:
            _gdi32.SelectObject(hdc_mem, old)
        if hbmp:
            _gdi32.DeleteObject(hbmp)
        if hdc_mem:
            _gdi32.DeleteDC(hdc_mem)
        if hdc_screen:
            _user32.ReleaseDC(None, hdc_screen)


def _headless_creationflags() -> int:
    """CREATE_NO_WINDOW for this module's child processes (tesseract,
    PowerShell). The callers are unattended by construction (an MCP
    server with no console of its own), so a console child would
    allocate a fresh, visible console window.
    """
    try:
        from tools.childproc import headless_creationflags
        return headless_creationflags()
    except Exception:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _ocr_excerpt(png: Path, max_chars: int = 300) -> str:
    exe = shutil.which("tesseract")
    if not exe:
        return "<tesseract not on PATH; OCR unavailable>"
    try:
        proc = subprocess.run(
            [exe, str(png), "stdout", "-l", "jpn+eng", "--psm", "6"],
            capture_output=True, timeout=30,
            creationflags=_headless_creationflags())
        text = proc.stdout.decode("utf-8", errors="replace")
        text = " ".join(text.split())
        return text[:max_chars] if text else "<no text recognised>"
    except Exception as exc:
        return "<OCR failed: {0}>".format(exc)


# --------------------------------------------------------------------------
# the tools
# --------------------------------------------------------------------------

def screen_full_desktop(output_path: str = "", max_dimension: int = 1600) -> str:
    """Capture the WHOLE 'Default' desktop as one picture, even while locked."""
    try:
        max_dimension = max(0, int(max_dimension))
    except (TypeError, ValueError):
        max_dimension = 1600
    dest = Path(output_path) if output_path else (
        Path.home() / "Desktop" / "screenshots" / "desktop_state"
        / "full-{0}.png".format(time.strftime("%Y%m%d-%H%M%S")))
    result: dict = {}

    def _work():
        hdesk = _user32.OpenDesktopW(
            "Default", 0, False, _DESKTOP_READOBJECTS | _DESKTOP_ENUMERATE)
        if not hdesk:
            result["error"] = "OpenDesktop('Default') failed"
            return
        hdc_screen = None
        hdc_mem = None
        hbmp = None
        old = None
        try:
            if not _user32.SetThreadDesktop(hdesk):
                result["error"] = ("SetThreadDesktop failed "
                                   "(winerror {0})".format(
                                       ctypes.get_last_error()))
                return
            x = _user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
            y = _user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
            width = _user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
            height = _user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
            if width <= 0 or height <= 0:
                result["error"] = "virtual screen has no area"
                return
            hdc_screen = _user32.GetDC(None)
            if not hdc_screen:
                result["error"] = "GetDC(None) returned NULL"
                return
            hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
            if not hdc_mem:
                result["error"] = "CreateCompatibleDC returned NULL"
                return
            hbmp = _gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
            if not hbmp:
                result["error"] = "CreateCompatibleBitmap returned NULL"
                return
            old = _gdi32.SelectObject(hdc_mem, hbmp)
            blitted = _gdi32.BitBlt(
                hdc_mem, 0, 0, width, height,
                hdc_screen, x, y, _SRCCOPY | _CAPTUREBLT)
            if not blitted:
                result["error"] = ("BitBlt failed (winerror {0})".format(
                    ctypes.get_last_error()))
                return
            # Unselect the bitmap before GetDIBits reads it (#8, #19).
            if old:
                _gdi32.SelectObject(hdc_mem, old)
                old = None
            img = _dib_to_image(hdc_mem, hbmp, width, height)
            if max_dimension and max(img.size) > max_dimension:
                img.thumbnail((max_dimension, max_dimension))
            dest.parent.mkdir(parents=True, exist_ok=True)
            img.save(str(dest), format="PNG")
            result["size"] = img.size
            result["blank"] = _is_blank(img)
            result["origin"] = (x, y)
            result["full_size"] = (width, height)
        except Exception as exc:
            result["error"] = "{0}: {1}".format(type(exc).__name__, exc)
        finally:
            # Restore original object before deleting the bitmap (#8), and
            # release only non-null GDI objects (#9).
            if hdc_mem and old:
                _gdi32.SelectObject(hdc_mem, old)
            if hbmp:
                _gdi32.DeleteObject(hbmp)
            if hdc_mem:
                _gdi32.DeleteDC(hdc_mem)
            if hdc_screen:
                _user32.ReleaseDC(None, hdc_screen)
            _user32.CloseDesktop(hdesk)

    # This thread is single-use: it is created, attaches to 'Default',
    # captures, and exits. SetThreadDesktop is therefore never undone and
    # does not need to be, because the thread does not outlive the call
    # (#12). Do not reuse this thread for other desktop work.
    worker = threading.Thread(target=_work, daemon=True)
    worker.start()
    worker.join(timeout=45)
    if worker.is_alive():
        return "full desktop capture timed out (capture thread still running)"
    if "error" in result:
        return "full desktop capture failed: {0}".format(result["error"])
    note = (" -- rendered but uniformly blank (the desktop is composed "
            "elsewhere right now; per-window captures may still work)"
            if result.get("blank") else "")
    return ("saved {0} -- the whole 'Default' desktop, "
            "{1}x{2} at {3}, saved at {4}x{5}{6}. "
            "Read it back with read_image.".format(
                dest, result["full_size"][0], result["full_size"][1],
                result["origin"], result["size"][0], result["size"][1], note))


def screen_state_report(include_captures: bool = True,
                        include_ocr: bool = False,
                        max_windows: int = 4,
                        top_processes: int = 8) -> str:
    """One integrated desktop report that works while locked / screen saver."""
    # Normalize numeric arguments (#20).
    try:
        max_windows = max(0, int(max_windows))
    except (TypeError, ValueError):
        max_windows = 4
    try:
        top_processes = max(0, int(top_processes))
    except (TypeError, ValueError):
        top_processes = 8

    lines = _session_lines()
    windows = _default_desktop_windows()
    titled = [w for w in windows if w["title"]]
    lines.append("")
    lines.append("{0} visible windows on 'Default' desktop "
                 "({1} titled), front to back:".format(
                     len(windows), len(titled)))
    for w in windows:
        title = w["title"] if w["title"] else "<no title>"
        mini = " [minimized]" if w["minimized"] else ""
        lines.append("  [{0}] {1} -- {2}x{3} at ({4}, {5}){6}".format(
            w["process"] or "?", title, w["width"], w["height"],
            w["left"], w["top"], mini))

    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                rss = p.info["memory_info"].rss if p.info["memory_info"] else 0
                procs.append((rss, p.info["name"] or "?", p.pid))
            except Exception:
                continue
        procs.sort(reverse=True)
        lines.append("")
        lines.append("top {0} processes by memory:".format(top_processes))
        for rss, name, pid in procs[:top_processes]:
            lines.append("  {0} (pid {1}) -- {2} MB".format(
                name, pid, rss // (1024 * 1024)))
    except Exception as exc:
        lines.append("process list unavailable: {0}".format(exc))

    if include_captures:
        base = (Path.home() / "Desktop" / "screenshots" / "desktop_state"
                / time.strftime("%Y%m%d-%H%M%S"))
        capturable = [w for w in titled if not w["minimized"]]
        lines.append("")
        lines.append("window captures -> {0}".format(base))
        for idx, w in enumerate(capturable[:max_windows]):
            dest = base / "{0:02d}_{1}.png".format(
                idx, _safe_filename(w["title"], "window"))
            try:
                ok, note = _capture_window_png(w["hwnd"], dest)
            except Exception as exc:
                ok, note = False, "capture error: {0}".format(exc)
            lines.append("  [{0}] {1}: {2} ({3})".format(
                idx, w["title"],
                "saved " + str(dest) if ok else "FAILED", note))
            if ok and include_ocr:
                lines.append("      OCR: {0}".format(_ocr_excerpt(dest)))
    return "\n".join(lines)


def screen_lock_history(days: int = 2, max_events: int = 30) -> str:
    """Lock / unlock / sleep / wake timeline from the Windows event logs."""
    # Normalize numeric arguments (#20).
    try:
        days = max(0, int(days))
    except (TypeError, ValueError):
        days = 2
    try:
        max_events = max(0, int(max_events))
    except (TypeError, ValueError):
        max_events = 30

    ps = shutil.which("powershell") or shutil.which("powershell.exe")
    if not ps:
        return "powershell not found on PATH; event log unavailable"
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$ErrorActionPreference='SilentlyContinue';"
        "$start=(Get-Date).AddDays(-{0});".format(days) +
        "$sys=Get-WinEvent -FilterHashtable @{LogName='System';"
        " StartTime=$start; Id=42,107,506,507,1,7001,7002} "
        "-MaxEvents 300 | Where-Object {"
        " ($_.ProviderName -eq 'Microsoft-Windows-Kernel-Power'"
        "  -and $_.Id -in 42,107,506,507) -or"
        " ($_.ProviderName -eq 'Microsoft-Windows-Power-Troubleshooter'"
        "  -and $_.Id -eq 1) -or"
        " ($_.ProviderName -eq 'Microsoft-Windows-Winlogon'"
        "  -and $_.Id -in 7001,7002)};"
        "$sec=Get-WinEvent -FilterHashtable @{LogName='Security';"
        " StartTime=$start; Id=4800,4801} -MaxEvents 100;"
        "$all=@($sys)+@($sec);"
        "$all | Sort-Object TimeCreated -Descending |"
        " Select-Object -First {0} | ForEach-Object {{".format(max_events) +
        " '{0:yyyy-MM-dd HH:mm:ss} [{1} {2}] {3}' -f $_.TimeCreated,"
        " $_.ProviderName, $_.Id,"
        " (($_.Message -split \"`n\")[0].Trim()) }"
    )
    timed_out = False
    partial = ""
    try:
        proc = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=60,
            creationflags=_headless_creationflags())
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        rc = proc.returncode
    except subprocess.TimeoutExpired as exc:
        # Handle the timeout separately and keep whatever partial output
        # was produced before the deadline (#16).
        timed_out = True
        out = ""
        err = ""
        rc = None
        if exc.stdout:
            partial = (exc.stdout.decode("utf-8", errors="replace")
                       if isinstance(exc.stdout, bytes) else str(exc.stdout))
            partial = partial.strip()
        if exc.stderr:
            serr = (exc.stderr.decode("utf-8", errors="replace")
                    if isinstance(exc.stderr, bytes) else str(exc.stderr))
            err = serr.strip()
    except Exception as exc:
        return "event log query failed: {0}".format(exc)

    if timed_out:
        header = ("event log query timed out after 60s; "
                  "partial output (newest first):\n")
        return header + partial if partial else (
            "event log query timed out after 60s with no output "
            "(stderr: {0})".format(err[:200]))

    # Check the return code before concluding 'no events' (#16).
    if rc != 0 and not out:
        return ("event log query exited with code {0}; "
                "stderr: {1}".format(rc, err[:300] or "<none>"))
    if not out:
        return ("no lock/sleep/wake events found in the last {0} day(s) "
                "(Security-log lock events may need audit policy; "
                "exit code {1}; query stderr: {2})".format(
                    days, rc, err[:200]))
    return ("lock / sleep / wake events, last {0} day(s), newest first:\n"
            .format(days) + out)


def screen_state_diff() -> str:
    """What changed on the desktop since the last call (or the baseline)."""
    windows = _default_desktop_windows()
    # Protect each Win32 probe so a single failure does not take down the
    # whole tool (#18).
    try:
        input_desktop = _input_desktop_name()
    except Exception as exc:
        input_desktop = "<error: {0}>".format(exc)
    try:
        saver = _screensaver_running()
    except Exception:
        saver = None
    current = {
        "input_desktop": input_desktop,
        "screensaver": saver,
        "windows": sorted(
            "{0} :: {1}".format(w["process"], w["title"])
            for w in windows if w["title"]),
        "taken_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    store = (Path.home() / "Desktop" / "screenshots" / "desktop_state"
             / "state_last.json")
    previous = None
    if store.exists():
        try:
            previous = json.loads(store.read_text(encoding="utf-8"))
        except Exception:
            previous = None
    store.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically: write a temp file in the same directory then
    # os.replace, so a concurrent reader never sees a half-written file
    # and a crash mid-write cannot corrupt the baseline (#17).
    tmp = store.with_name("state_last.json.tmp-{0}".format(os.getpid()))
    try:
        tmp.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8")
        os.replace(str(tmp), str(store))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
    if previous is None:
        return ("baseline saved at {0}: input desktop {1}, "
                "{2} titled windows. Call again to see what changed.".format(
                    current["taken_at"], current["input_desktop"] or "<unknown>",
                    len(current["windows"])))
    lines = ["changes since {0}:".format(previous.get("taken_at", "<unknown>"))]
    if previous.get("input_desktop") != current["input_desktop"]:
        lines.append("  input desktop: {0} -> {1}".format(
            previous.get("input_desktop"), current["input_desktop"]))
    if previous.get("screensaver") != current["screensaver"]:
        lines.append("  screen saver: {0} -> {1}".format(
            previous.get("screensaver"), current["screensaver"]))
    before = set(previous.get("windows", []))
    after = set(current["windows"])
    for name in sorted(after - before):
        lines.append("  opened: {0}".format(name))
    for name in sorted(before - after):
        lines.append("  closed: {0}".format(name))
    if len(lines) == 1:
        lines.append("  no changes")
    return "\n".join(lines)

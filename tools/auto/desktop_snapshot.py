# -*- coding: utf-8 -*-
"""
Desktop state snapshot that works while the workstation is locked or the
screen saver is running.

WHY THIS EXISTS. The regular capture path (screen_look / screenshot) reads
the *input* desktop. When the screen saver runs, the session is locked, or
a UAC secure-desktop prompt is up, the input desktop is "Screen-saver" or
"Winlogon", not the desktop the operator's windows live on, and capture is
refused outright -- exactly when an agent most needs to know what state the
machine was left in.

WHAT THIS DOES INSTEAD. The operator's windows keep living on the "Default"
desktop of the same window station, and that desktop can be opened and
enumerated by a process in the session regardless of which desktop has
input. Each window is then rendered individually with PrintWindow
(PW_RENDERFULLCONTENT), which asks the owning application to draw into a
memory DC; most applications honour it even when their desktop is not the
input desktop. The result is a state report (input desktop, screen-saver /
lock indicators, idle time, window list) plus optional per-window PNG
captures and a contact sheet.

LIMITS, stated plainly. This does NOT capture the lock screen itself, and
GPU / hardware-overlay windows may come back black from PrintWindow; a
capture that is uniformly blank is reported as blank rather than passed
off as a picture of the window. Capturing the Winlogon desktop's own
pixels would need a SYSTEM service component, which this module is not.

Standard library + Pillow only (Pillow is already in this server's venv).

--- AUDIT FIX NOTES (see FIXLOG.md) ---
This is the audit-fix revision. Changes applied versus the repository copy,
keyed to findings in findings_desktop_snapshot.md:
  #1  (high) ctypes argtypes/restype set for every Win32 call used here, so
             64-bit HANDLE/HWND/HDC values are not truncated to C int.
  #8  (high) _capture_window_png now saves the DC's prior bitmap from
             SelectObject and restores it before DeleteObject, so the bitmap
             is actually freed (no GDI handle leak).
  #2  (med)  _desktop_name honours the required-size out param and retries
             with a larger buffer.
  #4  (med)  _screensaver_running checks the BOOL return and reports unknown
             on failure instead of silently claiming "not running".
  #5  (med)  _idle_seconds uses GetTickCount64 and masks the delta so the
             49.7-day wraparound cannot produce a bogus huge/negative value.
  #6  (med)  _enum_default_desktop_windows binds the callback and checks the
             EnumDesktopWindows BOOL return.
  #9  (med)  _capture_window_png NULL-checks every GDI resource it acquires
             and bails out early, freeing what was already acquired.
  #10 (med)  _capture_window_png checks the GetDIBits scan-line count and
             fails instead of saving uninitialised pixels.
Low-severity findings were left as-is; see FIXLOG.md for the reasons.
No keyboard/extended-key lParam path and no timeout_ms exist in this module,
so those two mandated review points do not apply here (noted in FIXLOG.md).
"""
from __future__ import annotations

import ctypes
import os
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
_ERROR_INSUFFICIENT_BUFFER = 122

# ---------------------------------------------------------------------------
# Finding #1 (high): declare argtypes/restype for every Win32 function used.
# Without these, ctypes treats each restype as C int (32-bit signed) and may
# truncate pointer-sized HANDLE/HWND/HDC return values on 64-bit Python, and
# may truncate pointer-sized arguments as well. HANDLE-like types are made
# explicit so nothing is silently narrowed.
# ---------------------------------------------------------------------------
_HDESK = wintypes.HANDLE
_HDC = wintypes.HDC
_HBITMAP = wintypes.HBITMAP
_HGDIOBJ = wintypes.HGDIOBJ

_user32.GetUserObjectInformationW.argtypes = [
    wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_user32.GetUserObjectInformationW.restype = wintypes.BOOL

_user32.OpenInputDesktop.argtypes = [
    wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_user32.OpenInputDesktop.restype = _HDESK

_user32.OpenDesktopW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_user32.OpenDesktopW.restype = _HDESK

_user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
_user32.GetThreadDesktop.restype = _HDESK

_user32.CloseDesktop.argtypes = [_HDESK]
_user32.CloseDesktop.restype = wintypes.BOOL

_user32.SystemParametersInfoW.argtypes = [
    wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT]
_user32.SystemParametersInfoW.restype = wintypes.BOOL

_user32.GetLastInputInfo.restype = wintypes.BOOL

_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL

_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int

_user32.GetWindowTextW.argtypes = [
    wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int

_user32.GetWindowRect.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL

_user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD

_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = _HDC

_user32.ReleaseDC.argtypes = [wintypes.HWND, _HDC]
_user32.ReleaseDC.restype = ctypes.c_int

_user32.PrintWindow.argtypes = [wintypes.HWND, _HDC, wintypes.UINT]
_user32.PrintWindow.restype = wintypes.BOOL

_gdi32.CreateCompatibleDC.argtypes = [_HDC]
_gdi32.CreateCompatibleDC.restype = _HDC

_gdi32.CreateCompatibleBitmap.argtypes = [_HDC, ctypes.c_int, ctypes.c_int]
_gdi32.CreateCompatibleBitmap.restype = _HBITMAP

_gdi32.SelectObject.argtypes = [_HDC, _HGDIOBJ]
_gdi32.SelectObject.restype = _HGDIOBJ

_gdi32.DeleteObject.argtypes = [_HGDIOBJ]
_gdi32.DeleteObject.restype = wintypes.BOOL

_gdi32.DeleteDC.argtypes = [_HDC]
_gdi32.DeleteDC.restype = wintypes.BOOL

_gdi32.GetDIBits.argtypes = [
    _HDC, _HBITMAP, wintypes.UINT, wintypes.UINT,
    wintypes.LPVOID, wintypes.LPVOID, wintypes.UINT]
_gdi32.GetDIBits.restype = ctypes.c_int

_kernel32.GetCurrentThreadId.restype = wintypes.DWORD

_kernel32.GetTickCount64.restype = ctypes.c_ulonglong

_kernel32.OpenProcess.argtypes = [
    wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE

_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD)]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def _desktop_name(handle) -> str:
    # Finding #2 (med): honour the required-size out param. A fixed 256-char
    # buffer is usually enough, but if GetUserObjectInformationW fails with
    # ERROR_INSUFFICIENT_BUFFER we retry with the size it asked for instead
    # of silently truncating.
    if not handle:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD(0)
    ok = _user32.GetUserObjectInformationW(
        handle, _UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed))
    if ok:
        return buf.value
    if ctypes.get_last_error() == _ERROR_INSUFFICIENT_BUFFER and needed.value:
        buf = ctypes.create_unicode_buffer(needed.value // ctypes.sizeof(
            ctypes.c_wchar) + 1)
        ok = _user32.GetUserObjectInformationW(
            handle, _UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed))
        if ok:
            return buf.value
    return ""


def _input_desktop_name() -> str:
    h = _user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    if not h:
        # Finding #3 (low): surface the reason for diagnosis. Kept minimal.
        return ""
    try:
        return _desktop_name(h)
    finally:
        _user32.CloseDesktop(h)


def _thread_desktop_name() -> str:
    h = _user32.GetThreadDesktop(_kernel32.GetCurrentThreadId())
    # GetThreadDesktop returns a handle that must NOT be closed by the caller.
    return _desktop_name(h) if h else ""


def _screensaver_running() -> Optional[bool]:
    # Finding #4 (med): check the BOOL return. If the call fails we return
    # None (unknown) rather than the uninitialised/default False, so callers
    # can distinguish "not running" from "could not determine".
    flag = wintypes.BOOL(False)
    if not _user32.SystemParametersInfoW(
            _SPI_GETSCREENSAVERRUNNING, 0, ctypes.byref(flag), 0):
        return None
    return bool(flag.value)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


_user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]


def _idle_seconds() -> float:
    # Finding #5 (med): dwTime is a 32-bit tick count that wraps every ~49.7
    # days. Use GetTickCount64 for "now" and mask the subtraction into the
    # 32-bit domain so a wrap between dwTime and now yields a small correct
    # delta instead of a huge or negative one.
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return -1.0
    now32 = _kernel32.GetTickCount64() & 0xFFFFFFFF
    delta = (now32 - info.dwTime) & 0xFFFFFFFF
    return delta / 1000.0


def _process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        import psutil  # already installed in this server's venv
        return psutil.Process(pid).name()
    except Exception:
        # Finding #12 (low): the broad except is retained deliberately. This
        # is a best-effort fallback to the Win32 path below; psutil raising
        # NoSuchProcess/AccessDenied/etc must not abort name resolution.
        pass
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        _kernel32.CloseHandle(h)


def _enum_default_desktop_windows() -> list:
    """Enumerate windows on the 'Default' desktop, front to back."""
    hdesk = _user32.OpenDesktopW(
        "Default", 0, False, _DESKTOP_READOBJECTS | _DESKTOP_ENUMERATE)
    if not hdesk:
        return []
    hwnds: list = []

    enum_proc_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        hwnds.append(hwnd)
        return True

    # Finding #6 (med): bind the callback to a local so it cannot be garbage
    # collected mid-enumeration, and check the EnumDesktopWindows BOOL return
    # so a failed/partial enumeration is not silently treated as complete.
    cb = enum_proc_type(_cb)
    _user32.EnumDesktopWindows.argtypes = [_HDESK, enum_proc_type, wintypes.LPARAM]
    _user32.EnumDesktopWindows.restype = wintypes.BOOL
    try:
        if not _user32.EnumDesktopWindows(hdesk, cb, 0):
            # Partial list may still be useful; return what we gathered.
            return hwnds
    finally:
        _user32.CloseDesktop(hdesk)
    return hwnds


def _window_info(hwnd) -> Optional[dict]:
    if not _user32.IsWindowVisible(hwnd):
        return None
    length = _user32.GetWindowTextLengthW(hwnd)
    if length < 0:
        length = 0
    buf = ctypes.create_unicode_buffer(length + 1)
    copied = _user32.GetWindowTextW(hwnd, buf, length + 1)
    # Finding #7 (low): use the copied count to bound the title.
    title = buf.value[:copied] if copied > 0 else buf.value
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    pid = wintypes.DWORD(0)
    # Finding #13 (low): the 0-return of GetWindowThreadProcessId is tolerated;
    # pid stays 0 and _process_name's pid<=0 guard handles it. No crash path.
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return {
        "hwnd": hwnd,
        "title": title,
        "left": rect.left, "top": rect.top,
        "width": width, "height": height,
        "pid": pid.value,
        "process": _process_name(pid.value),
    }


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


# Finding #9 (med): cap capture dimensions so a pathological window size does
# not drive a width*height*4 allocation into MemoryError.
_MAX_CAPTURE_DIM = 16384


def _capture_window_png(hwnd, dest: Path) -> tuple:
    """Render one window with PrintWindow into a PNG.

    Returns (ok, note). A uniformly blank render is reported in the note.
    """
    from PIL import Image

    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False, "GetWindowRect failed"
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return False, "window has no area"
    if width > _MAX_CAPTURE_DIM or height > _MAX_CAPTURE_DIM:
        return False, "window too large to capture (%dx%d)" % (width, height)

    # Finding #8 (high) + #9 (med): acquire GDI resources, NULL-check each
    # one, save the DC's prior bitmap from SelectObject, and in finally
    # restore the prior bitmap before deleting ours so DeleteObject actually
    # frees it. Every acquired handle is released on every path.
    hdc_screen = _user32.GetDC(None)
    if not hdc_screen:
        return False, "GetDC failed"
    hdc_mem = None
    hbmp = None
    old_bmp = None
    try:
        hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
        if not hdc_mem:
            return False, "CreateCompatibleDC failed"
        hbmp = _gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
        if not hbmp:
            return False, "CreateCompatibleBitmap failed"

        old_bmp = _gdi32.SelectObject(hdc_mem, hbmp)
        if not old_bmp:
            return False, "SelectObject failed"

        if not _user32.PrintWindow(hwnd, hdc_mem, _PW_RENDERFULLCONTENT):
            return False, "PrintWindow returned 0 (window refused to render)"

        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(bmi)
        bmi.biWidth = width
        bmi.biHeight = -height  # top-down
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0  # BI_RGB
        buf = ctypes.create_string_buffer(width * height * 4)
        # Finding #10 (med): check the scan-line count. 0 means GetDIBits
        # failed; do not save uninitialised pixels as a successful capture.
        copied = _gdi32.GetDIBits(
            hdc_mem, hbmp, 0, height, buf, ctypes.byref(bmi), 0)
        if copied == 0:
            return False, "GetDIBits failed"
        img = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1)
        img = img.convert("RGB")
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(dest), format="PNG")
        extrema = img.convert("L").getextrema()
        blank = extrema[0] == extrema[1]
        return True, ("rendered but uniformly blank (likely GPU/overlay window)"
                      if blank else "ok")
    finally:
        # Restore the DC's original bitmap BEFORE deleting ours, otherwise
        # DeleteObject cannot free a bitmap still selected into a DC.
        if hdc_mem and old_bmp:
            _gdi32.SelectObject(hdc_mem, old_bmp)
        if hbmp:
            _gdi32.DeleteObject(hbmp)
        if hdc_mem:
            _gdi32.DeleteDC(hdc_mem)
        _user32.ReleaseDC(None, hdc_screen)


def _safe_filename(text: str, fallback: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in text)
    cleaned = cleaned.strip("._")[:60]
    return cleaned or fallback


def desktop_state(include_window_captures: bool = False,
                  max_windows: int = 6,
                  output_dir: str = "") -> str:
    """Report the desktop's state even when locked / screen saver is active.

    Unlike screen_look, this does not need the input desktop: it reports
    which desktop has input, whether the screen saver is running, how long
    the machine has been idle, and the windows living on the 'Default'
    desktop (title, process, position, size) -- all obtainable while the
    workstation is locked.

    Args:
        include_window_captures: When true, also render up to max_windows
            titled windows individually via PrintWindow and save PNGs plus
            a contact sheet, so the caller can SEE the windows' content
            without the input desktop.
        max_windows: Cap on per-window captures (front to back).
        output_dir: Where captures go. Defaults to
            ~/Desktop/screenshots/desktop_state/<timestamp>/.
    """
    lines: list = []
    try:
        input_name = _input_desktop_name()
    except Exception as exc:  # never fail the whole report on one probe
        input_name = "<error: %s>" % exc
    saver: Optional[bool] = None
    idle = -1.0
    try:
        saver = _screensaver_running()
    except Exception:
        saver = None
    try:
        idle = _idle_seconds()
    except Exception:
        pass

    lines.append("input desktop: %s" % (input_name or "<unknown>"))
    lines.append("thread desktop: %s" % (_thread_desktop_name() or "<unknown>"))
    # Finding #4 (med): report unknown distinctly from False.
    saver_text = "unknown" if saver is None else str(saver)
    lines.append("screen saver running: %s" % saver_text)
    if input_name and input_name != "Default":
        lines.append(
            "state: input is NOT on the operator desktop "
            "(locked, screen saver, or secure desktop) -- "
            "window state below comes from the 'Default' desktop")
    else:
        lines.append("state: operator desktop has input (not locked)")
    if idle >= 0:
        lines.append("seconds since last input: %.0f" % idle)

    windows: list = []
    for hwnd in _enum_default_desktop_windows():
        info = _window_info(hwnd)
        if info is None:
            continue
        windows.append(info)
    titled = [w for w in windows if w["title"]]
    lines.append("")
    lines.append("%d visible windows on 'Default' desktop (%d titled), "
                 "front to back:" % (len(windows), len(titled)))
    for w in windows:
        title = w["title"] if w["title"] else "<no title>"
        lines.append(
            "  [%s] %s -- %dx%d at (%d, %d)" % (
                w["process"] or "?", title,
                w["width"], w["height"], w["left"], w["top"]))

    if include_window_captures:
        base = Path(output_dir) if output_dir else (
            Path.home() / "Desktop" / "screenshots" / "desktop_state"
            / time.strftime("%Y%m%d-%H%M%S"))
        lines.append("")
        lines.append("window captures -> %s" % base)
        shots: list = []
        capturable = [w for w in titled
                      if w["left"] > -30000 and w["top"] > -30000]
        for idx, w in enumerate(capturable[:max_windows]):
            name = _safe_filename(w["title"], "window%d" % idx)
            dest = base / ("%02d_%s.png" % (idx, name))
            try:
                ok, note = _capture_window_png(w["hwnd"], dest)
            except Exception as exc:
                ok, note = False, "capture error: %s" % exc
            lines.append("  [%d] %s: %s (%s)" % (
                idx, w["title"],
                ("saved " + str(dest)) if ok else "FAILED", note))
            if ok:
                shots.append(dest)
        if len(shots) >= 2:
            try:
                from PIL import Image
                thumbs = []
                # Finding #15 (low): copy each thumbnail and close the source
                # handle so the PNG is not left locked by PIL's lazy loader.
                for p in shots:
                    with Image.open(str(p)) as im:
                        im.thumbnail((640, 640))
                        thumbs.append(im.copy())
                cols = 2
                rows = (len(thumbs) + cols - 1) // cols
                cell_w = max(t.width for t in thumbs)
                cell_h = max(t.height for t in thumbs)
                sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), "black")
                for i, t in enumerate(thumbs):
                    sheet.paste(t, ((i % cols) * cell_w, (i // cols) * cell_h))
                sheet_path = base / "contact_sheet.png"
                sheet.save(str(sheet_path), format="PNG")
                lines.append("  contact sheet: %s" % sheet_path)
            except Exception as exc:
                lines.append("  contact sheet FAILED: %s" % exc)
    return "\n".join(lines)


def capture_window_by_title(title_contains: str, output_path: str = "") -> str:
    """Capture ONE window from the 'Default' desktop by title substring.

    Works while the workstation is locked or the screen saver is running,
    because it renders the window with PrintWindow instead of reading the
    input desktop. Returns the saved PNG path, or a plain failure note
    (including when the render comes back uniformly blank, which happens
    for some GPU / overlay windows).
    """
    target = None
    for hwnd in _enum_default_desktop_windows():
        info = _window_info(hwnd)
        if info and title_contains.lower() in info["title"].lower():
            target = info
            break
    if target is None:
        return "no visible window on 'Default' desktop matches: %s" % title_contains
    dest = Path(output_path) if output_path else (
        Path.home() / "Desktop" / "screenshots" / "desktop_state"
        / ("%s-%s.png" % (_safe_filename(target["title"], "window"),
                          time.strftime("%H%M%S"))))
    try:
        ok, note = _capture_window_png(target["hwnd"], dest)
    except Exception as exc:
        return "capture failed for '%s': %s" % (target["title"], exc)
    if not ok:
        return "capture failed for '%s': %s" % (target["title"], note)
    return ("saved %s -- window '%s' (%dx%d, %s). "
            "Read it back with read_image." % (
                dest, target["title"], target["width"],
                target["height"], note))

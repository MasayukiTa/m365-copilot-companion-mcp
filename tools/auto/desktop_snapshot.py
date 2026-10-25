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


def _desktop_name(handle) -> str:
    if not handle:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD(0)
    ok = _user32.GetUserObjectInformationW(
        handle, _UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed))
    return buf.value if ok else ""


def _input_desktop_name() -> str:
    h = _user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    if not h:
        return ""
    try:
        return _desktop_name(h)
    finally:
        _user32.CloseDesktop(h)


def _thread_desktop_name() -> str:
    h = _user32.GetThreadDesktop(_kernel32.GetCurrentThreadId())
    return _desktop_name(h) if h else ""


def _screensaver_running() -> bool:
    flag = wintypes.BOOL(False)
    _user32.SystemParametersInfoW(
        _SPI_GETSCREENSAVERRUNNING, 0, ctypes.byref(flag), 0)
    return bool(flag.value)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def _idle_seconds() -> float:
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return -1.0
    tick = _kernel32.GetTickCount()
    return (tick - info.dwTime) / 1000.0


def _process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        import psutil  # already installed in this server's venv
        return psutil.Process(pid).name()
    except Exception:
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

    try:
        _user32.EnumDesktopWindows(hdesk, enum_proc_type(_cb), 0)
    finally:
        _user32.CloseDesktop(hdesk)
    return hwnds


def _window_info(hwnd) -> Optional[dict]:
    if not _user32.IsWindowVisible(hwnd):
        return None
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    pid = wintypes.DWORD(0)
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

    hdc_screen = _user32.GetDC(None)
    hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = _gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
    try:
        _gdi32.SelectObject(hdc_mem, hbmp)
        ok = _user32.PrintWindow(hwnd, hdc_mem, _PW_RENDERFULLCONTENT)
        if not ok:
            return False, "PrintWindow returned 0 (window refused to render)"

        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(bmi)
        bmi.biWidth = width
        bmi.biHeight = -height  # top-down
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0  # BI_RGB
        buf = ctypes.create_string_buffer(width * height * 4)
        _gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buf, ctypes.byref(bmi), 0)
        img = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1)
        img = img.convert("RGB")
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(dest), format="PNG")
        extrema = img.convert("L").getextrema()
        blank = extrema[0] == extrema[1]
        return True, ("rendered but uniformly blank (likely GPU/overlay window)"
                      if blank else "ok")
    finally:
        _gdi32.DeleteObject(hbmp)
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
        input_name = f"<error: {exc}>"
    saver = False
    idle = -1.0
    try:
        saver = _screensaver_running()
    except Exception:
        pass
    try:
        idle = _idle_seconds()
    except Exception:
        pass

    lines.append(f"input desktop: {input_name or '<unknown>'}")
    lines.append(f"thread desktop: {_thread_desktop_name() or '<unknown>'}")
    lines.append(f"screen saver running: {saver}")
    if input_name and input_name != "Default":
        lines.append(
            "state: input is NOT on the operator desktop "
            "(locked, screen saver, or secure desktop) -- "
            "window state below comes from the 'Default' desktop")
    else:
        lines.append("state: operator desktop has input (not locked)")
    if idle >= 0:
        lines.append(f"seconds since last input: {idle:.0f}")

    windows: list = []
    seen_titles = set()
    for hwnd in _enum_default_desktop_windows():
        info = _window_info(hwnd)
        if info is None:
            continue
        windows.append(info)
    titled = [w for w in windows if w["title"]]
    lines.append("")
    lines.append(f"{len(windows)} visible windows on 'Default' desktop "
                 f"({len(titled)} titled), front to back:")
    for w in windows:
        title = w["title"] if w["title"] else "<no title>"
        lines.append(
            f"  [{w['process'] or '?'}] {title} -- "
            f"{w['width']}x{w['height']} at ({w['left']}, {w['top']})")

    if include_window_captures:
        base = Path(output_dir) if output_dir else (
            Path.home() / "Desktop" / "screenshots" / "desktop_state"
            / time.strftime("%Y%m%d-%H%M%S"))
        lines.append("")
        lines.append(f"window captures -> {base}")
        shots: list = []
        capturable = [w for w in titled
                      if w['left'] > -30000 and w['top'] > -30000]
        for idx, w in enumerate(capturable[:max_windows]):
            name = _safe_filename(w["title"], f"window{idx}")
            dest = base / f"{idx:02d}_{name}.png"
            try:
                ok, note = _capture_window_png(w["hwnd"], dest)
            except Exception as exc:
                ok, note = False, f"capture error: {exc}"
            lines.append(f"  [{idx}] {w['title']}: "
                         f"{'saved ' + str(dest) if ok else 'FAILED'} ({note})")
            if ok:
                shots.append(dest)
        if len(shots) >= 2:
            try:
                from PIL import Image
                thumbs = []
                for p in shots:
                    im = Image.open(str(p))
                    im.thumbnail((640, 640))
                    thumbs.append(im)
                cols = 2
                rows = (len(thumbs) + cols - 1) // cols
                cell_w = max(t.width for t in thumbs)
                cell_h = max(t.height for t in thumbs)
                sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), "black")
                for i, t in enumerate(thumbs):
                    sheet.paste(t, ((i % cols) * cell_w, (i // cols) * cell_h))
                sheet_path = base / "contact_sheet.png"
                sheet.save(str(sheet_path), format="PNG")
                lines.append(f"  contact sheet: {sheet_path}")
            except Exception as exc:
                lines.append(f"  contact sheet FAILED: {exc}")
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
        return f"no visible window on 'Default' desktop matches: {title_contains}"
    dest = Path(output_path) if output_path else (
        Path.home() / "Desktop" / "screenshots" / "desktop_state"
        / f"{_safe_filename(target['title'], 'window')}-{time.strftime('%H%M%S')}.png")
    try:
        ok, note = _capture_window_png(target["hwnd"], dest)
    except Exception as exc:
        return f"capture failed for '{target['title']}': {exc}"
    if not ok:
        return f"capture failed for '{target['title']}': {note}"
    return (f"saved {dest} -- window '{target['title']}' "
            f"({target['width']}x{target['height']}, {note}). "
            f"Read it back with read_image.")

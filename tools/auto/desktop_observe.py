# -*- coding: utf-8 -*-
"""
Desktop observation tools that keep working while the workstation is
locked or the screen saver is running -- the integrated follow-up to
tools/auto/desktop_snapshot.py (which this module does NOT modify; it is
self-contained on purpose, so either module can be removed alone).

The tools:

  screen_full_desktop   -- ONE picture of the whole 'Default' desktop,
                           captured from a thread attached to that desktop
                           (SetThreadDesktop + BitBlt), instead of reading
                           the input desktop the way screenshot/screen_look
                           do. Works when input sits on 'Screen-saver' or
                           'Winlogon'.
  screen_state_report   -- one text report: session state (input desktop,
                           screen saver, idle), the windows on 'Default'
                           with their processes, the heaviest processes,
                           optional per-window PrintWindow captures, and an
                           optional OCR excerpt per capture so the content
                           is readable as text without an image round-trip.
  screen_lock_history   -- lock / unlock / sleep / wake timeline rebuilt
                           from the Windows event logs (the only answer
                           available for the time the machine was asleep:
                           nothing runs then, so the past is reconstructed,
                           not observed).
  screen_state_diff     -- what changed since the last screen_state_diff
                           (or baseline) call: windows opened/closed, lock
                           state changes. Cheap enough to poll.

Naming is deliberate: tools/tool_catalogue.py files tools into categories
by name fragments, first match wins, and 'screen_' is the screen rule --
so these names are what puts the tools in the index's screen category
when the auto-loader picks this module up. No catalogue edit is needed
and none was made.

Standard library + Pillow + psutil (both already in this server's venv).
OCR uses the tesseract binary when it is on PATH and says so plainly when
it is not.
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


# --------------------------------------------------------------------------
# session / desktop probes (same facts as desktop_snapshot, re-derived here
# so this module stands alone)
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
    h = _user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    if not h:
        return ""
    try:
        return _desktop_name(h)
    finally:
        _user32.CloseDesktop(h)


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
        size = wintypes.DWORD(len(buf))
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
        hwnds.append(hwnd)
        return True

    try:
        _user32.EnumDesktopWindows(hdesk, enum_proc_type(_cb), 0)
    finally:
        _user32.CloseDesktop(hdesk)

    out = []
    for hwnd in hwnds:
        if not _user32.IsWindowVisible(hwnd):
            continue
        length = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
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
            "minimized": rect.left <= -30000 or rect.top <= -30000,
        })
    return out


def _session_lines() -> list:
    name = ""
    try:
        name = _input_desktop_name()
    except Exception as exc:
        name = f"<error: {exc}>"
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
    lines = [f"input desktop: {name or '<unknown>'}",
             f"screen saver running: {saver}"]
    if name and name != "Default":
        lines.append("state: LOCKED / SCREEN-SAVER / SECURE DESKTOP "
                     "(input is not on the operator desktop)")
    else:
        lines.append("state: operator desktop has input (not locked)")
    if idle >= 0:
        lines.append(f"seconds since last input: {idle:.0f}")
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
    from PIL import Image
    bmi = _BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(bmi)
    bmi.biWidth = width
    bmi.biHeight = -height  # top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0
    buf = ctypes.create_string_buffer(width * height * 4)
    _gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buf, ctypes.byref(bmi), 0)
    img = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1)
    return img.convert("RGB")


def _is_blank(img) -> bool:
    extrema = img.convert("L").getextrema()
    return extrema[0] == extrema[1]


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
    hdc_screen = _user32.GetDC(None)
    hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = _gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
    try:
        _gdi32.SelectObject(hdc_mem, hbmp)
        if not _user32.PrintWindow(hwnd, hdc_mem, _PW_RENDERFULLCONTENT):
            return False, "PrintWindow returned 0 (window refused to render)"
        img = _dib_to_image(hdc_mem, hbmp, width, height)
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(dest), format="PNG")
        return True, ("rendered but uniformly blank (likely GPU/overlay window)"
                      if _is_blank(img) else "ok")
    finally:
        _gdi32.DeleteObject(hbmp)
        _gdi32.DeleteDC(hdc_mem)
        _user32.ReleaseDC(None, hdc_screen)


def _headless_creationflags() -> int:
    """CREATE_NO_WINDOW for this module's child processes (tesseract,
    PowerShell). The callers are unattended by construction (an MCP
    server with no console of its own), so a console child would
    allocate a fresh, visible console window. The repository's
    launch-site ratchet (tools/test_no_new_launch_inherits_its_console_
    by_accident.py) requires every launch to decide this explicitly;
    the canonical value lives in tools.childproc.
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
        return f"<OCR failed: {exc}>"


# --------------------------------------------------------------------------
# the tools
# --------------------------------------------------------------------------

def screen_full_desktop(output_path: str = "", max_dimension: int = 1600) -> str:
    """Capture the WHOLE 'Default' desktop as one picture, even while locked.

    A dedicated thread attaches itself to the 'Default' desktop
    (SetThreadDesktop) and BitBlts the virtual screen from there, so the
    picture is the operator's desktop as composed -- window layout intact --
    not the screen-saver / lock desktop that has input. This is the
    full-frame counterpart to desktop_snapshot's per-window captures, and
    unlike screenshot/screen_look it does not need the input desktop.

    Args:
        output_path: Where to save the PNG. Defaults to
            ~/Desktop/screenshots/desktop_state/full-<timestamp>.png.
        max_dimension: Downscale the longest edge to this many pixels.
    """
    dest = Path(output_path) if output_path else (
        Path.home() / "Desktop" / "screenshots" / "desktop_state"
        / f"full-{time.strftime('%Y%m%d-%H%M%S')}.png")
    result: dict = {}

    def _work():
        hdesk = _user32.OpenDesktopW(
            "Default", 0, False, _DESKTOP_READOBJECTS | _DESKTOP_ENUMERATE)
        if not hdesk:
            result["error"] = "OpenDesktop('Default') failed"
            return
        try:
            if not _user32.SetThreadDesktop(hdesk):
                result["error"] = ("SetThreadDesktop failed "
                                   f"(winerror {ctypes.get_last_error()})")
                return
            x = _user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
            y = _user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
            width = _user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
            height = _user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
            if width <= 0 or height <= 0:
                result["error"] = "virtual screen has no area"
                return
            hdc_screen = _user32.GetDC(None)
            hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
            hbmp = _gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
            try:
                _gdi32.SelectObject(hdc_mem, hbmp)
                _gdi32.BitBlt(hdc_mem, 0, 0, width, height,
                              hdc_screen, x, y, _SRCCOPY | _CAPTUREBLT)
                img = _dib_to_image(hdc_mem, hbmp, width, height)
            finally:
                _gdi32.DeleteObject(hbmp)
                _gdi32.DeleteDC(hdc_mem)
                _user32.ReleaseDC(None, hdc_screen)
            if max_dimension and max(img.size) > max_dimension:
                img.thumbnail((max_dimension, max_dimension))
            dest.parent.mkdir(parents=True, exist_ok=True)
            img.save(str(dest), format="PNG")
            result["size"] = img.size
            result["blank"] = _is_blank(img)
            result["origin"] = (x, y)
            result["full_size"] = (width, height)
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _user32.CloseDesktop(hdesk)

    worker = threading.Thread(target=_work, daemon=True)
    worker.start()
    worker.join(timeout=45)
    if worker.is_alive():
        return "full desktop capture timed out (capture thread still running)"
    if "error" in result:
        return f"full desktop capture failed: {result['error']}"
    note = (" -- rendered but uniformly blank (the desktop is composed "
            "elsewhere right now; per-window captures may still work)"
            if result.get("blank") else "")
    return (f"saved {dest} -- the whole 'Default' desktop, "
            f"{result['full_size'][0]}x{result['full_size'][1]} at "
            f"{result['origin']}, saved at {result['size'][0]}x"
            f"{result['size'][1]}{note}. Read it back with read_image.")


def screen_state_report(include_captures: bool = True,
                        include_ocr: bool = False,
                        max_windows: int = 4,
                        top_processes: int = 8) -> str:
    """One integrated desktop report that works while locked / screen saver.

    Combines, in a single call: the session state (input desktop, screen
    saver, idle seconds), every visible window on the 'Default' desktop
    with its process, the heaviest processes on the machine, and --
    optionally -- a PrintWindow capture per titled window plus an OCR
    excerpt of each capture, so the caller can read what the windows show
    as text without transferring any image.

    Args:
        include_captures: Render titled, non-minimized windows to PNG.
        include_ocr: Add a short OCR excerpt per capture (needs
            include_captures; uses the tesseract binary when present).
        max_windows: Cap on captures, front to back.
        top_processes: How many processes (by memory) to list.
    """
    lines = _session_lines()
    windows = _default_desktop_windows()
    titled = [w for w in windows if w["title"]]
    lines.append("")
    lines.append(f"{len(windows)} visible windows on 'Default' desktop "
                 f"({len(titled)} titled), front to back:")
    for w in windows:
        title = w["title"] if w["title"] else "<no title>"
        mini = " [minimized]" if w["minimized"] else ""
        lines.append(f"  [{w['process'] or '?'}] {title} -- "
                     f"{w['width']}x{w['height']} at ({w['left']}, {w['top']}){mini}")

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
        lines.append(f"top {top_processes} processes by memory:")
        for rss, name, pid in procs[:top_processes]:
            lines.append(f"  {name} (pid {pid}) -- {rss // (1024 * 1024)} MB")
    except Exception as exc:
        lines.append(f"process list unavailable: {exc}")

    if include_captures:
        base = (Path.home() / "Desktop" / "screenshots" / "desktop_state"
                / time.strftime("%Y%m%d-%H%M%S"))
        capturable = [w for w in titled if not w["minimized"]]
        lines.append("")
        lines.append(f"window captures -> {base}")
        for idx, w in enumerate(capturable[:max_windows]):
            dest = base / f"{idx:02d}_{_safe_filename(w['title'], 'window')}.png"
            try:
                ok, note = _capture_window_png(w["hwnd"], dest)
            except Exception as exc:
                ok, note = False, f"capture error: {exc}"
            lines.append(f"  [{idx}] {w['title']}: "
                         f"{'saved ' + str(dest) if ok else 'FAILED'} ({note})")
            if ok and include_ocr:
                lines.append(f"      OCR: {_ocr_excerpt(dest)}")
    return "\n".join(lines)


def screen_lock_history(days: int = 2, max_events: int = 30) -> str:
    """Lock / unlock / sleep / wake timeline from the Windows event logs.

    Nothing runs while the machine is asleep, so the state during sleep
    can only be reconstructed afterwards. This reads System-log power
    events (Kernel-Power: sleep, resume; Power-Troubleshooter: wake
    source) and Winlogon session events, newest first. Workstation
    lock/unlock (Security 4800/4801) is included when the Security log is
    readable and says so plainly when it is not.

    Args:
        days: How far back to look.
        max_events: Cap on returned events.
    """
    ps = shutil.which("powershell") or shutil.which("powershell.exe")
    if not ps:
        return "powershell not found on PATH; event log unavailable"
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$ErrorActionPreference='SilentlyContinue';"
        f"$start=(Get-Date).AddDays(-{int(days)});"
        # Filter by Id inside FilterHashtable so the log engine does the
        # narrowing: fetching N recent events first and filtering in
        # Where-Object misses everything on a busy System log (measured:
        # 2,000 events span hours here, and a 400-cap returned nothing for
        # days that do contain Kernel-Power rows).
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
        f" Select-Object -First {int(max_events)} | ForEach-Object {{"
        " '{0:yyyy-MM-dd HH:mm:ss} [{1} {2}] {3}' -f $_.TimeCreated,"
        " $_.ProviderName, $_.Id,"
        " (($_.Message -split \"`n\")[0].Trim()) }"
    )
    try:
        proc = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=60,
            creationflags=_headless_creationflags())
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        err = proc.stderr.decode("utf-8", errors="replace").strip()
    except Exception as exc:
        return f"event log query failed: {exc}"
    if not out:
        return (f"no lock/sleep/wake events found in the last {days} day(s) "
                f"(Security-log lock events may need audit policy; "
                f"query stderr: {err[:200]})")
    return (f"lock / sleep / wake events, last {days} day(s), newest first:\n"
            + out)


def screen_state_diff() -> str:
    """What changed on the desktop since the last call (or the baseline).

    Compares the current session state and titled-window set against the
    snapshot saved by the previous call in
    ~/Desktop/screenshots/desktop_state/state_last.json, reports windows
    opened / closed and lock-state changes, then saves the current state
    as the new baseline. The first call only establishes the baseline.
    """
    windows = _default_desktop_windows()
    current = {
        "input_desktop": _input_desktop_name(),
        "screensaver": _screensaver_running(),
        "windows": sorted(
            f"{w['process']} :: {w['title']}" for w in windows if w["title"]),
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
    store.write_text(json.dumps(current, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    if previous is None:
        return (f"baseline saved at {current['taken_at']}: input desktop "
                f"{current['input_desktop'] or '<unknown>'}, "
                f"{len(current['windows'])} titled windows. "
                "Call again to see what changed.")
    lines = [f"changes since {previous.get('taken_at', '<unknown>')}:"]
    if previous.get("input_desktop") != current["input_desktop"]:
        lines.append(f"  input desktop: {previous.get('input_desktop')} "
                     f"-> {current['input_desktop']}")
    if previous.get("screensaver") != current["screensaver"]:
        lines.append(f"  screen saver: {previous.get('screensaver')} "
                     f"-> {current['screensaver']}")
    before = set(previous.get("windows", []))
    after = set(current["windows"])
    for name in sorted(after - before):
        lines.append(f"  opened: {name}")
    for name in sorted(before - after):
        lines.append(f"  closed: {name}")
    if len(lines) == 1:
        lines.append("  no changes")
    return "\n".join(lines)

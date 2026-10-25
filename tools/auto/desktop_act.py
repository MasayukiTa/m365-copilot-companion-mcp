# -*- coding: utf-8 -*-
"""
Act on windows of the 'Default' desktop WITHOUT the input desktop --
the missing half of computer use while the machine is locked or the
screen saver is running.

FIXED VERSION. This is a corrected copy produced under tool-audit-fix;
the repository original is unchanged. See FIXLOG.md for the mapping of
finding number to the change made here.

WHAT THIS ADDS, and its honest limits.

  1. Window messages (PostMessage / SendMessage) addressed to a window
     handle on the 'Default' desktop. Delivery is by the window manager,
     not by the input desktop, so a click, a key or text can reach a
     window that is occluded, on the non-input desktop, or locked behind
     the screen saver.
       - Works well on standard Win32 controls (Button, Edit, dialogs).
       - Chromium / Electron windows often ignore synthetic WM_CHAR and
         mouse messages delivered this way; for those, use the UIA path.
       - It does not move the real cursor or change real focus, and it
         cannot touch the Winlogon desktop itself.
  2. UI Automation through the .NET client that ships with Windows,
     driven via PowerShell.

Standard library only (plus Pillow for the optional after-capture).
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from typing import Optional

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# --------------------------------------------------------------------------
# ctypes prototypes: argtypes / restype for every API used here.
# Finding 1, 2, 3, 22, 24: without these, ctypes defaults the return type
# to c_int (32-bit signed) and marshals pointer args as c_int, so HANDLEs,
# LRESULTs and byref() addresses are truncated on 64-bit Windows.
# --------------------------------------------------------------------------
LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t

try:
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
except Exception:  # pragma: no cover - non-Windows import guard
    WNDENUMPROC = None


def _configure_prototypes() -> None:
    k = _kernel32
    u = _user32

    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    k.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL

    u.OpenDesktopW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    u.OpenDesktopW.restype = wintypes.HANDLE
    u.CloseDesktop.argtypes = [wintypes.HANDLE]
    u.CloseDesktop.restype = wintypes.BOOL
    u.EnumDesktopWindows.argtypes = [
        wintypes.HANDLE, WNDENUMPROC, wintypes.LPARAM]
    u.EnumDesktopWindows.restype = wintypes.BOOL
    u.EnumChildWindows.argtypes = [
        wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
    u.EnumChildWindows.restype = wintypes.BOOL
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsWindowVisible.restype = wintypes.BOOL
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowTextW.restype = ctypes.c_int
    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetClassNameW.restype = ctypes.c_int
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindowRect.restype = wintypes.BOOL
    u.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    u.ScreenToClient.restype = wintypes.BOOL
    u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    u.ClientToScreen.restype = wintypes.BOOL
    u.ChildWindowFromPointEx.argtypes = [
        wintypes.HWND, wintypes.POINT, wintypes.UINT]
    u.ChildWindowFromPointEx.restype = wintypes.HWND
    u.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.PostMessageW.restype = wintypes.BOOL
    u.SendMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SendMessageW.restype = LRESULT
    u.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ULONG_PTR)]
    u.SendMessageTimeoutW.restype = LRESULT
    u.GetClassNameW.restype = ctypes.c_int
    u.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    u.MapVirtualKeyW.restype = wintypes.UINT


_configure_prototypes()

_DESKTOP_READOBJECTS = 0x0001
_DESKTOP_ENUMERATE = 0x0040
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONDOWN = 0x0204
_WM_RBUTTONUP = 0x0205
_WM_RBUTTONDBLCLK = 0x0206
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_CHAR = 0x0102
_WM_SETTEXT = 0x000C
_WM_GETTEXT = 0x000D
_WM_GETTEXTLENGTH = 0x000E
_WM_CLOSE = 0x0010
_BM_CLICK = 0x00F5
_MK_LBUTTON = 0x0001
_MK_RBUTTON = 0x0002

# SendMessageTimeout flags
_SMTO_ABORTIFHUNG = 0x0002
_SMTO_NORMAL = 0x0000

# ChildWindowFromPointEx skip flags
_CWP_SKIPINVISIBLE = 0x0001
_CWP_SKIPDISABLED = 0x0002
_CWP_SKIPTRANSPARENT = 0x0004

# MapVirtualKey translation
_MAPVK_VK_TO_VSC = 0

# lParam bits for WM_KEYDOWN / WM_KEYUP
_KF_EXTENDED = 0x0100  # bit 24 (<<16)
_KF_UP = 0x8000        # bit 31 (<<16)
_KF_REPEAT = 0x4000    # bit 30 (<<16)

_VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B,
    "escape": 0x1B, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "space": 0x20, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D, "ins": 0x2D,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
}

# Finding 13: keys that must carry the extended-key flag in lParam bit 24.
_EXTENDED_KEYS = {
    0x2E,  # delete
    0x2D,  # insert
    0x25, 0x26, 0x27, 0x28,  # left up right down
    0x24, 0x23,  # home end
    0x21, 0x22,  # pageup pagedown
    0x0D,  # numpad enter is extended; main Enter is not, but accept it
}


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
            return Path(buf.value).name
        return ""
    finally:
        _kernel32.CloseHandle(h)


def _window_text(hwnd) -> str:
    """Read a window's title without blocking on a hung window.

    Finding 5: GetWindowTextW sends WM_GETTEXT and can block on a hung
    window; use SendMessageTimeoutW(WM_GETTEXTLENGTH/WM_GETTEXT) with
    SMTO_ABORTIFHUNG so one wedged window cannot freeze enumeration.
    Finding 24: clamp length to >= 0 before allocating the buffer.
    """
    res = ULONG_PTR(0)
    ok = _user32.SendMessageTimeoutW(
        hwnd, _WM_GETTEXTLENGTH, 0, 0,
        _SMTO_ABORTIFHUNG, 500, ctypes.byref(res))
    if not ok:
        return ""
    length = int(res.value)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    got = ULONG_PTR(0)
    ok = _user32.SendMessageTimeoutW(
        hwnd, _WM_GETTEXT, length + 1, ctypes.cast(buf, wintypes.LPARAM),
        _SMTO_ABORTIFHUNG, 1000, ctypes.byref(got))
    if not ok:
        return ""
    return buf.value


def _enum_windows() -> list:
    """Enumerate visible top-level windows on the 'Default' desktop.

    Finding 6: on failure to open the desktop, surface GetLastError via
    a RuntimeError instead of returning [] silently.
    """
    hdesk = _user32.OpenDesktopW(
        "Default", 0, False, _DESKTOP_READOBJECTS | _DESKTOP_ENUMERATE)
    if not hdesk:
        err = ctypes.get_last_error()
        raise RuntimeError(
            "OpenDesktopW('Default') failed: GetLastError=%d" % err)
    hwnds: list = []

    def _cb(hwnd, _lp):
        hwnds.append(hwnd)
        return True

    # Finding 4: bind the callback object to a local so it is not GC'd
    # while EnumDesktopWindows is still iterating.
    cb = WNDENUMPROC(_cb)
    try:
        _user32.EnumDesktopWindows(hdesk, cb, 0)
    finally:
        _user32.CloseDesktop(hdesk)
    out = []
    for hwnd in hwnds:
        if not _user32.IsWindowVisible(hwnd):
            continue
        title = _window_text(hwnd)
        rect = wintypes.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            continue
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append({"hwnd": hwnd, "title": title,
                    "left": rect.left, "top": rect.top,
                    "width": rect.right - rect.left,
                    "height": rect.bottom - rect.top,
                    "pid": pid.value, "process": _process_name(pid.value)})
    return out


def _find_window(title_contains: str) -> Optional[dict]:
    low = title_contains.lower()
    for w in _enum_windows():
        if w["title"] and low in w["title"].lower():
            return w
    return None


def _children(hwnd) -> list:
    kids: list = []

    def _cb(child, _lp):
        kids.append(child)
        return True

    # Finding 4: keep the callback alive across the enumeration.
    cb = WNDENUMPROC(_cb)
    _user32.EnumChildWindows(hwnd, cb, 0)
    out = []
    for child in kids:
        cls = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(child, cls, 256)
        txt = _window_text(child)
        rect = wintypes.RECT()
        _user32.GetWindowRect(child, ctypes.byref(rect))
        out.append({"hwnd": child, "class": cls.value, "text": txt,
                    "width": rect.right - rect.left,
                    "height": rect.bottom - rect.top,
                    "area": max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)})
    return out


def _lparam_xy(x: int, y: int) -> int:
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def _to_client(hwnd, window_x: int, window_y: int, win: dict) -> tuple:
    pt = wintypes.POINT(win["left"] + window_x, win["top"] + window_y)
    _user32.ScreenToClient(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def _child_at(hwnd, client_x: int, client_y: int):
    """Find the deepest real child at a parent-client point.

    Finding 7: use ChildWindowFromPointEx with skip flags (invisible /
    disabled / transparent) and descend recursively so clicks land on a
    nested control, not an intermediate container.
    """
    flags = _CWP_SKIPINVISIBLE | _CWP_SKIPDISABLED | _CWP_SKIPTRANSPARENT
    current = hwnd
    cx, cy = client_x, client_y
    # Descend at most a bounded number of levels to avoid loops.
    for _ in range(16):
        pt = wintypes.POINT(cx, cy)
        child = _user32.ChildWindowFromPointEx(current, pt, flags)
        if not child or child == current:
            break
        # Translate the point into the child's client frame for the next
        # level: client(current) -> screen -> client(child).
        screen = wintypes.POINT(cx, cy)
        _user32.ClientToScreen(current, ctypes.byref(screen))
        _user32.ScreenToClient(child, ctypes.byref(screen))
        current = child
        cx, cy = screen.x, screen.y
    return current


# --------------------------------------------------------------------------
# message-based actions
# --------------------------------------------------------------------------

def screen_msg_click(title_contains: str, x: int, y: int,
                     button: str = "left", double: bool = False) -> str:
    """Click inside a 'Default'-desktop window by window message.

    x, y are pixels in the WINDOW's own frame (origin at the window's
    top-left, borders included). The click is posted to the child control
    under that point when there is one.
    """
    win = _find_window(title_contains)
    if win is None:
        return f"no visible window on 'Default' desktop matches: {title_contains}"
    # Finding 8: bounds-check the window-relative point.
    if not (0 <= int(x) < win["width"] and 0 <= int(y) < win["height"]):
        return (f"point ({x}, {y}) is outside the window frame "
                f"({win['width']}x{win['height']}) of '{win['title']}'")
    cx, cy = _to_client(win["hwnd"], int(x), int(y), win)
    target = _child_at(win["hwnd"], cx, cy)
    # coordinates for the child are in the child's own client frame
    pt = wintypes.POINT(win["left"] + int(x), win["top"] + int(y))
    _user32.ScreenToClient(target, ctypes.byref(pt))
    lp = _lparam_xy(pt.x, pt.y)
    if button == "right":
        down, up, dbl, mk = (_WM_RBUTTONDOWN, _WM_RBUTTONUP,
                             _WM_RBUTTONDBLCLK, _MK_RBUTTON)
    else:
        down, up, dbl, mk = (_WM_LBUTTONDOWN, _WM_LBUTTONUP,
                             _WM_LBUTTONDBLCLK, _MK_LBUTTON)
    if double:
        # Finding 20: a real double click is DOWN, UP, DBLCLK, UP -- not
        # two independent down/up pairs that the control may read as two
        # single clicks.
        _user32.PostMessageW(target, _WM_MOUSEMOVE, 0, lp)
        _user32.PostMessageW(target, down, mk, lp)
        _user32.PostMessageW(target, up, 0, lp)
        _user32.PostMessageW(target, dbl, mk, lp)
        _user32.PostMessageW(target, up, 0, lp)
        clicks = 2
    else:
        _user32.PostMessageW(target, _WM_MOUSEMOVE, 0, lp)
        _user32.PostMessageW(target, down, mk, lp)
        _user32.PostMessageW(target, up, 0, lp)
        clicks = 1
    return (f"posted {button} click x{clicks} at window-pixel ({x}, {y}) "
            f"to '{win['title']}' [{win['process']}] "
            f"(target child client {pt.x}, {pt.y}). "
            "Verify the effect with a window capture or screen_state_diff.")


def screen_msg_text(title_contains: str, text: str, method: str = "auto") -> str:
    """Type text into a 'Default'-desktop window by window message.

    method:
      'settext' -- WM_SETTEXT on the largest Edit/RichEdit child.
      'chars'   -- WM_CHAR per UTF-16 code unit to the top window.
      'auto'    -- settext when a text child exists and succeeds, else chars.
    """
    win = _find_window(title_contains)
    if win is None:
        return f"no visible window on 'Default' desktop matches: {title_contains}"
    # Finding 11: NUL terminates a unicode buffer; drop embedded NULs so
    # the whole string reaches the control.
    text = text.replace("\x00", "")
    kids = _children(win["hwnd"])
    edits = [k for k in kids
             if "edit" in k["class"].lower() or "rich" in k["class"].lower()]
    edits.sort(key=lambda k: k["area"], reverse=True)
    used = None
    settext_ok = False
    if method in ("auto", "settext") and edits:
        target = edits[0]
        buf = ctypes.create_unicode_buffer(text)
        rc = _user32.SendMessageW(target["hwnd"], _WM_SETTEXT, 0,
                                  ctypes.cast(buf, wintypes.LPARAM))
        readback_len = _user32.SendMessageW(
            target["hwnd"], _WM_GETTEXTLENGTH, 0, 0)
        # Finding 9: WM_SETTEXT returns TRUE on success; verify and, under
        # 'auto', fall through to WM_CHAR when it did not take.
        settext_ok = bool(rc) and int(readback_len) == len(text)
        used = (f"WM_SETTEXT on child [{target['class']}] of "
                f"'{win['title']}'; rc={int(rc)}, control now holds "
                f"{int(readback_len)} chars (expected {len(text)})")
        if method == "settext":
            return used
        if method == "auto" and settext_ok:
            return used
    if method in ("auto", "chars"):
        # Finding 12: WM_CHAR carries one UTF-16 code unit per message, so
        # characters outside the BMP (emoji) must be split into their
        # surrogate pair. Iterate UTF-16-LE code units.
        units = text.encode("utf-16-le")
        count = 0
        for i in range(0, len(units), 2):
            cu = units[i] | (units[i + 1] << 8)
            _user32.PostMessageW(win["hwnd"], _WM_CHAR, cu, 0)
            count += 1
            time.sleep(0.005)
        note = ""
        if used and not settext_ok:
            note = " (WM_SETTEXT did not verify; fell back to WM_CHAR)"
        return (f"posted {count} WM_CHAR code units to '{win['title']}' "
                f"[{win['process']}]{note}. Whether the application honoured "
                "them must be verified (capture / readback); Chromium/Electron "
                "windows commonly ignore them.")
    return used or "no text target found"


def _key_lparam(vk: int, scan: int, up: bool, extended: bool) -> int:
    """Build a WM_KEYDOWN / WM_KEYUP lParam.

    Finding 13: bits 16-23 hold the scan code, bit 24 the extended-key
    flag, bit 30 the previous key state, bit 31 the transition state.
    Repeat count (bits 0-15) is 1 for a single press.
    """
    lp = 1  # repeat count
    lp |= (scan & 0xFF) << 16
    if extended:
        lp |= 1 << 24
    if up:
        lp |= 1 << 30  # previous state down
        lp |= 1 << 31  # transition: being released
    return lp & 0xFFFFFFFF


def screen_msg_key(title_contains: str, key: str) -> str:
    """Press one named key in a 'Default'-desktop window by window message.

    key: enter, tab, esc, backspace, delete, insert, space, arrows, home,
    end, pageup, pagedown, f1-f12 (see the module table). Posted as
    WM_KEYDOWN/WM_KEYUP to the top window with a correct lParam (scan code
    and extended-key flag), so arrow / navigation / delete keys are not
    misread as numpad keys.
    """
    win = _find_window(title_contains)
    if win is None:
        return f"no visible window on 'Default' desktop matches: {title_contains}"
    # Finding 23: guard against None / empty key names.
    name = str(key or "").strip().lower()
    if not name:
        return "empty key name"
    vk = _VK.get(name)
    if vk is None:
        return f"unknown key name: {key} (known: {', '.join(sorted(_VK))})"
    scan = _user32.MapVirtualKeyW(vk, _MAPVK_VK_TO_VSC)
    extended = vk in _EXTENDED_KEYS
    down_lp = _key_lparam(vk, scan, up=False, extended=extended)
    up_lp = _key_lparam(vk, scan, up=True, extended=extended)
    _user32.PostMessageW(win["hwnd"], _WM_KEYDOWN, vk, down_lp)
    _user32.PostMessageW(win["hwnd"], _WM_KEYUP, vk, up_lp)
    return (f"posted key '{name}' (vk 0x{vk:02X}, scan 0x{scan:02X}, "
            f"extended={extended}) to '{win['title']}' [{win['process']}]")


def screen_msg_close(title_contains: str) -> str:
    """Ask a 'Default'-desktop window to close (WM_CLOSE).

    Posted to EVERY matching window, not sent, so a busy application
    cannot hang the caller.
    """
    low = title_contains.lower()
    wins = [w for w in _enum_windows()
            if w["title"] and low in w["title"].lower()]
    if not wins:
        return f"no visible window on 'Default' desktop matches: {title_contains}"
    for w in wins:
        _user32.PostMessageW(w["hwnd"], _WM_CLOSE, 0, 0)
    return (f"posted WM_CLOSE to {len(wins)} window(s) titled "
            f"'{wins[0]['title']}' [{wins[0]['process']}]")


# --------------------------------------------------------------------------
# UI Automation through the .NET client that ships with Windows
# --------------------------------------------------------------------------

def _headless_creationflags() -> int:
    """CREATE_NO_WINDOW for the PowerShell child below."""
    try:
        from tools.childproc import headless_creationflags
        return headless_creationflags()
    except Exception:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_uia(script_body: str, timeout: int = 45) -> str:
    """Run a UIA PowerShell snippet.

    Finding 15: write the script to a temp .ps1 and run it with -File so
    the body is never squeezed through a -Command string (command-line
    length limit, newline / backtick fragility, quoting).
    Finding 19: use Popen + communicate(timeout) and kill the child on
    timeout so a wedged PowerShell / COM holder does not survive.
    Finding 16: return a structured ok/message prefix the caller can test
    instead of matching free-form strings.
    """
    import shutil
    ps = shutil.which("powershell") or shutil.which("powershell.exe")
    if not ps:
        return "ERR: powershell not found on PATH; UI Automation unavailable"
    script = (
        "$ErrorActionPreference='Stop'\n"
        "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8\n"
        "Add-Type -AssemblyName UIAutomationClient\n"
        "Add-Type -AssemblyName UIAutomationTypes\n"
        + script_body)
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".ps1")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(script)
        proc = subprocess.Popen(
            [ps, "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_headless_creationflags())
        try:
            out_b, err_b = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            return f"ERR: UI Automation timed out after {timeout}s"
        out = out_b.decode("utf-8", errors="replace").strip()
        err = err_b.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            tail = (err or out)[:400]
            return f"ERR: UI Automation exit {proc.returncode}: {tail}"
        if err:
            out = (out + f"\n[stderr] {err[:400]}").strip()
        return "OK: " + (out or "(no output)")
    except Exception as exc:
        return f"ERR: UI Automation call failed: {exc}"
    finally:
        if path:
            try:
                os.unlink(path)
            except Exception:
                pass


def _ps_quote(text: str) -> str:
    # Finding 15: strip control characters (newline / carriage return)
    # before embedding in a single-quoted PowerShell literal.
    cleaned = text.replace("\r", "").replace("\n", "")
    return "'" + cleaned.replace("'", "''") + "'"


def screen_uia_dump(title_contains: str, max_elements: int = 40) -> str:
    """List the UI Automation elements of a window on any desktop."""
    body = """
$root = [System.Windows.Automation.AutomationElement]::RootElement
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
$target = $null
foreach ($w in $wins) { if ($w.Current.Name -like TITLEQ) { $target = $w; break } }
if (-not $target) { 'no window matches'; exit 2 }
'window: ' + $target.Current.Name + ' [' + $target.Current.ControlType.ProgrammaticName + ']'
$all = $target.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
$n = [Math]::Min($all.Count, MAXN)
for ($i = 0; $i -lt $n; $i++) {
  $e = $all.Item($i)
  $pats = ''
  foreach ($pair in @(@('Invoke', [System.Windows.Automation.InvokePattern]::Pattern), @('Value', [System.Windows.Automation.ValuePattern]::Pattern), @('Toggle', [System.Windows.Automation.TogglePattern]::Pattern), @('SelectionItem', [System.Windows.Automation.SelectionItemPattern]::Pattern), @('ExpandCollapse', [System.Windows.Automation.ExpandCollapsePattern]::Pattern))) {
    $o = $null
    try { if ($e.TryGetCurrentPattern($pair[1], [ref]$o)) { $pats += $pair[0] + ' ' } } catch {}
  }
  '{0} | {1} | {2}' -f $e.Current.Name, $e.Current.ControlType.ProgrammaticName, $pats
}
'elements shown: ' + $n + ' of ' + $all.Count
"""
    body = body.replace("TITLEQ", "'*' + " + _ps_quote(title_contains) + " + '*'")
    body = body.replace("MAXN", str(int(max_elements)))
    return _run_uia(body)


def screen_uia_act(title_contains: str, element_name: str,
                   action: str = "invoke", value: str = "") -> str:
    """Act on one named UI element in a window, via UI Automation."""
    actions = {
        "invoke": "$pat = $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)\n$pat.Invoke()",
        "setvalue": "$pat = $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)\n$pat.SetValue(VALUEQ)",
        "toggle": "$pat = $e.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)\n$pat.Toggle()",
    }
    if action not in actions:
        return f"ERR: unknown action: {action} (invoke / setvalue / toggle)"
    body = """
$root = [System.Windows.Automation.AutomationElement]::RootElement
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
$target = $null
foreach ($w in $wins) { if ($w.Current.Name -like TITLEQ) { $target = $w; break } }
if (-not $target) { 'no window matches'; exit 2 }
$all = $target.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
$e = $null
foreach ($c in $all) { if ($c.Current.Name -eq NAMEQ) { $e = $c; break } }
if (-not $e) { foreach ($c in $all) { if ($c.Current.Name -like NAMELIKE) { $e = $c; break } } }
if (-not $e) { 'no element matches'; exit 3 }
try {
ACTIONBODY
  'action completed on element: ' + $e.Current.Name
} catch {
  'FAILED: ' + $_.Exception.Message
  exit 1
}
"""
    body = body.replace("TITLEQ", "'*' + " + _ps_quote(title_contains) + " + '*'")
    body = body.replace("NAMELIKE", "'*' + " + _ps_quote(element_name) + " + '*'")
    body = body.replace("NAMEQ", _ps_quote(element_name))
    body = body.replace("ACTIONBODY", actions[action].replace("VALUEQ", _ps_quote(value)))
    return _run_uia(body)

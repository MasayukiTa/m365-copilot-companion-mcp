# -*- coding: utf-8 -*-
"""
Act on windows of the 'Default' desktop WITHOUT the input desktop --
the missing half of computer use while the machine is locked or the
screen saver is running.

WHAT WAS THERE. tools/desktop_input.py drives the machine with SendInput
only, and is explicit about its limit: SendInput lands on the desktop
that RECEIVES INPUT, so on 'Winlogon' or 'Screen-saver' nothing this
process sends arrives (thread_desktop_receives_input /
unreachable_desktop_reason exist to refuse in exactly that state).
A repo-wide search found no PostMessage, no SendMessage and no UI
Automation anywhere in tools/ -- there was no second route.

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
     driven via PowerShell (comtypes / pywinauto are not installed in
     this venv, and the harness refuses casual installs). UIA talks to
     the window, not the desktop, so it also reaches windows on the
     non-input desktop for applications that expose patterns (Invoke,
     Value, Toggle, SelectionItem).

Every action returns what it did and, where it can, a way to check:
verify with desktop_state / capture_window_by_title / screen_state_diff
from the observation modules. Actions are addressed by window-title
substring so a caller names the window it means, never a raw handle.

Standard library only (plus Pillow for the optional after-capture).
"""
from __future__ import annotations

import ctypes
import json
import shutil
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import Optional

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_DESKTOP_READOBJECTS = 0x0001
_DESKTOP_ENUMERATE = 0x0040
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_RBUTTONDOWN = 0x0204
_WM_RBUTTONUP = 0x0205
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

_VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B,
    "escape": 0x1B, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "space": 0x20, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
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


def _enum_windows() -> list:
    hdesk = _user32.OpenDesktopW(
        "Default", 0, False, _DESKTOP_READOBJECTS | _DESKTOP_ENUMERATE)
    if not hdesk:
        return []
    hwnds: list = []
    cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lp):
        hwnds.append(hwnd)
        return True

    try:
        _user32.EnumDesktopWindows(hdesk, cb_type(_cb), 0)
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
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append({"hwnd": hwnd, "title": buf.value,
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
    cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(child, _lp):
        kids.append(child)
        return True

    _user32.EnumChildWindows(hwnd, cb_type(_cb), 0)
    out = []
    for child in kids:
        cls = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(child, cls, 256)
        length = _user32.GetWindowTextLengthW(child)
        txt = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(child, txt, length + 1)
        rect = wintypes.RECT()
        _user32.GetWindowRect(child, ctypes.byref(rect))
        out.append({"hwnd": child, "class": cls.value, "text": txt.value,
                    "width": rect.right - rect.left,
                    "height": rect.bottom - rect.top,
                    "area": max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)})
    return out


def _lparam_xy(x: int, y: int) -> int:
    return (y << 16) | (x & 0xFFFF)


def _to_client(hwnd, window_x: int, window_y: int, win: dict) -> tuple:
    pt = wintypes.POINT(win["left"] + window_x, win["top"] + window_y)
    _user32.ScreenToClient(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def _child_at(hwnd, client_x: int, client_y: int):
    pt = wintypes.POINT(client_x, client_y)
    child = _user32.ChildWindowFromPoint(hwnd, pt)
    return child if child else hwnd


# --------------------------------------------------------------------------
# message-based actions
# --------------------------------------------------------------------------

def screen_msg_click(title_contains: str, x: int, y: int,
                     button: str = "left", double: bool = False) -> str:
    """Click inside a 'Default'-desktop window by window message.

    x, y are pixels in the WINDOW's own frame (the same frame a
    PrintWindow capture of that window returns: origin at the window's
    top-left, borders included). The click is posted to the child control
    under that point when there is one. Delivery does not need the input
    desktop, so it reaches occluded / locked-behind-saver windows --
    standard Win32 controls reliably; Chromium/Electron windows often
    ignore synthetic mouse messages (use screen_uia_act there).
    """
    win = _find_window(title_contains)
    if win is None:
        return f"no visible window on 'Default' desktop matches: {title_contains}"
    cx, cy = _to_client(win["hwnd"], int(x), int(y), win)
    target = _child_at(win["hwnd"], cx, cy)
    # coordinates for the child are in the child's own client frame
    pt = wintypes.POINT(win["left"] + int(x), win["top"] + int(y))
    _user32.ScreenToClient(target, ctypes.byref(pt))
    lp = _lparam_xy(pt.x, pt.y)
    down, up, mk = ((_WM_RBUTTONDOWN, _WM_RBUTTONUP, _MK_RBUTTON)
                    if button == "right" else
                    (_WM_LBUTTONDOWN, _WM_LBUTTONUP, _MK_LBUTTON))
    clicks = 2 if double else 1
    for _ in range(clicks):
        _user32.PostMessageW(target, _WM_MOUSEMOVE, 0, lp)
        _user32.PostMessageW(target, down, mk, lp)
        _user32.PostMessageW(target, up, 0, lp)
        time.sleep(0.05)
    return (f"posted {button} click x{clicks} at window-pixel ({x}, {y}) "
            f"to '{win['title']}' [{win['process']}] "
            f"(target hwnd class frame: client {pt.x}, {pt.y}). "
            "Verify the effect with a window capture or screen_state_diff.")


def screen_msg_text(title_contains: str, text: str, method: str = "auto") -> str:
    """Type text into a 'Default'-desktop window by window message.

    method:
      'settext' -- WM_SETTEXT on the largest Edit/RichEdit child (the
        reliable path for standard text controls; replaces its content).
      'chars'   -- WM_CHAR per character to the top window (append-style;
        some applications accept it, many modern ones do not).
      'auto'    -- settext when a text child exists, else chars.
    """
    win = _find_window(title_contains)
    if win is None:
        return f"no visible window on 'Default' desktop matches: {title_contains}"
    kids = _children(win["hwnd"])
    edits = [k for k in kids
             if "edit" in k["class"].lower() or "rich" in k["class"].lower()]
    edits.sort(key=lambda k: k["area"], reverse=True)
    used = None
    if method in ("auto", "settext") and edits:
        target = edits[0]
        buf = ctypes.create_unicode_buffer(text)
        _user32.SendMessageW(target["hwnd"], _WM_SETTEXT, 0,
                             ctypes.cast(buf, ctypes.c_void_p))
        readback_len = _user32.SendMessageW(target["hwnd"], _WM_GETTEXTLENGTH, 0, 0)
        used = (f"WM_SETTEXT on child [{target['class']}] of "
                f"'{win['title']}'; control now holds {readback_len} chars")
        if method == "settext" or method == "auto":
            return used
    if method in ("auto", "chars"):
        for ch in text:
            _user32.PostMessageW(win["hwnd"], _WM_CHAR, ord(ch), 0)
            time.sleep(0.005)
        return (f"posted {len(text)} WM_CHAR messages to '{win['title']}' "
                f"[{win['process']}]. Whether the application honoured them "
                "must be verified (capture / readback); Chromium/Electron "
                "windows commonly ignore them.")
    return used or "no text target found"


def screen_msg_key(title_contains: str, key: str) -> str:
    """Press one named key in a 'Default'-desktop window by window message.

    key: enter, tab, esc, backspace, delete, space, arrows, home, end,
    pageup, pagedown, f1-f12 (see the module table). Posted as
    WM_KEYDOWN/WM_KEYUP to the top window; standard controls and dialogs
    honour it, games and Chromium/Electron often do not.
    """
    win = _find_window(title_contains)
    if win is None:
        return f"no visible window on 'Default' desktop matches: {title_contains}"
    vk = _VK.get(key.strip().lower())
    if vk is None:
        return f"unknown key name: {key} (known: {', '.join(sorted(_VK))})"
    _user32.PostMessageW(win["hwnd"], _WM_KEYDOWN, vk, 0)
    _user32.PostMessageW(win["hwnd"], _WM_KEYUP, vk, 0)
    return f"posted key '{key}' (vk 0x{vk:02X}) to '{win['title']}' [{win['process']}]"


def screen_msg_close(title_contains: str) -> str:
    """Ask a 'Default'-desktop window to close (WM_CLOSE).

    This is the polite close -- the application may still prompt to save,
    and some frameworks (WinForms parking windows, Tk) expose several
    top-level handles under one title, so the message is posted to EVERY
    matching window: posting only to the first handle was observed to
    leave a WinForms fixture open. Posted, not sent, so a busy
    application cannot hang the caller.
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
    """CREATE_NO_WINDOW for the PowerShell child below.

    This module's callers are unattended by construction (an MCP server
    with no console of its own), so a console child would allocate a
    fresh, visible console window. The repository's launch-site ratchet
    (tools/test_no_new_launch_inherits_its_console_by_accident.py)
    requires every launch to decide this explicitly; the canonical
    value lives in tools.childproc.
    """
    try:
        from tools.childproc import headless_creationflags
        return headless_creationflags()
    except Exception:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_uia(script_body: str, timeout: int = 45) -> str:
    ps = shutil.which("powershell") or shutil.which("powershell.exe")
    if not ps:
        return "powershell not found on PATH; UI Automation unavailable"
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8\n"
        "Add-Type -AssemblyName UIAutomationClient\n"
        "Add-Type -AssemblyName UIAutomationTypes\n"
        + script_body)
    try:
        proc = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout,
            creationflags=_headless_creationflags())
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        if err:
            out = (out + f"\n[stderr] {err[:400]}").strip()
        return out or "(no output)"
    except Exception as exc:
        return f"UI Automation call failed: {exc}"


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def screen_uia_dump(title_contains: str, max_elements: int = 40) -> str:
    """List the UI Automation elements of a window on any desktop.

    Returns each element's name, control type and which patterns it
    supports (Invoke / Value / Toggle / SelectionItem), so a following
    screen_uia_act call can be aimed at a real element instead of
    guessed pixels. UIA reaches windows by handle, not by input desktop.
    """
    body = """
$root = [System.Windows.Automation.AutomationElement]::RootElement
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
$target = $null
foreach ($w in $wins) { if ($w.Current.Name -like TITLEQ) { $target = $w; break } }
if (-not $target) { 'no window matches'; exit }
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
    """Act on one named UI element in a window, via UI Automation.

    action 'invoke'   -- InvokePattern (buttons, menu items, links).
    action 'setvalue' -- ValuePattern.SetValue(value) (text fields,
                         including the modern Notepad document and most
                         Chromium/Electron text inputs).
    action 'toggle'   -- TogglePattern (checkboxes, switches).
    Element lookup is by exact Name first, then substring.
    """
    actions = {
        "invoke": "$pat = $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)\n$pat.Invoke()",
        "setvalue": "$pat = $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)\n$pat.SetValue(VALUEQ)",
        "toggle": "$pat = $e.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)\n$pat.Toggle()",
    }
    if action not in actions:
        return f"unknown action: {action} (invoke / setvalue / toggle)"
    body = """
$root = [System.Windows.Automation.AutomationElement]::RootElement
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
$target = $null
foreach ($w in $wins) { if ($w.Current.Name -like TITLEQ) { $target = $w; break } }
if (-not $target) { 'no window matches'; exit }
$all = $target.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
$e = $null
foreach ($c in $all) { if ($c.Current.Name -eq NAMEQ) { $e = $c; break } }
if (-not $e) { foreach ($c in $all) { if ($c.Current.Name -like NAMELIKE) { $e = $c; break } } }
if (-not $e) { 'no element matches'; exit }
try {
ACTIONBODY
  'action completed on element: ' + $e.Current.Name
} catch {
  'FAILED: ' + $_.Exception.Message
}
"""
    body = body.replace("TITLEQ", "'*' + " + _ps_quote(title_contains) + " + '*'")
    body = body.replace("NAMELIKE", "'*' + " + _ps_quote(element_name) + " + '*'")
    body = body.replace("NAMEQ", _ps_quote(element_name))
    body = body.replace("ACTIONBODY", actions[action].replace("VALUEQ", _ps_quote(value)))
    return _run_uia(body)

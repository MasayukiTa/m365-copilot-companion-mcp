"""edge_recover.py -- recover a wedged companion Edge by closing its tabs ONE BY ONE.

WHY one-by-one (and not just X-ing the window or killing the process):
  Edge has SESSION RESTORE. If you close the whole window with X -- or kill msedge --
  Edge records the session as "was still open" and RESTORES all those tabs on the next
  launch, bringing the wedged M365 conversations right back, so the stall recurs (this
  was observed directly). Closing each tab via CDP page.close() records them as
  INTENTIONALLY closed, so the next launch comes up clean.

  This is the recovery path for the symptom where the fleet's synchronous attach()
  stalls because the dedicated Edge stops responding: close every tab here, then the
  fleet / bridge can proceed on a fresh tab.

NOTE on surface() truthfulness (fixed 2026-07-04): surface() now reflects REAL success --
  it verifies (via the live msedge process list, see _headed_process_present()) that a
  headed companion-Edge process actually exists after the launcher call, and returns
  False if not, instead of returning True whenever subprocess.run merely didn't throw.
  Callers MUST gate any "surfaced!" notification on this return value. One inherent
  limitation remains: a caller process that is already running with the OLD surface()
  loaded in memory keeps the old (always-True) behavior until it is restarted -- fixing
  the source file does not retroactively patch a live process's imported bytecode.

Usage:
  python -m relay.edge_recover               # close all tabs, leave one blank tab
  python -m relay.edge_recover --to-agent    # ... and open a fresh agent chat instead
  python -m relay.edge_recover --cdp-url http://localhost:9222
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def cdp_alive(cdp_url="http://localhost:9222", timeout_ms=5000):
    """Quick health check: can we reach the Edge over CDP? (Used by the fleet's auto-
    recovery to tell a live Edge from a wedged/dead one.) MUST be called from a thread
    that is NOT inside another Playwright sync call -- the sync API is not re-entrant."""
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(cdp_url, timeout=timeout_ms)
            _ = b.contexts
            try:
                b.close()
            except Exception:
                pass
        return True
    except Exception:
        return False


def hard_reset(port=9222, wait=True):
    """Kill the companion Edge, wipe its session-restore state, relaunch -- by invoking
    start_companion_edge.ps1 -HardReset (the verified path). Safe to call from a thread:
    it shells out to PowerShell and touches NO Playwright. Returns True on success."""
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ps1 = os.path.join(repo, "scripts", "start_companion_edge.ps1")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1,
             "-HardReset", "-Port", str(port)],
            cwd=repo, timeout=120 if wait else 5,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _profile_for_port(port):
    """Map a CDP port to the Edge user-data-dir profile the launcher uses.
      :9223 -> copilot-bridge-edge (the interactive bridge)
      anything else -> the companion/fleet default (env MCP_EDGE_PROFILE or
                        'copilot-companion-edge').
    This mirrors scripts/start_companion_edge.ps1's -Profile default so we read the
    right PER-PROFILE mode file (.fleet\\edge_mode_<profile>)."""
    try:
        if int(port) == 9223:
            return "copilot-bridge-edge"
    except Exception:
        pass
    return os.environ.get("MCP_EDGE_PROFILE") or "copilot-companion-edge"


def _read_mode(fleet_dir, port):
    """Read the launcher's remembered window mode ('headless'/'headed'/'') for the
    Edge on `port`. Prefers the PER-PROFILE file .fleet\\edge_mode_<profile> that the
    launcher actually writes; falls back to the plain .fleet\\edge_mode only if the
    per-profile file is missing (it may be a stale/different artifact). Never raises."""
    profile = _profile_for_port(port)
    for name in ("edge_mode_" + profile, "edge_mode"):
        try:
            mf = os.path.join(fleet_dir, name)
            if os.path.isfile(mf):
                return open(mf).read().strip()
        except Exception:
            pass
    return ""


def _surface_flag(port, fleet_dir):
    """Pure decision helper (no side effects, no shelling out): which launcher flag
    surface() should invoke for the Edge on `port`.
      headless  -> '-Foreground'  (no window exists; the launcher now kills the
                   headless instance and relaunches HEADED so the user can sign in)
      otherwise -> '-Surface'     (a real window exists; just bring it to the front)
    Exposed separately so the decision can be unit-tested without launching Edge."""
    return "-Foreground" if _read_mode(fleet_dir, port) == "headless" else "-Surface"


def _msedge_cmdlines():
    """Snapshot of ' '.join(cmdline) for every currently-running msedge.exe process.
    Returns [] (never raises) if psutil is unavailable or enumeration fails -- callers
    must treat that the same as "no matching process found", not as an error."""
    try:
        import psutil
    except Exception:
        return []
    out = []
    try:
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                nm = (p.info.get("name") or "").lower()
                if "msedge" not in nm:
                    continue
                out.append(" ".join(p.info.get("cmdline") or []))
            except Exception:
                continue
    except Exception:
        return []
    return out


def _headed_process_present(profile_marker, cmdlines):
    """PURE decision helper (no I/O, no psutil call of its own) -- unit-testable with a
    synthetic cmdlines list. Given the msedge cmdlines currently running (as produced by
    _msedge_cmdlines()) and the profile marker for a port (_profile_for_port(port)),
    decide whether a HEADED companion-Edge MAIN BROWSER process for that profile exists.

    True  <-> some MAIN BROWSER process cmdline mentions `profile_marker` AND does NOT
              contain '--headless'.
    False <-> no matching main-browser process at all, or every matching one is headless.

    CRITICAL: only the main browser process's cmdline carries (or omits) '--headless=new'.
    Its child processes (--type=renderer / gpu-process / utility / crashpad-handler / ...)
    inherit --user-data-dir (so they DO contain profile_marker) but do NOT repeat
    '--headless' even when the browser is running fully headless -- verified directly
    against a live 'Get-CimInstance Win32_Process -Filter "Name=\\'msedge.exe\\'"' listing.
    A helper that scanned every child process would misclassify a headless instance as
    headed just because one of its non-main subprocesses lacks the flag. We therefore
    restrict the scan to processes WITHOUT a '--type=' argument, which is exactly the
    main browser process (its child processes always carry --type=)."""
    for cmd in cmdlines:
        if profile_marker not in cmd:
            continue
        if "--type=" in cmd:
            continue  # a child process (renderer/gpu/utility/crashpad/...), not the main browser
        if "--headless" not in cmd:
            return True
    return False


def _surface_launcher_argv(ps1, flag, port, open_url=""):
    """PURE helper (no I/O) building the powershell argv surface() shells out to -- split out
    so the -Url plumbing is unit-testable without launching Edge. `open_url` is threaded
    through as -Url ONLY when both it is non-empty AND flag=='-Foreground': that is the one
    path that can actually (re)launch the browser (headless->headed kill+relaunch, or a fresh
    launch), so it is the only path where a target URL means anything. -Surface merely raises
    an already-headed window -- it never navigates -- so -Url would be a no-op there and is
    deliberately omitted to match the launcher's own -Surface behavior."""
    argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1,
            flag, "-Port", str(port)]
    if open_url and flag == "-Foreground":
        argv += ["-Url", open_url]
    return argv


def surface(port=9222, poll_timeout_s=8.0, poll_interval_s=0.5, open_url=""):
    """Bring the (minimized/background/headless) companion Edge to the foreground --
    used when sign-in is required so the user can complete it. Shells out to the
    launcher; no Playwright, thread-safe (swallows errors, never raises).

    Returns a TRUTHFUL bool: whether a headed (foreground-able) companion Edge process
    for this port's profile actually exists after the attempt -- NOT merely whether the
    subprocess call didn't throw. Callers MUST gate their "surfaced!" notification on
    this return value; a stale caller that ignores it and always claims success is
    exactly the bug this function used to have.

    If the running instance is HEADLESS there is no window to raise, so we invoke the
    launcher with -Foreground: it now kills the headless instance and relaunches HEADED
    (see start_companion_edge.ps1). Relaunching takes a moment, so we poll briefly
    (up to `poll_timeout_s`) for a headed process for this profile to appear.

    If the running instance is already HEADED, we invoke -Surface (raise the existing
    window) and treat "a headed process for this profile exists" as success -- the
    launcher's own window-find can fail silently (e.g. race, no top-level window yet),
    so we verify independently via the process list rather than trusting its exit code.

    `open_url` (optional): when a HEADED RELAUNCH actually happens (the headless->headed
    swap, or Edge was not running at all), pass this through as the launcher's -Url so the
    window lands on the caller's target conversation (e.g. the agent URL being driven for
    a genuine sign-in) instead of the launcher's default generic top page. Default ""
    preserves the old behavior exactly (launcher's own default $Url). Ignored on the plain
    -Surface path (raising an already-headed window never navigates it)."""
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ps1 = os.path.join(repo, "scripts", "start_companion_edge.ps1")
    fleet = os.path.join(repo, ".fleet")
    # tell the background keeper to stop re-hiding the window while the user signs in
    try:
        os.makedirs(fleet, exist_ok=True)
        open(os.path.join(fleet, "edge_keep_pause"), "w").write(str(time.time()))
    except Exception:
        pass
    flag = _surface_flag(port, fleet)
    profile = _profile_for_port(port)
    try:
        subprocess.run(
            _surface_launcher_argv(ps1, flag, port, open_url),
            cwd=repo, timeout=60 if flag == "-Foreground" else 15,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False
    # Verify the REAL outcome rather than trusting subprocess.run's exit code. A -Foreground
    # headless->headed swap needs a moment to kill+relaunch, so poll briefly; -Surface on an
    # already-headed process should be near-instant but a short poll costs little and covers
    # any race between the launcher's SetForegroundWindow and our check.
    deadline = time.time() + max(0.0, poll_timeout_s)
    while True:
        if _headed_process_present(profile, _msedge_cmdlines()):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(max(0.05, poll_interval_s))


def touch_pause():
    """Create/refresh the <repo>\\.fleet\\edge_keep_pause mtime so the background
    keeper (edge_keeper.ps1) keeps backing off. surface() writes this file ONCE at
    the start of sign-in, but the keeper's age check expires after 180s -- a slow
    MFA login would then get re-minimized mid-typing. Callers driving a login wait
    loop call this every iteration (~1s) to keep the pause fresh for as long as the
    login page is showing. Thread-safe (pure filesystem, no Playwright); swallows
    errors so a wait loop never dies on a transient FS hiccup."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fleet = os.path.join(repo, ".fleet")
    try:
        os.makedirs(fleet, exist_ok=True)
        with open(os.path.join(fleet, "edge_keep_pause"), "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


# PowerShell snippet that finds the DEDICATED companion Edge's top-level window
# (msedge process whose command line contains 'copilot-companion-edge', its
# Chrome_WidgetWin_1 window) and MINIMIZES it right away (ShowWindow SW_MINIMIZE=6) --
# but ONLY if it is currently visible and not already minimized: minimizing a hidden
# (e.g. headless) window makes Windows set WS_VISIBLE and show it minimized instead,
# which reveals a window that was supposed to have no on-screen presence at all.
# Mirrors the Find()/ShowWindow technique in scripts\win\edge_keeper.ps1. ASCII only.
_REHIDE_PS = r'''
$ErrorActionPreference = "SilentlyContinue"
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public class RK {
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] static extern int GetClassName(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll")] static extern int GetWindowTextLength(IntPtr h);
  delegate bool EnumProc(IntPtr h, IntPtr p);
  public static IntPtr Find(int[] pids) {
    IntPtr found = IntPtr.Zero;
    HashSet<int> set = new HashSet<int>(pids);
    EnumWindows(delegate(IntPtr h, IntPtr p) {
      uint pid; GetWindowThreadProcessId(h, out pid);
      if (set.Contains((int)pid)) {
        StringBuilder sb = new StringBuilder(64); GetClassName(h, sb, 64);
        if (sb.ToString() == "Chrome_WidgetWin_1" && GetWindowTextLength(h) > 0) { found = h; return false; }
      }
      return true;
    }, IntPtr.Zero);
    return found;
  }
}
"@
$pids = @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
          Where-Object { $_.CommandLine -match 'copilot-companion-edge' } |
          ForEach-Object { [int]$_.ProcessId })
if ($pids.Count -gt 0) {
  $h = [RK]::Find($pids)
  # Only minimize a window that is actually visible: a headless (WS_VISIBLE clear)
  # window has no window to minimize, and ShowWindow(SW_MINIMIZE) on it makes Windows
  # SET WS_VISIBLE and show it minimized -- creating a taskbar button. Mirrors the
  # same guard in scripts\win\edge_keeper.ps1.
  if ($h -ne [IntPtr]::Zero -and [RK]::IsWindowVisible($h) -and -not [RK]::IsIconic($h)) {
    [RK]::ShowWindow($h, 6) | Out-Null
  }
}
'''


def rehide():
    """Return the companion Edge to the background IMMEDIATELY once auth completes:
    delete the keeper's pause file (so it resumes its 2s re-minimize duty) and
    directly minimize the window RIGHT NOW rather than waiting up to 2s for the
    keeper's next tick. Shells out to a PowerShell snippet that finds the dedicated
    Edge (command line contains 'copilot-companion-edge') and calls ShowWindow(6)
    on its Chrome_WidgetWin_1 window -- mirrors edge_keeper.ps1. Thread-safe like
    surface() (no Playwright) and swallows all errors."""
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fleet = os.path.join(repo, ".fleet")
    # Remove the pause first so the keeper is free to re-minimize on its own tick too.
    try:
        pf = os.path.join(fleet, "edge_keep_pause")
        if os.path.isfile(pf):
            os.remove(pf)
    except Exception:
        pass
    # Minimize immediately so the window drops to the background without a 2s lag.
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _REHIDE_PS],
            cwd=repo, timeout=20,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def companion_edge_mb(profile_marker="copilot-companion-edge"):
    """Total resident memory (MB) of the DEDICATED companion Edge -- isolated from the
    user's main Edge by matching `profile_marker` (its user-data-dir) in the command line.
    Returns 0.0 if psutil is unavailable or no matching process is found."""
    try:
        import psutil
    except Exception:
        return 0.0
    total = 0
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            nm = (p.info.get("name") or "").lower()
            if "msedge" not in nm:
                continue
            cmd = " ".join(p.info.get("cmdline") or [])
            if profile_marker in cmd:
                total += p.memory_info().rss
        except Exception:
            continue
    return total / (1024.0 * 1024.0)


def should_recycle(edge_mb, free_mb, edge_cap_mb=1500.0, free_floor_mb=1000.0):
    """Decide whether to hard-reset the companion Edge BEFORE a run, to keep it lean.
    Returns (recycle: bool, reason: str). Recycle when the dedicated Edge has bloated past
    `edge_cap_mb`, or free RAM has dropped below `free_floor_mb` (the heavy M365 SPA is
    unreliable under pressure -- a fresh profile state stabilizes it)."""
    if edge_mb and edge_mb > edge_cap_mb:
        return (True, "companion Edge at %d MB (> %d cap)" % (round(edge_mb), round(edge_cap_mb)))
    if free_mb and free_mb < free_floor_mb:
        return (True, "only %d MB free RAM (< %d floor)" % (round(free_mb), round(free_floor_mb)))
    return (False, "")


def looks_like_login(url):
    u = (url or "").lower()
    return ("login.microsoftonline" in u or "login.live.com" in u
            or "/signin" in u or "oauth2/authorize" in u)


def close_all_tabs(cdp_url="http://localhost:9222", connect_timeout_ms=8000,
                   open_url=None):
    """Close every tab of the Edge at `cdp_url`, one by one, leaving exactly one fresh
    tab open (at `open_url` or about:blank). Returns a dict result.

    A keeper tab is opened FIRST: closing the final tab can terminate the whole browser,
    and we want Edge to stay up on a clean page. If the CDP endpoint does not answer
    within `connect_timeout_ms`, Edge is truly dead -- the caller must kill + relaunch
    it (see start_companion_edge.ps1), which is clean because the launcher hides the
    restore bubble and we never leave wedged tabs marked 'open'.
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url, timeout=connect_timeout_ms)
        except Exception as e:
            return {"ok": False, "error": "cdp unreachable: " + type(e).__name__ + ": " + str(e),
                    "hint": "Edge is unresponsive (CDP dead) -- run a hard reset: "
                            ".\\scripts\\start_companion_edge.ps1 -HardReset  (kills it, wipes "
                            "session-restore so wedged tabs are NOT restored, relaunches)"}
        ctx = browser.contexts[0] if browser.contexts else None
        if ctx is None:
            return {"ok": False, "error": "no browser context"}

        originals = list(ctx.pages)
        keeper = ctx.new_page()                      # keep the browser alive on a clean tab
        try:
            keeper.goto(open_url or "about:blank", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        closed = 0
        for pg in originals:
            try:
                pg.close()                            # records the tab as intentionally closed
                closed += 1
                time.sleep(0.2)
            except Exception:
                pass

        remaining = 0
        try:
            remaining = len(ctx.pages)
        except Exception:
            pass
        return {"ok": True, "closed": closed, "remaining": remaining,
                "keeper": open_url or "about:blank"}


def main():
    ap = argparse.ArgumentParser(
        description="Recover a wedged companion Edge by closing its tabs one by one.")
    ap.add_argument("--cdp-url", default=os.environ.get("MCP_CDP_URL", "http://localhost:9222"))
    ap.add_argument("--to-agent", action="store_true",
                    help="open a fresh agent chat as the keeper tab (uses MCP_IMPL_AGENT_URL)")
    ap.add_argument("--connect-timeout-ms", type=int, default=8000)
    args = ap.parse_args()

    open_url = None
    if args.to_agent:
        open_url = (os.environ.get("MCP_FLEET_AGENT_URL")
                    or os.environ.get("MCP_IMPL_AGENT_URL") or None)

    res = close_all_tabs(args.cdp_url, args.connect_timeout_ms, open_url)
    if res.get("ok"):
        print("recovered: closed %d tab(s) one by one; %d tab(s) remain (keeper: %s)"
              % (res["closed"], res["remaining"], res["keeper"]))
    else:
        print("recovery failed: %s" % res.get("error"))
        if res.get("hint"):
            print("  hint: %s" % res["hint"])
        sys.exit(1)


if __name__ == "__main__":
    main()

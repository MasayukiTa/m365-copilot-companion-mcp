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
    ps1 = os.path.join(repo, "start_companion_edge.ps1")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1,
             "-HardReset", "-Port", str(port)],
            cwd=repo, timeout=120 if wait else 5,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def surface(port=9222):
    """Bring the (minimized, background) companion Edge to the foreground -- used when
    sign-in is required so the user can complete it. Shells out to the launcher's
    -Surface mode (Win32 restore + foreground); no Playwright, thread-safe."""
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ps1 = os.path.join(repo, "start_companion_edge.ps1")
    fleet = os.path.join(repo, ".fleet")
    # tell the background keeper to stop re-hiding the window while the user signs in
    try:
        os.makedirs(fleet, exist_ok=True)
        open(os.path.join(fleet, "edge_keep_pause"), "w").write(str(time.time()))
    except Exception:
        pass
    mode = ""
    try:
        mf = os.path.join(fleet, "edge_mode")
        if os.path.isfile(mf):
            mode = open(mf).read().strip()
    except Exception:
        pass
    # headless has no window to bring forward -> relaunch HEADED so the user can sign in
    flag = "-Foreground" if mode == "headless" else "-Surface"
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1,
             flag, "-Port", str(port)],
            cwd=repo, timeout=60 if flag == "-Foreground" else 15,
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
                            ".\\start_companion_edge.ps1 -HardReset  (kills it, wipes "
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

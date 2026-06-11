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

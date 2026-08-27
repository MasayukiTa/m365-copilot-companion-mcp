"""Which blocked resource type stops the composer from rendering?

The capture fails after the opener has waited its full 75 seconds for a composer that never
appears. That is a rendering question, not a protocol one, so it can be answered without
spending a single Copilot turn: open the page under each candidate set and look for the
composer.
"""
import importlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, r"C:\Users\M118A8586\resonac-mcp")

SETS = [
    "",                                  # control: no blocking at all
    "image",
    "font",
    "media",
    "stylesheet",
    "image,font,media",
    "image,font,media,stylesheet",
]


def agent_url():
    for path in (os.path.join(os.environ.get("APPDATA", ""), "copilot-bridge", ".env"),
                 r"C:\Users\M118A8586\resonac-mcp\.env"):
        if os.path.isfile(path):
            for line in open(path, encoding="utf-8-sig", errors="replace"):
                if line.startswith("MCP_FLEET_AGENT_URL="):
                    return line.split("=", 1)[1].strip()
    return ""


def main():
    from playwright.sync_api import sync_playwright

    url = agent_url()
    from relay.relay_fleet import COPILOT_SELECTORS
    sel = COPILOT_SELECTORS["composer"]

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=30000)
        context = browser.contexts[0]
        for spec in SETS:
            os.environ["MCP_CAPTURE_LEAN_TYPES"] = spec
            import relay.lean_capture as L
            importlib.reload(L)
            page = context.new_page()
            interception = L.install(page) if spec else None
            t0 = time.time()
            rendered = False
            try:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    pass
                for _ in range(40):
                    page.wait_for_timeout(1000)
                    if page.locator(sel).count() > 0:
                        rendered = True
                        break
            except Exception as exc:
                print("  %-28s ERROR %s" % (spec or "(no blocking)", str(exc)[:60]))
            finally:
                stats = interception.stats() if interception else {}
                if interception:
                    interception.teardown()
                try:
                    page.close()
                except Exception:
                    pass
            print("  %-28s composer=%-5s %5.1fs  %s"
                  % (spec or "(no blocking)", rendered, time.time() - t0, stats), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

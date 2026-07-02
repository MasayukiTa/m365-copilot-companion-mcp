"""edge_reconnect.py -- AUTOMATICALLY re-establish the MCP connector connection in the
companion Edge, with NO user credential entry.

WHY this exists:
  The connector's connection is per-browser-PROFILE. The fleet runs in the headless
  :9222 Edge; its connection to the MCP connector goes stale ("古い") -- then the
  agent can no longer call list_directory / run_python / create_file and just reports
  "ローカル操作は実行不可", looping to MAXTURNS. Reconnecting is NOT a credential sign-in:
  the Bearer key is already configured on the connector. It is only the connection-SELECT
  confirm card (接続マネージャーを開く -> レビュー -> 送信する), which is safe to auto-click.
  This is the same click-through RelayWorker._auto_consent does mid-run; here it is a
  standalone, on-demand operation so the connection can be repaired without a human and
  without waiting for a full fleet run to stumble into the card.

FLOW:
  connect to :9222 over CDP -> open the impl/fleet agent -> send a tiny connector probe
  -> if the reply is the consent card, click it through -> re-probe to confirm the
  connector now answers with a real result.

  python -m relay.edge_reconnect                 # uses MCP_FLEET_AGENT_URL from .env
  python -m relay.edge_reconnect --probe "デスクトップのmdの数を数えて"
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay.copilot_autopilot_relay import COPILOT_SELECTORS, CopilotWebDriver

# A probe that REQUIRES the connector (list_directory) and asks for a one-line answer, so
# a working connector returns "N個" while a connector-less default Copilot says 実行不可.
# The probe target is the current user's Desktop, resolved at runtime so it works for any user.
_DESKTOP_PROBE_PATH = (
    os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Desktop")
    .replace("\\", "/")
)
DEFAULT_PROBE = (
    "接続確認。call_tool 経由で list_directory を使い "
    + _DESKTOP_PROBE_PATH + " 直下の項目数だけを『N個』の形で1行で答えて。"
)
CONSENT_MARKERS = ("接続マネージャーを開く", "connection manager")
NO_CONNECTOR_MARKERS = ("実行不可", "コネクタ無し", "コネクタがありません", "ツールが使用できません")


def _load_agent_url() -> str:
    try:
        from dotenv import load_dotenv
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        load_dotenv(os.path.join(repo, ".env"), override=True)
    except Exception:
        pass
    return (os.environ.get("MCP_FLEET_AGENT_URL")
            or os.environ.get("MCP_IMPL_AGENT_URL") or "").strip()


def click_through_consent(page) -> bool:
    """Click the MCP connection-consent card through to a committed connection.
    Mirrors RelayWorker._auto_consent: 接続マネージャーを開く (popup) -> レビュー ->
    送信する. NOT a credential entry. Returns True iff 送信する was clicked."""
    try:
        ctx = page.context
        link = page.locator('a:has-text("接続マネージャーを開く"), a:has-text("connection manager")')
        if not link.count():
            return False
        try:
            with ctx.expect_page(timeout=15000) as pinfo:
                link.first.click()
            cs = pinfo.value
        except Exception:
            return False                 # opened in-place / no popup
        try:
            cs.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        for _ in range(15):              # let the CS /auth redirect settle
            try:
                if "/auth" not in (cs.url or ""):
                    break
            except Exception:
                break
            cs.wait_for_timeout(2000)
        try:                             # stale connection -> レビュー opens the select dialog
            rev = cs.locator('a:has-text("レビュー"), button:has-text("レビュー"), a:has-text("Review")')
            if rev.count():
                rev.first.click()
                cs.wait_for_timeout(3000)
        except Exception:
            pass
        submitted = False                # the connection is pre-selected -> just submit
        for label in ("送信する", "送信", "Submit"):
            try:
                btn = cs.locator('button:has-text("%s")' % label)
                if btn.count():
                    btn.first.click()
                    cs.wait_for_timeout(4000)
                    submitted = True
                    break
            except Exception:
                continue
        try:
            cs.close()
        except Exception:
            pass
        try:
            page.bring_to_front()
        except Exception:
            pass
        return submitted
    except Exception:
        return False


def reconnect_via_connection_manager(cdp_url: str, conn_url: str, want: str = "260616test",
                                     max_rounds: int = 6) -> dict:
    """Re-establish stale ("古い") connector connections directly on the Copilot Studio
    user-connections page -- NO credential entry (the Bearer key is already on the
    connector; this only re-selects + commits the connection). Proven flow (2026-06-30):
    navigate to the .../conversations/<id>/user-connections page (already signed in in the
    companion Edge profile) -> for each `want` row showing レビュー, click レビュー -> the
    接続の作成または選択 dialog opens with the connection pre-selected (green check) -> click
    送信する. Repeat until no `want` row is stale. Returns a summary dict."""
    from playwright.sync_api import sync_playwright
    out = {"ok": False, "rounds": 0, "submitted": 0, "any_stale_left": True, "url": ""}
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(cdp_url)
        ctx = b.contexts[0] if b.contexts else b.new_context()
        page = ctx.new_page()
        try:
            page.goto(conn_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(8000)
            out["url"] = page.url
            for _ in range(max_rounds):
                # a レビュー control whose row text contains `want`
                rev = page.locator(
                    'xpath=//*[self::a or self::button or @role="button"][normalize-space()="レビュー"'
                    ' or normalize-space()="Review"]')
                target = None
                for i in range(rev.count()):
                    el = rev.nth(i)
                    try:
                        row = el.locator('xpath=ancestor::*[contains(., "%s")][1]' % want)
                        if row.count():
                            target = el
                            break
                    except Exception:
                        continue
                if target is None:
                    break                          # no stale `want` row left
                out["rounds"] += 1
                try:
                    target.scroll_into_view_if_needed()
                    target.click()
                    page.wait_for_timeout(4000)
                except Exception:
                    break
                submit = page.locator(
                    'xpath=//button[normalize-space()="送信する" or normalize-space()="送信"'
                    ' or normalize-space()="Submit"]')
                if submit.count():
                    try:
                        submit.first.click()
                        out["submitted"] += 1
                        page.wait_for_timeout(6000)
                    except Exception:
                        pass
            page.reload()
            page.wait_for_timeout(8000)
            body = (page.locator("body").inner_text() or "")
            # any `want` row still 古い?
            stale = False
            for line in body.splitlines():
                if want in line and ("古い" in line or "期限切れ" in line or "未接続" in line):
                    stale = True
                    break
            out["any_stale_left"] = stale
            out["ok"] = (out["submitted"] > 0) and not stale
        finally:
            try:
                page.close()
            except Exception:
                pass
    return out


def reconnect(cdp_url: str, agent_url: str, probe: str, turn_timeout_s: int = 180) -> dict:
    from playwright.sync_api import sync_playwright
    out = {"ok": False, "agent_loaded": False, "had_card": False, "clicked": False,
           "resp1": "", "resp2": "", "url": ""}
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(cdp_url)
        ctx = b.contexts[0] if b.contexts else b.new_context()
        page = ctx.new_page()
        try:
            page.goto(agent_url, wait_until="domcontentloaded", timeout=45000)
            for _ in range(40):          # wait for the composer (agent surface ready)
                page.wait_for_timeout(1000)
                if page.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                    out["agent_loaded"] = True
                    break
            out["url"] = page.url
            if not out["agent_loaded"]:
                out["error"] = "composer never rendered (agent did not load)"
                return out
            drv = CopilotWebDriver(page)
            drv.send(probe)
            drv.wait_for_idle(timeout_s=turn_timeout_s)
            resp = drv.read_last_response() or ""
            out["resp1"] = resp[:600]
            out["had_card"] = any(m in resp for m in CONSENT_MARKERS)
            if out["had_card"]:
                out["clicked"] = click_through_consent(page)
                if out["clicked"]:
                    drv.send(probe)      # re-invoke the tool on the now-valid connection
                    drv.wait_for_idle(timeout_s=turn_timeout_s)
                    resp2 = drv.read_last_response() or ""
                    out["resp2"] = resp2[:600]
            final = out["resp2"] or out["resp1"]
            out["ok"] = (
                "個" in final
                and not any(m in final for m in NO_CONNECTOR_MARKERS)
                and not any(m in final for m in CONSENT_MARKERS)
            )
        finally:
            try:
                page.close()
            except Exception:
                pass
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Auto-reconnect the MCP connector (no credentials).")
    ap.add_argument("--cdp-url", default=os.environ.get("MCP_CDP_URL", "http://localhost:9222"))
    ap.add_argument("--agent-url", default="")
    ap.add_argument("--probe", default=DEFAULT_PROBE)
    ap.add_argument("--turn-timeout", type=int, default=180)
    ap.add_argument("--reconnect-url", default="",
                    help="Copilot Studio .../user-connections page URL. When set, drive that "
                         "page (レビュー -> 送信する) to re-establish stale connections, no chat probe.")
    ap.add_argument("--want", default="260616test",
                    help="connector row name to reconnect on the user-connections page")
    args = ap.parse_args(argv)
    if args.reconnect_url:
        res = reconnect_via_connection_manager(args.cdp_url, args.reconnect_url, args.want)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        if res.get("ok"):
            print("\nRECONNECT OK: all '%s' connections are no longer stale." % args.want)
            return 0
        print("\nReconnect incomplete -- see summary (any_stale_left).")
        return 1
    agent_url = args.agent_url or _load_agent_url()
    if not agent_url:
        print("ERROR: no agent URL (set MCP_FLEET_AGENT_URL in .env or pass --agent-url)")
        return 2
    res = reconnect(args.cdp_url, agent_url, args.probe, args.turn_timeout)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("ok"):
        print("\nRECONNECT OK: the connector answered with a real result.")
        return 0
    if res.get("had_card") and not res.get("clicked"):
        print("\nNEEDS MANUAL: consent card appeared but auto click-through failed.")
        return 1
    if not res.get("agent_loaded"):
        print("\nAGENT DID NOT LOAD: the deep-link fell back to default Copilot (no connector).")
        return 1
    print("\nNO CARD / connector still unavailable -- see resp1.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

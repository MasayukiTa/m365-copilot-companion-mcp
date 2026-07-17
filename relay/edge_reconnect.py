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
# Honors OneDrive Known Folder Move (Desktop redirected under "OneDrive - <org>\Desktop") --
# see bridge/copilot_bridge.py's _resolve_desktop_dir (same fix, kept in sync).
def _resolve_desktop_dir() -> str:
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            val, _ = winreg.QueryValueEx(key, "Desktop")
            resolved = os.path.expandvars(val)
            if resolved:
                return resolved.replace("\\", "/")
    except Exception:
        pass
    return os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Desktop").replace("\\", "/")


_DESKTOP_PROBE_PATH = _resolve_desktop_dir()
DEFAULT_PROBE = (
    "接続確認。call_tool 経由で list_directory を使い "
    + _DESKTOP_PROBE_PATH + " 直下の項目数だけを『N個』の形で1行で答えて。"
)
CONSENT_MARKERS = ("接続マネージャーを開く", "connection manager")
NO_CONNECTOR_MARKERS = ("実行不可", "コネクタ無し", "コネクタがありません", "ツールが使用できません")

# Commit buttons for the connection-select / consent dialog. 送信する = the older
# 接続の作成または選択 dialog; 許可 = the newer 接続して続行する dialog. First one present wins.
COMMIT_LABELS = ("送信する", "送信", "Submit", "許可", "Allow")
# Text that means at least one connection is still not usable, used to derive stale_left.
STALE_MARKERS = ("古い", "期限切れ", "未接続")


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Where the connection-manager (接続の管理 / user-connections) URL is cached so the relay can
# DIRECT-HIT it next time instead of re-driving the 接続マネージャーを開く popup. The URL is
# per-conversation but the /auth landing page reliably lists all connections, so a cached URL is
# reusable across runs until it 404s/redirects to login.
CONN_URL_CACHE = os.path.join(_repo_root(), ".fleet", "conn_manager_url.txt")


def save_conn_url(url: str) -> None:
    """Persist the connection-manager URL for later DIRECT-HIT. Best-effort; never raises."""
    try:
        if not url:
            return
        os.makedirs(os.path.dirname(CONN_URL_CACHE), exist_ok=True)
        with open(CONN_URL_CACHE, "w", encoding="utf-8") as fh:
            fh.write(url.strip())
    except Exception:
        pass


def load_conn_url() -> str:
    """Return the cached connection-manager URL, or '' if none. Never raises."""
    try:
        with open(CONN_URL_CACHE, "r", encoding="utf-8") as fh:
            return (fh.read() or "").strip()
    except Exception:
        return ""


def _stale_left(page) -> bool:
    """True if any レビュー control remains OR the body text still mentions a stale connection."""
    try:
        rev = page.locator(
            'xpath=//*[self::a or self::button or @role="button"][normalize-space()="レビュー"'
            ' or normalize-space()="Review"]')
        if rev.count():
            return True
    except Exception:
        pass
    try:
        body = (page.locator("body").inner_text() or "")
        return any(m in body for m in STALE_MARKERS)
    except Exception:
        return False


def fix_all_stale_connections(page, max_rounds: int = 12) -> dict:
    """Given a Playwright page ALREADY on the connection-manager (接続の管理 / user-connections)
    page, re-establish EVERY stale ("古い") connection -- NO credential entry (the Bearer key is
    already on the connector; this only re-selects + commits the connection).

    For each round: find every 「レビュー」/「Review」 control (regardless of row name), click the
    first one, and in the resulting dialog click whichever commit button exists (送信する/送信/
    Submit/許可/Allow). Wait for the dialog to close, then re-scan. Reload between rounds so the
    freshly-committed row drops out of the レビュー set. Loop until no レビュー control remains or
    max_rounds is reached. Returns {"submitted": N, "stale_left": bool}."""
    submitted = 0
    for _ in range(max_rounds):
        try:
            rev = page.locator(
                'xpath=//*[self::a or self::button or @role="button"][normalize-space()="レビュー"'
                ' or normalize-space()="Review"]')
            n = rev.count()
        except Exception:
            n = 0
        if not n:
            break                                   # no stale row left
        try:
            el = rev.first
            el.scroll_into_view_if_needed()
            el.click()
            page.wait_for_timeout(3000)             # let the select/consent dialog render
        except Exception:
            break
        # commit whichever button the dialog offers (pre-selected connection -> just confirm)
        did_commit = False
        for label in COMMIT_LABELS:
            try:
                btn = page.locator('button:has-text("%s")' % label)
                if btn.count():
                    btn.first.click()
                    submitted += 1
                    did_commit = True
                    page.wait_for_timeout(4000)     # wait for the dialog to close / commit
                    break
            except Exception:
                continue
        # reload so the committed row leaves the レビュー set; if nothing committed, reload anyway
        # to recover from a dialog that opened without a recognized button (avoids an infinite loop
        # clicking the same レビュー).
        try:
            page.reload(wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(6000)
        except Exception:
            pass
        if not did_commit:
            # couldn't commit this レビュー -> stop rather than spin; report stale_left honestly
            break
    return {"submitted": submitted, "stale_left": _stale_left(page)}


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
    """Click the MCP connection-consent card through to a committed connection. NOT a credential
    entry. Handles two variants:
      (a) the chat card itself has a 許可/Allow button (接続して続行する) -> one click completes it;
      (b) the card has a 接続マネージャーを開く link -> open the popup, cache its URL, then
          fix ALL stale rows via fix_all_stale_connections.
    Returns True iff a commit happened."""
    try:
        # variant (a): 許可/Allow directly on the chat card -> single click completes consent.
        for label in ("許可", "Allow"):
            try:
                btn = page.locator('button:has-text("%s")' % label)
                if btn.count():
                    btn.first.click()
                    page.wait_for_timeout(4000)
                    return True
            except Exception:
                continue
        # variant (b): 接続マネージャーを開く opens the connection-manager popup.
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
        try:
            save_conn_url(cs.url)         # cache the settled URL for later DIRECT-HIT
        except Exception:
            pass
        res = fix_all_stale_connections(cs)   # fix ALL stale rows, not just the first
        try:
            cs.close()
        except Exception:
            pass
        try:
            page.bring_to_front()
        except Exception:
            pass
        return res.get("submitted", 0) > 0
    except Exception:
        return False


def reconnect_via_connection_manager(cdp_url: str, conn_url: str, want: str = "",
                                     max_rounds: int = 12) -> dict:
    """Re-establish stale ("古い") connector connections directly on the Copilot Studio
    user-connections page -- NO credential entry (the Bearer key is already on the connector;
    this only re-selects + commits the connection). Proven flow (2026-06-30): navigate to the
    .../conversations/<id>/user-connections page (already signed in in the companion Edge
    profile) -> for each row showing レビュー, click レビュー -> the 接続の作成または選択 dialog
    opens with the connection pre-selected -> click 送信する (or 許可 in the newer dialog).

    `want`="" (default) fixes ALL stale rows via fix_all_stale_connections. When `want` is a
    non-empty name, only レビュー controls whose row text contains that name are clicked (the
    original CLI behavior). Returns a summary dict."""
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
            save_conn_url(page.url)          # cache for later DIRECT-HIT

            if not want:
                # ALL rows -> shared helper drives every レビュー regardless of name.
                res = fix_all_stale_connections(page, max_rounds=max_rounds)
                out["submitted"] = res["submitted"]
                out["rounds"] = res["submitted"]
                out["any_stale_left"] = res["stale_left"]
                out["ok"] = (res["submitted"] > 0) and not res["stale_left"]
                return out

            # name-filtered path (preserve the --want CLI flag).
            for _ in range(max_rounds):
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
                    ' or normalize-space()="Submit" or normalize-space()="許可"'
                    ' or normalize-space()="Allow"]')
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
            stale = False
            for line in body.splitlines():
                if want in line and any(m in line for m in STALE_MARKERS):
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
    ap.add_argument("--want", default="",
                    help="connector row name to reconnect on the user-connections page; "
                         "empty (default) = fix ALL stale rows regardless of name")
    args = ap.parse_args(argv)
    if args.reconnect_url:
        res = reconnect_via_connection_manager(args.cdp_url, args.reconnect_url, args.want)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        if res.get("ok"):
            scope = ("'%s'" % args.want) if args.want else "all"
            print("\nRECONNECT OK: %s connections are no longer stale." % scope)
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

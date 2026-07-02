# -*- coding: utf-8 -*-
"""Auto-fix the agent's stale connection consent end-to-end:
  1. drive the agent to call a tool -> Copilot shows the connection-verify card
  2. extract the card's '接続マネージャーを開く' link (conversation-specific) and open it
  3. on that connection manager: 古い filter -> for each stale -> レビュー -> 送信する
Screens + button dumps at each step. Reusable; run against the 1 stale connection left for test.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from dotenv import load_dotenv
load_dotenv(os.path.join(REPO, ".env"))
from playwright.sync_api import sync_playwright
from relay.copilot_autopilot_relay import CopilotWebDriver, find_conversation_page

AGENT = os.environ.get("MCP_FLEET_AGENT_URL") or os.environ.get("MCP_IMPL_AGENT_URL")
SHOT = lambda n: os.path.join(REPO, ".fleet", "_csfx_%s.png" % n)


def buttons(pg):
    out = []
    for el in pg.query_selector_all("button, [role=button], a[role=button], a"):
        try:
            t = (el.inner_text() or "").strip().replace("\n", " ")
        except Exception:
            t = ""
        if t and t not in out:
            out.append(t)
    return out


def click_text(pg, text, contains=False):
    for el in pg.query_selector_all("button, [role=button], a[role=button], a"):
        try:
            t = (el.inner_text() or "").strip()
        except Exception:
            t = ""
        if t and ((text in t and len(t) < 16) if contains else t == text):
            try:
                el.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            try:
                el.click()
                return t
            except Exception:
                pass
    return None


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    ctx = b.contexts[0] if b.contexts else b.new_context()
    chat = find_conversation_page(ctx, AGENT)
    chat.bring_to_front()
    drv = CopilotWebDriver(chat)
    try:
        drv.send(u"list_my_tools を呼んで、unlock があるか教えて。")
    except Exception as e:
        print("send error:", e)
    chat.wait_for_timeout(14000)

    # extract the connection-manager link (full href)
    cm_url = None
    for el in chat.query_selector_all("a[href]"):
        try:
            t = (el.inner_text() or "").strip()
            href = el.get_attribute("href") or ""
        except Exception:
            continue
        if u"接続マネージャー" in t and "copilotstudio" in href:
            cm_url = href
            break
    print("connection-manager URL found:", bool(cm_url))
    if not cm_url:
        print("NO card link -- maybe no card this time (already consented?). buttons:", buttons(chat)[:10])
        sys.exit(0)

    cm = ctx.new_page()
    cm.goto(cm_url, wait_until="domcontentloaded", timeout=60000)
    cm.wait_for_timeout(7000)
    click_text(cm, u"古い")
    cm.wait_for_timeout(3000)
    cm.screenshot(path=SHOT("1_stale"), full_page=True)
    print("connection-manager buttons (古い view):", buttons(cm))

    r = click_text(cm, u"レビュー") or click_text(cm, u"レビュー", contains=True) or click_text(cm, u"管理")
    cm.wait_for_timeout(3500)
    cm.screenshot(path=SHOT("2_review"), full_page=False)
    print("clicked review-ish:", r, "| dialog buttons:", buttons(cm))

    s = click_text(cm, u"送信する") or click_text(cm, u"送信", contains=True) or click_text(cm, u"許可")
    cm.wait_for_timeout(4000)
    cm.screenshot(path=SHOT("3_after"), full_page=False)
    print("clicked submit-ish:", s)
print("shots ->", SHOT("1_stale"), SHOT("2_review"), SHOT("3_after"))

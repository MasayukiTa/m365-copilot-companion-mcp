"""copilot_bridge.py -- a Python-only chat front-end for an M365 Copilot agent.

NO Node, NO npm, NO build step, NO Chrome. Just:  python bridge/copilot_bridge.py
Requirements on any PC: Python + pip (playwright) + Edge (built into Windows).

It makes the M365 Copilot agent look like a local assistant: a self-contained
HTML chat page (served by Python's stdlib http.server) talks to an SSE endpoint
that drives the agent over CDP and **streams the answer token-by-token** by
differential scraping -- the partial answer grows in the `loading-message` element
during generation, and `lastChatMessage` populates when it's done.

This is the "no Premium / no Direct Line" path: we drive the Copilot web UI you
are already signed into, instead of paying for the Direct Line API.

Setup (once):
  * Launch Edge with the debug port:
      & "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" --remote-debugging-port=9222
  * In .env set the bare agent URL of YOUR agent (the one with the MCP connector):
      MCP_IMPL_AGENT_URL=https://m365.cloud.microsoft/chat/agent/T_....<id>
  * Run:  .venv\\Scripts\\python.exe bridge\\copilot_bridge.py
  * Open http://127.0.0.1:8765
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DELETE_LOG = REPO / ".fleet" / "delete_log.jsonl"
GUID_RE = re.compile(r"/conversation/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


def _conv_guid(url):
    """Extract the /conversation/<guid> id from an agent conversation URL, or ''."""
    if not url:
        return ""
    m = GUID_RE.search(url)
    return m.group(1) if m else ""


def _log_delete(guid, title, ok, reason):
    """Append one delete-attempt record to .fleet/delete_log.jsonl (exception-safe)."""
    try:
        DELETE_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": round(time.time(), 3), "guid": guid or "", "title": (title or "")[:120],
               "ok": bool(ok), "reason": reason or ""}
        with open(DELETE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

from dotenv import load_dotenv
from relay.copilot_autopilot_relay import COPILOT_SELECTORS, CopilotWebDriver, PROCESSING_MARKERS

load_dotenv()

# Main-chat prompt clamp. Kept in TWO separate parts on purpose:
#   * STYLE  -- kill the impl agent's advisor/lecturer/ego persona (what the user hated).
#   * EXECUTION -- but do NOT let "be concise" become "do the minimum and stop". The full fleet
#     OUTPUT_DISCIPLINE contains "質問には直接かつ簡潔に答えて止まる / 求められたもののみ", which on a
#     SINGLE-TURN main-chat task made the agent HALT after the first tool result (e.g. it returned
#     the email body and stopped instead of continuing the task). So here we explicitly tell it to
#     keep going to completion. (The fleet doesn't need this clause because its PROTOCOL already
#     adds the autonomous CONTINUE/DONE loop on top of the discipline; the bridge has no such loop.)
BRIDGE_DISCIPLINE = (
    "【スタイル規律】自我・人格・キャラ付け、助言者ぶった上から目線や決めつけ"
    "（『今の理解レベルだと』『初心者の9割は』式）、頼まれていない講釈・長い前置き・"
    "命令調コーチング（『まずは〜しろ』『〜を完璧に固めろ』式）は出さない。淡々と実務的に。\n"
    "【実行規律】依頼されたタスクは途中の一手（例: メール取得）で止めず、必要なツールを"
    "連続して使い、最後まで自律的に実行・完了させること。簡潔さのために作業を省略しない。"
    "成果物・操作結果を出し、続けるべき作業が残っていれば自分で続行する。\n\n"
)

LOADING = '[data-testid="loading-message"]'   # holds the GROWING partial answer
LASTMSG = '[data-testid="lastChatMessage"]'    # populates when the turn is DONE

HELP_TEXT = (
    "## 使えるスラッシュコマンド\n\n"
    "### 委譲コマンド（別エージェントへ委譲・数分）\n"
    "- `/research <調べたいこと>` — M365 リサーチ ツールを **Claude (Anthropic)** に切替えて deep research（確認→承認→本実行、数分）。`/deepresearch` `/dr` も同じ。\n"
    "- `/analyze <ファイルの絶対パス> | <分析指示>` — アナリストにデータファイルを渡して分析（数値は鵜呑みにせず自分でも確かめて）。`/an` も同じ。\n\n"
    "### プロンプトテンプレート（このエージェントが即応答・通常ストリーム）\n"
    "- `/summarize <文章/トピック>` — 要点を箇条書きで簡潔に要約。\n"
    "- `/translate <言語> <文章>` — 指定言語へ翻訳。\n"
    "- `/plan <ゴール>` — 具体的な手順プランを作成。\n"
    "- `/critique <文章>` — 批判的レビュー（長所/短所/リスク）。\n"
    "- `/proofread <文章>` — 校正し、修正版＋変更点リストを返す。\n"
    "- `/rewrite <スタイル> <文章>` — 指定スタイルで書き直し。\n"
    "- `/brainstorm <トピック>` — 10 個のアイデアを発想。\n"
    "- `/steps <タスク>` — 番号付きの実行可能ステップに分解。\n"
    "- `/eli5 <トピック>` — できるだけ易しく説明。\n"
    "- `/proscons <トピック>` — 賛否（メリット/デメリット）を表で。\n"
    "- `/table <説明>` — 説明に沿った Markdown 表を作成。\n\n"
    "### その他\n"
    "- `/history` — （HTTP `GET /history?url=...` 経由）会話全文をロール付きで取得。\n"
    "- `/help` — このヘルプ。`/?` `/commands` も同じ。\n\n"
    "それ以外の文は、そのまま Copilot エージェントに送られます。"
)

# Slash commands that are pure prompt-templates: they transform the user's args
# into a fully-written instruction and send it through the NORMAL streaming path,
# so the answer streams back like an ordinary turn (no delegation / side page).
# value = (usage_hint, builder(arg) -> templated prompt string)
PROMPT_TEMPLATES = {
    "summarize": (
        "/summarize <要約したい文章またはトピック>",
        lambda a: "次の内容を、日本語で簡潔に箇条書き（3〜6点）で要約してください。"
                  "重要な事実・結論を漏らさず、冗長な表現は省いてください。\n\n--- 対象 ---\n" + a,
    ),
    "translate": (
        "/translate <言語> <翻訳したい文章>",
        lambda a: (lambda parts: (
            "次の文章を【" + parts[0] + "】に翻訳してください。"
            "自然で読みやすい訳文だけを返し、原文の意味を正確に保ってください。\n\n--- 原文 ---\n" + parts[1]
        ))((a.split(None, 1) + [""])[:2]),
    ),
    "plan": (
        "/plan <達成したいゴール>",
        lambda a: "次のゴールを達成するための、具体的で実行可能なステップバイステップの計画を日本語で作成してください。"
                  "各ステップに番号を振り、必要なら所要時間・前提条件・注意点も添えてください。\n\n--- ゴール ---\n" + a,
    ),
    "critique": (
        "/critique <批評したい文章/案>",
        lambda a: "次の内容を批判的にレビューしてください。日本語で、(1) 長所 (2) 短所 (3) リスク・懸念 "
                  "(4) 改善提案 の見出しに分けて、それぞれ箇条書きで挙げてください。\n\n--- 対象 ---\n" + a,
    ),
    "proofread": (
        "/proofread <校正したい文章>",
        lambda a: "次の文章を校正してください。まず『修正版』として誤字脱字・文法・表現を直した全文を示し、"
                  "続いて『変更点』として何をどう直したかを箇条書きで列挙してください。日本語で回答してください。\n\n--- 原文 ---\n" + a,
    ),
    "rewrite": (
        "/rewrite <スタイル> <書き直したい文章>",
        lambda a: (lambda parts: (
            "次の文章を【" + parts[0] + "】のスタイルで書き直してください。"
            "内容の意味は保ったまま、トーンと表現だけを変えてください。書き直した文章だけを返してください。\n\n--- 原文 ---\n" + parts[1]
        ))((a.split(None, 1) + [""])[:2]),
    ),
    "brainstorm": (
        "/brainstorm <発想したいトピック>",
        lambda a: "次のトピックについて、多様な切り口で 10 個のアイデアをブレインストーミングしてください。"
                  "番号付きリストで、各アイデアは1〜2文で簡潔に説明してください。日本語で。\n\n--- トピック ---\n" + a,
    ),
    "steps": (
        "/steps <分解したいタスク>",
        lambda a: "次のタスクを、実行可能で具体的な番号付きステップに分解してください。"
                  "各ステップは1行で、動詞から始める形で書いてください。日本語で。\n\n--- タスク ---\n" + a,
    ),
    "eli5": (
        "/eli5 <やさしく説明してほしいトピック>",
        lambda a: "次のトピックを、専門用語を避けて、とてもやさしく（小学生にもわかるように）説明してください。"
                  "身近なたとえを使っても構いません。日本語で。\n\n--- トピック ---\n" + a,
    ),
    "proscons": (
        "/proscons <賛否を知りたいトピック>",
        lambda a: "次のトピックについて、メリット（Pros）とデメリット（Cons）を Markdown の表形式で整理してください。"
                  "表は2列（メリット／デメリット）で、それぞれ複数行挙げてください。日本語で。\n\n--- トピック ---\n" + a,
    ),
    "table": (
        "/table <表にしたい内容の説明>",
        lambda a: "次の説明に沿って、適切な列とデータを持つ Markdown の表を作成してください。"
                  "表のあとに、必要なら1〜2文の補足を添えてください。日本語で。\n\n--- 説明 ---\n" + a,
    ),
}

PAGE = None      # set at startup
DRIVER = None
BUSY = False     # single conversation -> serialize requests
AGENT_URL = ""   # bare agent URL (a fresh chat); set at startup


def _wait_composer(timeout=40):
    surfaced = False
    for _ in range(timeout):
        PAGE.wait_for_timeout(1000)
        if PAGE.locator(COPILOT_SELECTORS["composer"]).count() > 0:
            # If we surfaced the hidden Edge for sign-in, auth is now done (the
            # composer rendered) -> drop the window back to the background at once.
            if surfaced:
                try:
                    from relay.edge_recover import rehide
                    rehide()
                except Exception:
                    pass
            return True
        # the dedicated Edge runs hidden in the background -- if a sign-in page shows up,
        # bring it to the foreground once so the user can authenticate. While the login
        # page is still up, refresh the keeper's pause file every ~1s so a slow MFA login
        # is not re-minimized out from under the user (the 180s backoff would else expire).
        try:
            from relay.edge_recover import surface, looks_like_login, touch_pause
            if looks_like_login(PAGE.url):
                if not surfaced:
                    surface()
                    surfaced = True
                touch_pause()
        except Exception:
            pass
    return False


# ── SSO-redirect recovery ───────────────────────────────────────────────────────
# The bridge Edge runs hidden, so an expired / re-challenged SSO cookie silently bounces a
# PAGE.goto onto a landing like  .../chat/?redirfrom=CsrToSSR&auth=2  instead of the requested
# conversation. The composer eventually renders there (it's a bare chat), so _wait_composer is
# satisfied -- but every downstream op then runs against the WRONG surface and fails:
# history scrape comes back empty ("この会話の本文はまだ取得できません") and delete-by-GUID can't
# find the row ("自動削除はできませんでした"). These helpers detect the bounce and re-navigate.
_REDIRECT_MARKERS = ("redirfrom", "csrtossr", "auth=2", "/login", "login.microsoftonline",
                     "login.live", "/oauth")


def _looks_redirected(landed_url, target_url=""):
    u = (landed_url or "").lower()
    if any(m in u for m in _REDIRECT_MARKERS):
        return True
    g = _conv_guid(target_url)            # asked for a specific conversation ...
    if g and g.lower() not in u:          # ... but didn't land on it -> bounced
        return True
    return False


def _goto_settled(url, timeout=25000, tries=3, compose_wait=40):
    """PAGE.goto(url) that recovers from SSO-redirect landings. Returns True once the page is on
    the requested surface (off any redirect/login wall); re-navigates up to `tries` times,
    surfacing the hidden Edge once on a hard login wall so the user can sign in. `compose_wait` is
    how many seconds to wait for the composer each attempt -- keep it SHORT for interactive reads
    (/history) so an unreachable conversation fails fast instead of hanging the single-threaded
    bridge for minutes."""
    surfaced = False
    for _ in range(max(1, tries)):
        try:
            PAGE.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception:
            pass
        _wait_composer(compose_wait)
        if not _looks_redirected(PAGE.url or "", url):
            # Landed on the requested surface. If we had surfaced the hidden Edge for a
            # sign-in wall, auth has completed -> return it to the background at once.
            if surfaced:
                try:
                    from relay.edge_recover import rehide
                    rehide()
                except Exception:
                    pass
            return True
        try:
            from relay.edge_recover import surface, looks_like_login, touch_pause
            if looks_like_login(PAGE.url or ""):
                # Surface once so the user can sign in; keep the keeper backed off while the
                # login page is still showing so a slow MFA login is not re-minimized.
                if not surfaced:
                    surface()
                    surfaced = True
                touch_pause()
        except Exception:
            pass
        PAGE.wait_for_timeout(1500)
    settled = not _looks_redirected(PAGE.url or "", url)
    # If we surfaced but the wall never cleared, still rehide so a failed/abandoned
    # sign-in does not leave the companion Edge stuck in the foreground.
    if surfaced:
        try:
            from relay.edge_recover import rehide
            rehide()
        except Exception:
            pass
    return settled


def _reap_orphan_tabs():
    """Close stray SSO-redirect / login landing tabs that accumulate in the bridge context (a
    failed goto leaves one behind). Never touches the active PAGE or a real /conversation/<guid>
    tab -- only bare redirect/login landings. Mirrors the fleet-side reaper so the hidden bridge
    Edge does not pile up dead tabs over a long session."""
    try:
        ctx = PAGE.context
    except Exception:
        return
    for pg in list(getattr(ctx, "pages", []) or []):
        if pg is PAGE:
            continue
        try:
            u = (pg.url or "").lower()
        except Exception:
            continue
        if any(m in u for m in _REDIRECT_MARKERS) and "/conversation/" not in u:
            try:
                pg.close()
            except Exception:
                pass


# ── agent-rail (the agent's own conversation list) helpers ──────────────────────
# Discovered live (2026-06) on the companion agent page: after expanding the side nav
# (button aria-label="ナビゲーションの展開"/"Expand navigation"), the agent's conversations
# render inside  #m365-copilot-chats-section  as
#   button.fui-NavSubItem[id]   where  id == value == the conversation GUID  and
#                                aria-label == the conversation title.
# Each row sits in a .fui-SplitNavItem that also holds a More button
# (button.fui-SplitNavItem__menuButton, aria-label="More", aria-haspopup="menu").
# The More menu has menuitems 名前の変更 (rename) and 削除 (delete); 削除 opens a confirm
# dialog ("チャットを削除しますか?") with buttons 削除する / キャンセル. These are isolated
# here so a Microsoft DOM change is a localized patch, like COPILOT_SELECTORS.
CHATS_SECTION = "#m365-copilot-chats-section"
NAV_ROW_SEL = CHATS_SECTION + " button.fui-NavSubItem[id]"


def _expand_nav():
    """Best-effort: expand the collapsed side nav so the agent's chat rail renders.
    Harmless if already expanded (the expand button is absent)."""
    for nm in ("ナビゲーションの展開", "Expand navigation"):
        try:
            b = PAGE.get_by_role("button", name=nm)
            if b.count() > 0:
                b.first.click(timeout=3000, force=True)
                PAGE.wait_for_timeout(1500)
                return
        except Exception:
            pass


_RAIL_SCRAPE_JS = r"""
() => {
  var sec = document.getElementById('m365-copilot-chats-section');
  if (!sec) return {ready:false, rows:[]};
  var rows = [].slice.call(sec.querySelectorAll('button.fui-NavSubItem[id]')).map(function(b){
    return {guid: b.id || b.getAttribute('value') || '',
            title: (b.getAttribute('aria-label') || '').replace(/^unread\s+/, '').trim()};
  }).filter(function(r){ return r.guid; });
  return {ready:true, rows:rows};
}
"""


def _scrape_agent_rail(settle=20):
    """On the agent page, expand the nav and scrape the agent's conversation rail.
    Returns a list of {guid, title}. Read-only (deletes nothing)."""
    _expand_nav()
    rows = []
    for _ in range(settle):
        try:
            res = PAGE.evaluate(_RAIL_SCRAPE_JS)
        except Exception:
            res = None
        if res and res.get("ready") and res.get("rows"):
            rows = res["rows"]
            break
        PAGE.wait_for_timeout(700)
    return rows


def _rail_has_guid(guid):
    """True if the rail currently lists a row whose id/value == guid."""
    try:
        return PAGE.evaluate(
            "(g)=>{var s=document.getElementById('m365-copilot-chats-section');"
            "return !!(s && s.querySelector('button.fui-NavSubItem[id=\"'+g+'\"]'));}", guid)
    except Exception:
        return False


def _delete_rail_row(guid):
    """Open the More menu for the rail row with id==guid, click 削除, confirm 削除する.
    Returns (clicked_ok, detail). Assumes the agent rail is already rendered/expanded."""
    # mark the row's own More button (inside its .fui-SplitNavItem) so we can click the
    # REAL element (a forced/synthetic click can open the wrong, global nav menu).
    marked = PAGE.evaluate(
        r"""(guid) => {
            var btn = document.querySelector('button.fui-NavSubItem[id="'+guid+'"]');
            if (!btn) return {ok:false, why:'row not found'};
            try { btn.scrollIntoView({block:'center'}); } catch(e){}
            var wrap = btn.closest('.fui-SplitNavItem') || btn.parentElement;
            var more = wrap ? wrap.querySelector('button[aria-haspopup="menu"], button[aria-label="More"], button.fui-SplitNavItem__menuButton') : null;
            if (!more){ var cur=btn,d=0; while(cur&&d<4&&!more){cur=cur.parentElement; if(cur) more=cur.querySelector('button[aria-haspopup="menu"], button[aria-label="More"]'); d++; } }
            if (!more) return {ok:false, why:'row More button not found'};
            document.querySelectorAll('[data-fleet-more],[data-fleet-row]').forEach(function(e){e.removeAttribute('data-fleet-more');e.removeAttribute('data-fleet-row');});
            btn.setAttribute('data-fleet-row','1');
            more.setAttribute('data-fleet-more','1');
            return {ok:true};
        }""", guid)
    if not marked or not marked.get("ok"):
        return False, (marked or {}).get("why", "row lookup failed")
    # The row's More button is hidden until the ROW is hovered, and the reveal/menu-open
    # is flaky under React. So: hover the row -> click More -> poll for the 削除 menuitem;
    # if the row menu never surfaces, RETRY the whole hover+click a couple of times.
    def _find_delete_mi():
        cand = PAGE.get_by_role("menuitem", name="削除", exact=True)
        if cand.count() == 0:
            cand = PAGE.locator('[role="menuitem"][aria-label="削除"]')
        return cand if cand.count() > 0 else None

    mi = None
    for attempt in range(3):
        try:
            row = PAGE.locator('button[data-fleet-row="1"]').first
            try:
                row.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            try:
                row.hover(timeout=3000)
                PAGE.wait_for_timeout(350)
            except Exception:
                pass
            mb = PAGE.locator('button[data-fleet-more="1"]').first
            # hover the now-revealed More, then click it (force=True as a fallback path
            # if the actionability check blocks a freshly-revealed element)
            try:
                mb.hover(timeout=2500)
                PAGE.wait_for_timeout(200)
                mb.click(timeout=4000)
            except Exception:
                mb.click(timeout=4000, force=True)
        except Exception as e:
            if attempt == 2:
                return False, "More click failed: %s" % type(e).__name__
            PAGE.wait_for_timeout(600)
            continue
        # the row context menu (名前の変更 / 削除) fades in -- POLL for the 削除 menuitem
        for _ in range(8):
            PAGE.wait_for_timeout(350)
            cand = _find_delete_mi()
            if cand is not None:
                mi = cand
                break
        if mi is not None:
            break
        # menu didn't surface (or wrong menu opened) -> dismiss and retry
        try:
            PAGE.keyboard.press("Escape")
        except Exception:
            pass
        PAGE.wait_for_timeout(500)
    if mi is None:
        seen = ""
        try:
            seen = PAGE.evaluate(
                r"""()=>[].slice.call(document.querySelectorAll('[role="menu"]'))
                    .filter(function(m){var r=m.getBoundingClientRect();return r.width>0&&r.height>0;})
                    .map(function(m){return [].slice.call(m.querySelectorAll('[role="menuitem"]'))
                        .map(function(x){return (x.getAttribute('aria-label')||x.innerText||'').slice(0,12);}).join('/');})
                    .join(' || ').slice(0,160)""")
        except Exception:
            pass
        try:
            PAGE.keyboard.press("Escape")
        except Exception:
            pass
        return False, "delete menuitem not found in row menu" + ((" [open menu: %s]" % seen) if seen else "")
    # click the 削除 menuitem (force skips the fade-in stability wait)
    try:
        mi.first.click(timeout=4000, force=True)
    except Exception as e:
        try:
            PAGE.keyboard.press("Escape")
        except Exception:
            pass
        return False, "delete menuitem click failed: %s" % type(e).__name__
    PAGE.wait_for_timeout(1100)
    # confirm "削除する" in the alertdialog (trusted force-click; fall back to JS dispatch)
    try:
        cb = PAGE.get_by_role("button", name="削除する")
        if cb.count() == 0:
            cb = PAGE.locator('[role="alertdialog"] button:has-text("削除する"), [role="dialog"] button:has-text("削除する"), button:has-text("削除する")')
        cb.first.click(timeout=5000, force=True)
        return True, "confirmed"
    except Exception:
        cf = PAGE.evaluate(
            r"""() => { var scope=[].slice.call(document.querySelectorAll('[role="alertdialog"],[role="dialog"]'));
                if(!scope.length) scope=[document.body];
                for(var k=0;k<scope.length;k++){
                    var b=[].slice.call(scope[k].querySelectorAll('button')).find(function(x){
                        var t=((x.innerText||'')+' '+(x.getAttribute('aria-label')||''));
                        return t.indexOf('削除')>=0 && t.indexOf('キャンセル')<0; });
                    if(b){ b.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window})); return true; } }
                return false; }""")
        return (bool(cf), "confirmed (js)" if cf else "confirm button not found")


def _delete_by_guid(url, guid, title=""):
    """GUID-primary delete: navigate to the agent conversation, verify the loaded URL's
    GUID matches the requested GUID (certain linkage), then delete the matching rail row
    and verify the GUID disappeared. Deletes ONLY when the linkage is certain.
    Returns (ok, reason)."""
    # 1) open the conversation directly and confirm we really landed on it. _goto_settled
    #    recovers from an SSO-redirect landing first -- otherwise a hidden-Edge auth bounce reads
    #    as "guid mismatch" and the delete is wrongly abandoned ("自動削除はできませんでした").
    try:
        _goto_settled(url)
        PAGE.wait_for_timeout(800)
    except Exception as e:
        return False, "unreachable: %s: %s" % (type(e).__name__, e)
    landed = _conv_guid(PAGE.url or "")
    if landed != guid:
        # opening it bounced us elsewhere (already gone / new chat) -> do NOT delete
        return False, "guid mismatch/unreachable (landed=%s)" % (landed or "none")
    # 2) the rail row for this guid must be present to act on
    _expand_nav()
    present = False
    for _ in range(16):
        if _rail_has_guid(guid):
            present = True
            break
        PAGE.wait_for_timeout(600)
    if not present:
        return False, "guid mismatch/unreachable (row absent in rail)"
    # 3) delete via the row's More -> 削除 -> 削除する
    clicked, detail = _delete_rail_row(guid)
    if not clicked:
        return False, detail
    # 4) verify by GUID DISAPPEARANCE (not title count): the row must leave the rail
    PAGE.wait_for_timeout(1500)
    ok = False
    for _ in range(18):
        if not _rail_has_guid(guid):
            ok = True
            break
        PAGE.wait_for_timeout(600)
    if not ok:
        # rail can lag -> hard re-check by navigating to the URL: a deleted conversation
        # redirects away (its GUID no longer in the URL)
        try:
            PAGE.goto(url, wait_until="domcontentloaded", timeout=20000)
            _wait_composer()
            PAGE.wait_for_timeout(800)
            if _conv_guid(PAGE.url or "") != guid:
                ok = True
        except Exception:
            pass
    return (ok, "deleted" if ok else "delete may not have applied (guid still present)")


def _try_delete_conversation(url, title=""):
    """Delete the backing Copilot conversation. PRIMARY path: if the URL carries a
    /conversation/<guid>, delete by GUID (certain linkage) off the AGENT rail. Only
    when there is no GUID do we fall back to the legacy title-match on the GENERAL
    /chat history rail (preserved verbatim below).
    The rail rows (captured live 2026-06) are a <div> holding a TITLE button
    (aria-label = the conversation title) and a "More" button (aria-label="More",
    aria-haspopup="menu") -- the rows carry NO id/href, so we match by EXACT title and
    act ONLY when it is UNIQUE (so we never delete a different conversation). Returns
    (ok, reason); on any miss the caller falls back to opening it for manual delete."""
    title = (title or "").strip()
    guid = _conv_guid(url)
    if guid:
        # PRIMARY: certain GUID linkage. Delete by GUID off the agent rail.
        try:
            ok, reason = _delete_by_guid(url, guid, title)
        except Exception as e:
            try:
                PAGE.keyboard.press("Escape")
            except Exception:
                pass
            ok, reason = False, "%s: %s" % (type(e).__name__, str(e))
        _log_delete(guid, title, ok, reason)
        # restore the bridge to a fresh agent chat for the next message
        try:
            if AGENT_URL:
                PAGE.goto(AGENT_URL, wait_until="domcontentloaded", timeout=20000)
                _wait_composer()
        except Exception:
            pass
        return ok, reason
    # FALLBACK (no GUID in URL): legacy title-match on the GENERAL /chat history rail.
    if not title:
        _log_delete("", title, False, "no title to match (history rows carry no conversation id)")
        return False, "no title to match (history rows carry no conversation id)"
    ok, reason = False, "not run"

    def _count(t):
        return PAGE.evaluate(
            "(t)=>[].slice.call(document.querySelectorAll('button[aria-label]'))"
            ".filter(function(b){return b.getAttribute('aria-label')===t;}).length", t)

    try:
        PAGE.goto("https://m365.cloud.microsoft/chat", wait_until="commit", timeout=30000)
        found = 0
        for _ in range(20):                      # let the history rail populate
            PAGE.wait_for_timeout(1000)
            found = _count(title)
            if found:
                break
        if found < 1:
            ok, reason = False, "conversation '%s' not found in history" % title[:30]
        else:
            # open the FIRST matching rail row's More menu via element-dispatch (a
            # coordinate click can miss; the same title may render more than once).
            opened = PAGE.evaluate(
                """(title) => {
                    var tbs = [].slice.call(document.querySelectorAll('button[aria-label]'))
                        .filter(function(b){return b.getAttribute('aria-label')===title;});
                    for (var i=0;i<tbs.length;i++){
                        var row = tbs[i].parentElement;
                        while (row && row.querySelectorAll('button[aria-label="More"]').length===0 && row!==document.body) row = row.parentElement;
                        var more = row ? row.querySelector('button[aria-label="More"]') : null;
                        if (more){ more.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window})); return true; }
                    }
                    return false;
                }""", title)
            if not opened:
                ok, reason = False, "More button not found for the row"
            else:
                PAGE.wait_for_timeout(900)
                # the 削除 menuitem resolves via the accessibility tree (name), not a
                # plain attribute selector; force-click (it fades in).
                mi = PAGE.get_by_role("menuitem", name="削除", exact=True)
                if mi.count() == 0:
                    mi = PAGE.locator('[role="menuitem"][aria-label="削除"]')
                mi_ok = True
                try:
                    mi.first.click(timeout=4000, force=True)
                except Exception:
                    ok, reason, mi_ok = False, "delete menuitem click failed", False
                if mi_ok:
                    PAGE.wait_for_timeout(1200)
                    # confirm "削除する": Playwright force-click first -- it sends a TRUSTED
                    # event (a dispatched MouseEvent has isTrusted=false and React may ignore
                    # it); force skips the stability wait on the fading-in dialog button.
                    cf = False
                    try:
                        cb = PAGE.get_by_role("button", name="削除する")
                        if cb.count() == 0:
                            cb = PAGE.locator('[role="alertdialog"] button:has-text("削除する"), [role="dialog"] button:has-text("削除する"), button:has-text("削除する")')
                        cb.first.click(timeout=5000, force=True)
                        cf = True
                    except Exception:
                        cf = PAGE.evaluate(
                            """() => { var scope = [].slice.call(document.querySelectorAll('[role="alertdialog"],[role="dialog"]'));
                                if (!scope.length) scope=[document.body];
                                for (var k=0;k<scope.length;k++){
                                    var b=[].slice.call(scope[k].querySelectorAll('button')).find(function(x){
                                        var t=((x.innerText||'')+' '+(x.getAttribute('aria-label')||''));
                                        return t.indexOf('削除')>=0 && t.indexOf('キャンセル')<0; });
                                    if(b){ b.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window})); return true; } }
                                return false; }""")
                    if not cf:
                        ok, reason = False, "confirm button not found"
                    else:
                        # the live rail does NOT refresh after a delete -> reload /chat
                        # and re-count to verify the row really went away.
                        PAGE.wait_for_timeout(1500)
                        try:
                            PAGE.reload(wait_until="commit", timeout=20000)
                        except Exception:
                            pass
                        for _ in range(16):
                            PAGE.wait_for_timeout(700)
                            if _count(title) < found:
                                ok = True
                                break
                        reason = "deleted" if ok else "delete may not have applied"
    except Exception as e:
        try:
            PAGE.keyboard.press("Escape")
        except Exception:
            pass
        ok, reason = False, "%s: %s" % (type(e).__name__, str(e))
    _log_delete("", title, ok, reason)
    # restore the bridge to a fresh agent chat so the next message goes to the agent
    try:
        if AGENT_URL:
            PAGE.goto(AGENT_URL, wait_until="domcontentloaded", timeout=20000)
            _wait_composer()
    except Exception:
        pass
    return ok, reason


def _is_proc(t: str) -> bool:
    t = (t or "").strip()
    return (not t) or (any(m in t for m in PROCESSING_MARKERS) and len(t) < 40)


# Search/tool-status lines that appear in [data-testid="loading-message"] while Copilot
# is running a web search.  We suppress them entirely (no length cap) so they are never
# streamed as a prefix of the real answer.
_SEARCH_STATUS_MARKERS = (
    "を検索しています",
    "を検索中",
    "検索しています",
    "Searching",
    "調べています",
    "情報を探しています",
    "処理しています…",
    "Working on",
    "考えています",
    # generic loading / "please wait" placeholders
    "しばらくお待ちください",
    "お待ちください",
    "少々お待ち",
    "Please wait",
    "Just a moment",
    "One moment",
    "Generating",
    "Loading",
)


def _is_search_status(t: str) -> bool:
    """Return True when `t` is a loading/search-status line (no length cap)."""
    t = (t or "").strip()
    return bool(t) and any(m in t for m in _SEARCH_STATUS_MARKERS)


def _text(sel: str) -> str:
    try:
        loc = PAGE.locator(sel)
        return (loc.last.inner_text() or "").strip() if loc.count() else ""
    except Exception:
        return ""


# JS run in the page to extract the LAST assistant answer cleanly:
#  - Selects the last [data-testid="markdown-reply"]; falls back to lastChatMessage.
#  - Deep-clones the node and strips all chrome (citations, suggestions, feedback buttons).
#  - Replaces each .monaco-editor with a fenced code block extracted from Monaco models
#    or, as a best-effort fallback, from the visible .view-lines (Monaco virtualizes long
#    blocks, so only visible lines may be returned for very long code).
#  - Returns clone.innerText trimmed, or "" on any error (never throws into Python).
_CLEAN_ANSWER_JS = r"""
() => {
  try {
    // --- 1. Find the answer node ---
    var answerNode = null;
    var mrList = document.querySelectorAll('[data-testid="markdown-reply"]');
    if (mrList.length > 0) {
      answerNode = mrList[mrList.length - 1];
    } else {
      var lcList = document.querySelectorAll('[data-testid="lastChatMessage"]');
      if (lcList.length > 0) answerNode = lcList[lcList.length - 1];
    }
    if (!answerNode) return "";

    // --- 2. Extract Monaco code blocks from the ORIGINAL node (before cloning) ---
    var monacoEditors = answerNode.querySelectorAll('.monaco-editor');
    var monacoTexts = [];
    for (var mi = 0; mi < monacoEditors.length; mi++) {
      var code = "";
      try {
        if (window.monaco && window.monaco.editor && window.monaco.editor.getModels) {
          var models = window.monaco.editor.getModels();
          if (mi < models.length) {
            try { code = models[mi].getValue() || ""; } catch(e2) { code = ""; }
          }
        }
      } catch(e) {}
      if (!code) {
        // Fallback: read .view-lines (best-effort; only visible lines for long blocks)
        try {
          var vl = monacoEditors[mi].querySelectorAll('.view-line');
          var lines = [];
          for (var li = 0; li < vl.length; li++) {
            lines.push(vl[li].textContent || "");
          }
          code = lines.join("\n");
        } catch(e) { code = ""; }
      }
      monacoTexts.push(code || "[code]");
    }

    // --- 3. Deep-clone so we don't mutate the live page ---
    var clone = answerNode.cloneNode(true);

    // --- 4. Remove chrome from the clone ---
    var removeSelectors = [
      '[data-testid="foot-note-div"]',
      '[data-testid="sources-button-testid"]',
      '[data-testid="web-search-info-icon"]',
      '[data-testid="messageAttributionIcon"]',
      '[data-testid="chat-suggestion"]',
      '[data-testid="chat-response-message-disclaimer"]',
      '[data-testid="CopyButtonContainerTestId"]',
      '[data-testid="feedback-button-testid"]',
      '[data-testid="FeedbackContainerTestId"]',
      '[data-testid="overflow-menu-button"]',
      '[data-testid*="citation" i]',
      '[data-testid*="reference" i]',
      '[data-testid*="source" i]',
      '[data-testid*="attribution" i]',
      'sup',
      'cite',
      'button',
      '[role="button"]'
    ];
    // Class-based selectors (case-insensitive substring matching via attribute selector).
    // These target citation/source/footnote chrome ONLY -- code is rendered in Monaco or in
    // "scriptor-textRun" spans, neither of which match these, so code is never removed.
    var classRemoveSelectors = [
      '[class*="citation" i]',
      '[class*="cite-" i]',
      '[class*="foot-note" i]',
      '[class*="footnote" i]',
      '[class*="reference" i]',
      '[class*="source" i]',
      '[aria-label*="citation" i]',
      '[aria-label*="reference" i]',
      '[aria-label*="source" i]'
    ];
    var allRemove = removeSelectors.concat(classRemoveSelectors);
    for (var si = 0; si < allRemove.length; si++) {
      try {
        var toRemove = clone.querySelectorAll(allRemove[si]);
        for (var ri = 0; ri < toRemove.length; ri++) {
          try { toRemove[ri].parentNode.removeChild(toRemove[ri]); } catch(e) {}
        }
      } catch(e) {}
    }

    // --- 5. Replace .monaco-editor placeholders in the clone with fenced text nodes ---
    var cloneMonacos = clone.querySelectorAll('.monaco-editor');
    for (var ci = 0; ci < cloneMonacos.length; ci++) {
      var codeText = (ci < monacoTexts.length) ? monacoTexts[ci] : "[code]";
      var fenced = "\n```\n" + codeText + "\n```\n";
      try {
        var textNode = document.createTextNode(fenced);
        cloneMonacos[ci].parentNode.replaceChild(textNode, cloneMonacos[ci]);
      } catch(e) {}
    }

    // --- 6. Return trimmed innerText ---
    return (clone.innerText || "").trim();
  } catch(e) {
    return "";
  }
}
"""


def _clean_answer_text() -> "str | None":
    """Extract the last assistant answer with citations/chrome stripped and Monaco
    code blocks inlined as fenced blocks.  Returns the cleaned string, or None if
    extraction failed or yielded empty (caller falls back to _text(LASTMSG))."""
    try:
        result = PAGE.evaluate(_CLEAN_ANSWER_JS)
        if result and isinstance(result, str) and result.strip():
            return result.strip()
        return None
    except Exception:
        return None


# JS run inside the page to scrape every turn in DOM order with its author role.
# Discovered from the live M365 Copilot DOM: each turn is a
# [data-testid="m365-chat-llm-web-ui-chat-message"] block containing the user
# bubble [data-testid="chatQuestion"] (text prefixed "You said: ") and the
# assistant bubble .fai-CopilotMessage (text prefixed "<agent> said: <agent>").
# Returns an ordered array of {role, text}; tolerates a turn that is missing
# either bubble (role inferred from which selector matched).
_SCRAPE_JS = r"""
() => {
  function clean(s){ return (s||'').replace(/​/g,'').replace(/‌/g,'').trim(); }
  function stripPrefix(s){
    // drop a leading "<author> said:" prefix, then a duplicated author line/word
    s = clean(s);
    var idx = s.indexOf(' said:');
    var author = '';
    if (idx !== -1 && idx < 80){ author = s.slice(0, idx).trim(); s = s.slice(idx + 6); }
    var lines = s.split('\n');
    while (lines.length && !lines[0].trim()) lines.shift();
    // assistant innerText is "<author> said: <author>\n<body>" -> the body's
    // first line repeats the author; drop it when it equals the captured author
    if (lines.length && author && lines[0].trim() === author) lines.shift();
    else if (lines.length >= 2 && lines[0].trim() && lines[0].trim() === lines[1].trim()) lines.shift();
    return lines.join('\n').trim();
  }
  function stripUser(s){
    s = clean(s);
    var idx = s.indexOf(' said:');           // "You said: ..." / "<name> said: ..."
    if (idx !== -1 && idx < 80) s = s.slice(idx + 6);
    return clean(s);
  }
  var out = [];
  try {
    var turns = document.querySelectorAll('[data-testid="m365-chat-llm-web-ui-chat-message"]');
    turns.forEach(function(turn){
      var q = turn.querySelector('[data-testid="chatQuestion"]');
      if (q){ var ut = stripUser(q.innerText); if (ut) out.push({role:'user', text:ut}); }
      var a = turn.querySelector('.fai-CopilotMessage')
           || turn.querySelector('[data-testid="copilot-message-reply-div"]')
           || turn.querySelector('[data-testid="copilot-message-div"]');
      if (a){ var at = stripPrefix(a.innerText); if (at) out.push({role:'assistant', text:at}); }
    });
    if (out.length === 0){
      // fallback: assistant bubbles only, in DOM order
      document.querySelectorAll('.fai-CopilotMessage').forEach(function(a){
        var at = stripPrefix(a.innerText); if (at) out.push({role:'assistant', text:at});
      });
    }
  } catch (e) { return {__error: String(e)}; }
  return out;
}
"""


# ── Scroll-and-accumulate: full transcript even for virtualized long conversations ──────────
#
# Microsoft Copilot's chat is a React SPA that VIRTUALIZES message rendering: for long
# conversations only the messages near the current scroll position are in the DOM; messages
# scrolled out of view are unmounted. A single querySelectorAll snapshot (the old _SCRAPE_JS
# path) therefore misses the bulk of a long transcript. scrape_full_transcript() scrolls the
# conversation container from top to bottom, collecting messages at each position into a
# deduplicating accumulator, until convergence or a safety bound is hit.
#
# Stable-key strategy: each turn block carries a position index in the DOM's rendered order.
# We use a SHA-1 of (role + first-80-chars of text) as the stable de-dup key -- this is a
# best-effort heuristic since M365 Copilot's chat DOM exposes NO per-message GUID or
# data-id attribute on turn containers (confirmed from live DOM, 2026-06). The hash is
# computed in Python on the text returned from the page, so the pure accumulate() function
# below is fully testable without a browser.

# JS that returns ONE scroll step's worth of info: the scroll container's geometry and
# all turn blocks currently visible in the DOM. The scroll position is set BY THE CALLER
# (Python-side, via a separate evaluate call) so the JS itself stays pure/stateless.
_SCROLL_STEP_JS = r"""
() => {
  // --- find the scroll container ---
  // Strategy: look for the first ancestor of a turn block whose scrollHeight > clientHeight.
  // Fall back to the body if nothing found.
  var TURN_SEL = '[data-testid="m365-chat-llm-web-ui-chat-message"]';
  var container = null;
  var firstTurn = document.querySelector(TURN_SEL);
  if (firstTurn) {
    var el = firstTurn.parentElement;
    while (el && el !== document.body) {
      if (el.scrollHeight > el.clientHeight + 2) { container = el; break; }
      el = el.parentElement;
    }
  }
  if (!container) container = document.body;

  // --- geometry ---
  var scrollTop = container.scrollTop || 0;
  var scrollHeight = container.scrollHeight || 0;
  var clientHeight = container.clientHeight || 0;

  // --- helper text cleaners (mirrors _SCRAPE_JS) ---
  function clean(s){ return (s||'').replace(/​/g,'').replace(/‌/g,'').trim(); }
  function stripPrefix(s){
    s = clean(s);
    var idx = s.indexOf(' said:');
    var author = '';
    if (idx !== -1 && idx < 80){ author = s.slice(0, idx).trim(); s = s.slice(idx + 6); }
    var lines = s.split('\n');
    while (lines.length && !lines[0].trim()) lines.shift();
    if (lines.length && author && lines[0].trim() === author) lines.shift();
    else if (lines.length >= 2 && lines[0].trim() && lines[0].trim() === lines[1].trim()) lines.shift();
    return lines.join('\n').trim();
  }
  function stripUser(s){
    s = clean(s);
    var idx = s.indexOf(' said:');
    if (idx !== -1 && idx < 80) s = s.slice(idx + 6);
    return clean(s);
  }

  // --- collect all turns currently in the DOM ---
  var msgs = [];
  var turns = document.querySelectorAll(TURN_SEL);
  turns.forEach(function(turn, idx){
    var q = turn.querySelector('[data-testid="chatQuestion"]');
    if (q){ var ut = stripUser(q.innerText); if (ut) msgs.push({role:'user', text:ut, dom_idx:idx*2}); }
    var a = turn.querySelector('.fai-CopilotMessage')
         || turn.querySelector('[data-testid="copilot-message-reply-div"]')
         || turn.querySelector('[data-testid="copilot-message-div"]');
    if (a){ var at = stripPrefix(a.innerText); if (at) msgs.push({role:'assistant', text:at, dom_idx:idx*2+1}); }
  });
  // assistant-only fallback (matches _SCRAPE_JS)
  if (msgs.length === 0) {
    document.querySelectorAll('.fai-CopilotMessage').forEach(function(a, idx){
      var at = stripPrefix(a.innerText);
      if (at) msgs.push({role:'assistant', text:at, dom_idx:idx});
    });
  }

  return {
    scrollTop: scrollTop,
    scrollHeight: scrollHeight,
    clientHeight: clientHeight,
    atBottom: (scrollTop + clientHeight + 4) >= scrollHeight,
    msgs: msgs
  };
}
"""

# JS that scrolls the container to a given scrollTop (0 = very top).
_SCROLL_TO_JS = r"""
(scrollTop) => {
  var TURN_SEL = '[data-testid="m365-chat-llm-web-ui-chat-message"]';
  var container = null;
  var firstTurn = document.querySelector(TURN_SEL);
  if (firstTurn) {
    var el = firstTurn.parentElement;
    while (el && el !== document.body) {
      if (el.scrollHeight > el.clientHeight + 2) { container = el; break; }
      el = el.parentElement;
    }
  }
  if (!container) container = document.body;
  container.scrollTop = scrollTop;
  return container.scrollTop;
}
"""

# ── Pure accumulate/converge logic (no browser dependency -- fully unit-testable) ──────────

def _msg_key(role: str, text: str) -> str:
    """Stable dedup key: SHA-1 of role + first 80 chars of message text.

    We use the first 80 chars rather than the full text so a message that was captured
    mid-stream (truncated) and then re-captured complete still deduplicates correctly
    (the first 80 chars are almost always settled before the rest of the text renders).
    Role is included so a user and assistant message with the same opening line are not
    collapsed."""
    snippet = (role + ":" + (text or "")[:80]).encode("utf-8")
    return hashlib.sha1(snippet).hexdigest()


def accumulate_messages(
    pages: "list[list[dict]]",
    max_steps: int = 400,
    no_progress_limit: int = 5,
) -> "tuple[list[dict], bool]":
    """Pure accumulate-and-dedupe logic for scroll-collected message windows.

    This function is the heart of the virtualisation-aware scraper; it contains NO
    browser calls and is fully unit-testable.

    Args:
        pages: A list of "windows" -- each window is the list of message dicts
            ({role, text, dom_idx}) visible at one scroll position. The caller
            feeds windows in scroll order (top -> bottom).
        max_steps: Safety bound on the number of pages processed (NOT scroll steps;
            the caller controls how many scroll steps map to how many page samples).
        no_progress_limit: Stop early if this many consecutive pages yield zero new keys.

    Returns:
        (messages, truncated)
        - messages: de-duplicated list in first-seen order (approximates top-to-bottom
          order because pages arrive from top to bottom; within a page dom_idx breaks ties).
        - truncated: True iff a bound (max_steps or no_progress_limit) was reached before
          the caller signalled convergence. The caller also sets this True on wall-clock
          timeout. A truncated transcript is NOT silently claimed as complete.
    """
    # acc maps stable_key -> (first_seen_page_index, dom_idx_at_first_seen, msg_dict)
    acc: "dict[str, tuple[int, int, dict]]" = {}
    no_progress = 0
    truncated = False

    for step_idx, window in enumerate(pages):
        if step_idx >= max_steps:
            truncated = True
            break
        new_this_step = 0
        for msg in (window or []):
            role = (msg.get("role") or "assistant").strip()
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            key = _msg_key(role, text)
            if key not in acc:
                dom_idx = int(msg.get("dom_idx", 0))
                acc[key] = (step_idx, dom_idx, {"role": role, "text": text})
                new_this_step += 1
        if new_this_step == 0:
            no_progress += 1
            if no_progress >= no_progress_limit:
                break   # converged (or stuck) -- stop early
        else:
            no_progress = 0

    # Reconstruct order: sort by (first_seen_page_index, dom_idx_at_first_seen).
    # For a top-to-bottom scroll pass this restores reading order reliably; messages
    # that were visible at multiple positions keep their FIRST-SEEN position.
    ordered = sorted(acc.values(), key=lambda t: (t[0], t[1]))
    messages = [entry[2] for entry in ordered]
    return messages, truncated


# ── CDP-driving scroll wrapper (browser-dependent; falls back gracefully) ───────────────────

# Bounds for the scroll pass
_SCROLL_MAX_STEPS = 400          # hard cap on scroll increments
_SCROLL_WALL_TIMEOUT_S = 45.0   # overall wall-clock limit for the whole scroll pass
_SCROLL_NO_PROGRESS = 5         # consecutive steps with 0 new msgs -> converged
_SCROLL_SETTLE_MS = 300          # ms to wait after each scroll for React to unmount/mount


def scrape_full_transcript(page) -> "tuple[list[dict], bool]":
    """Scrape ALL messages from the current conversation by scrolling top-to-bottom.

    Works around Microsoft Copilot's virtualised message rendering: only messages near
    the viewport are in the DOM, so a single querySelectorAll snapshot misses everything
    outside the current view. This function scrolls from the very top to the very bottom
    in increments, collecting messages at each position and deduplicating by a stable key.

    Args:
        page: A Playwright page object (or compatible mock with .evaluate()).

    Returns:
        (messages, truncated) where `messages` is a list of {role, text} dicts in
        reading order and `truncated` is True if a bound was hit before full convergence.
        On any unrecoverable error returns ([], False) -- caller falls back to the old
        single-snapshot path.

    The caller (_scrape_history) uses the result if it's non-empty; if it IS empty it
    falls back to the old single-snapshot _SCRAPE_JS path, so this function can never
    make things worse than the current state.
    """
    t0 = time.time()
    pages: "list[list[dict]]" = []
    truncated = False

    try:
        # 1. Scroll to the very top first so we start from message 0.
        try:
            page.evaluate(_SCROLL_TO_JS, 0)
            page.wait_for_timeout(_SCROLL_SETTLE_MS)
        except Exception as e:
            logger.debug("scrape_full_transcript: scroll-to-top failed: %s", e)
            return [], False

        # 2. Grab an initial reading to learn scrollHeight and clientHeight.
        try:
            info = page.evaluate(_SCROLL_STEP_JS)
        except Exception as e:
            logger.debug("scrape_full_transcript: initial evaluate failed: %s", e)
            return [], False

        if not isinstance(info, dict):
            return [], False

        scroll_height = int(info.get("scrollHeight") or 0)
        client_height = int(info.get("clientHeight") or 1)
        pages.append(info.get("msgs") or [])

        if scroll_height <= client_height + 4:
            # Short conversation -- everything already in view; no scrolling needed.
            msgs, trunc = accumulate_messages(pages,
                                             max_steps=_SCROLL_MAX_STEPS,
                                             no_progress_limit=_SCROLL_NO_PROGRESS)
            return msgs, trunc

        # 3. Scroll down in increments of 80% of the client height.
        step = max(1, int(client_height * 0.8))
        current_top = 0
        no_progress_steps = 0
        prev_key_count = 0

        for _ in range(_SCROLL_MAX_STEPS):
            # Wall-clock safety valve
            if time.time() - t0 > _SCROLL_WALL_TIMEOUT_S:
                truncated = True
                logger.info("scrape_full_transcript: wall-clock timeout after %.1fs, "
                            "captured %d windows so far", time.time() - t0, len(pages))
                break

            current_top = min(current_top + step, scroll_height - client_height)
            try:
                page.evaluate(_SCROLL_TO_JS, current_top)
                page.wait_for_timeout(_SCROLL_SETTLE_MS)
                info = page.evaluate(_SCROLL_STEP_JS)
            except Exception as e:
                logger.debug("scrape_full_transcript: scroll step failed: %s", e)
                truncated = True
                break

            if not isinstance(info, dict):
                truncated = True
                break

            pages.append(info.get("msgs") or [])

            # Count unique keys so far for no-progress detection
            # (accumulate_messages handles this internally, but we need an early-out check)
            current_key_count = sum(len(w) for w in pages)
            if current_key_count == prev_key_count:
                no_progress_steps += 1
                if no_progress_steps >= _SCROLL_NO_PROGRESS:
                    break  # converged early
            else:
                no_progress_steps = 0
            prev_key_count = current_key_count

            if info.get("atBottom"):
                break  # reached the end of the scroll container

        # Scroll back to the bottom so the user sees the latest message on return.
        try:
            page.evaluate(_SCROLL_TO_JS, scroll_height)
        except Exception:
            pass

    except Exception as e:
        logger.warning("scrape_full_transcript: unexpected error: %s", e)
        return [], False

    msgs, acc_trunc = accumulate_messages(pages,
                                          max_steps=_SCROLL_MAX_STEPS,
                                          no_progress_limit=_SCROLL_NO_PROGRESS)
    return msgs, (truncated or acc_trunc)


TURN_SEL = '[data-testid="m365-chat-llm-web-ui-chat-message"]'


def _wait_turns(timeout=30):
    """After navigating to a conversation, the turn blocks AND the assistant reply
    bubbles inside them render a few seconds AFTER the composer appears (the user
    bubble paints first, the .fai-CopilotMessage a beat later). Poll until at least
    one turn block exists AND it carries an assistant bubble, so the scrape never
    races ahead and captures only the user turns. Then settle briefly. If turns
    exist but no assistant bubble ever appears (a conversation with a pending/only
    user turn), proceed anyway after the timeout."""
    saw_turn = False
    for _ in range(timeout):
        try:
            turns = PAGE.locator(TURN_SEL).count()
            if turns > 0:
                saw_turn = True
                if PAGE.locator(".fai-CopilotMessage").count() > 0:
                    PAGE.wait_for_timeout(900)   # let the last bubble's text settle
                    return True
        except Exception:
            pass
        PAGE.wait_for_timeout(1000)
    return saw_turn


def _scrape_history():
    """Scrape all messages of the currently loaded conversation, in order.

    PRIMARY path: scroll-and-accumulate (scrape_full_transcript) which handles
    Microsoft Copilot's virtualised message rendering by scrolling the conversation
    container from top to bottom, collecting messages at each position, and
    deduplicating by a stable key.  If the primary path returns nothing (e.g. the
    scroll container selector failed to find the container, or a very short
    conversation was already in view), we fall back to the old single-snapshot
    _SCRAPE_JS path so this function never regresses for short conversations.

    A `truncated=True` result is surfaced in the return value's metadata key so
    the /history endpoint can report it to the caller -- we never silently claim
    full capture when we hit a bound.

    Returns a list of {"role": "...", "text": "..."}, optionally with a final
    sentinel {"role": "__meta__", "truncated": True, "captured": N} appended when
    the scroll pass hit a safety bound before full convergence."""
    _wait_turns()

    # PRIMARY: scroll-and-accumulate
    try:
        full_msgs, truncated = scrape_full_transcript(PAGE)
    except Exception as e:
        logger.warning("_scrape_history: scrape_full_transcript raised: %s", e)
        full_msgs, truncated = [], False

    if full_msgs:
        out = []
        for m in full_msgs:
            try:
                role = (m.get("role") or "").strip() or "assistant"
                text = (m.get("text") or "").strip()
            except Exception:
                continue
            if text and role != "__meta__":
                out.append({"role": role, "text": text})
        if out:
            if truncated:
                logger.info("_scrape_history: truncated=True, captured %d messages", len(out))
                out.append({"role": "__meta__", "truncated": True, "captured": len(out)})
            return out

    # FALLBACK: single-snapshot (no scrolling; the old behaviour)
    logger.debug("_scrape_history: falling back to single-snapshot path")
    res = PAGE.evaluate(_SCRAPE_JS)
    if isinstance(res, dict) and res.get("__error"):
        raise RuntimeError("scrape failed: " + str(res.get("__error")))
    out = []
    for m in (res or []):
        try:
            role = (m.get("role") or "").strip() or "assistant"
            text = (m.get("text") or "").strip()
        except Exception:
            continue
        if text:
            out.append({"role": role, "text": text})
    return out


PAGE_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Copilot (local bridge)</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;background:#1a1a1a;color:#e8e8e8;font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif}
  header{padding:12px 18px;border-bottom:1px solid #333;font-weight:600;color:#c9a36a}
  #log{max-width:820px;margin:0 auto;padding:18px}
  .msg{margin:14px 0;display:flex;gap:10px}
  .who{flex:0 0 64px;color:#888;font-size:13px;padding-top:2px}
  .body{white-space:pre-wrap;word-break:break-word;flex:1}
  .user .body{color:#9ecbff}
  .bar{position:sticky;bottom:0;background:#1a1a1a;border-top:1px solid #333;padding:12px}
  form{max-width:820px;margin:0 auto;display:flex;gap:8px}
  textarea{flex:1;resize:none;background:#252525;color:#e8e8e8;border:1px solid #3a3a3a;border-radius:8px;padding:10px;font:inherit}
  button{background:#c9a36a;color:#1a1a1a;border:0;border-radius:8px;padding:0 18px;font-weight:600;cursor:pointer}
  button:disabled{opacity:.5;cursor:default}
  .cursor::after{content:"\\25ae";color:#c9a36a;animation:b 1s steps(1) infinite}
  @keyframes b{50%{opacity:0}}
</style></head><body>
<header>● Copilot — local bridge (Python + Edge, no Node)</header>
<div id="log"></div>
<div class="bar"><form id="f">
  <textarea id="q" rows="2" placeholder="メッセージを入力 (Enter で送信)"></textarea>
  <button id="send" type="submit">送信</button>
</form></div>
<script>
const log=document.getElementById('log'),q=document.getElementById('q'),btn=document.getElementById('send'),f=document.getElementById('f');
function add(who,cls){const m=document.createElement('div');m.className='msg '+cls;
  const w=document.createElement('div');w.className='who';w.textContent=who;
  const b=document.createElement('div');b.className='body';m.append(w,b);log.append(m);
  window.scrollTo(0,document.body.scrollHeight);return b;}
function ask(text){
  add('You','user').textContent=text;
  const out=add('Copilot','asst');out.classList.add('cursor');
  btn.disabled=true;
  const es=new EventSource('/stream?msg='+encodeURIComponent(text));
  es.onmessage=e=>{const d=JSON.parse(e.data);
    if(d.replace!==undefined){out.textContent=d.replace;window.scrollTo(0,document.body.scrollHeight);}
    else if(d.delta){out.textContent+=d.delta;window.scrollTo(0,document.body.scrollHeight);}};
  es.addEventListener('done',()=>{out.classList.remove('cursor');es.close();btn.disabled=false;q.focus();});
  es.onerror=()=>{out.classList.remove('cursor');es.close();btn.disabled=false;};
}
f.onsubmit=e=>{e.preventDefault();const t=q.value.trim();if(!t)return;q.value='';ask(t);};
q.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();f.requestSubmit();}});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        global BUSY
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            body = PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/stream":
            qs = urllib.parse.parse_qs(parsed.query)
            msg = (qs.get("msg") or [""])[0]
            self._stream(msg)
            return
        if parsed.path == "/new":          # start a fresh Copilot conversation
            global BUSY
            if BUSY:
                self._json({"ok": False, "error": "busy"}); return
            ok = False
            try:
                if AGENT_URL:
                    PAGE.goto(AGENT_URL, wait_until="domcontentloaded")
                    ok = _wait_composer()
            except Exception as e:
                self._json({"ok": False, "error": str(e)}); return
            self._json({"ok": ok, "url": PAGE.url})
            return
        if parsed.path == "/conv":         # current conversation URL (for saving)
            self._json({"url": PAGE.url})
            return
        if parsed.path == "/switch":       # continue a saved conversation
            if BUSY:
                self._json({"ok": False, "error": "busy"}); return
            url = (urllib.parse.parse_qs(parsed.query).get("url") or [""])[0]
            ok = False
            try:
                _reap_orphan_tabs()
                if url:
                    ok = _goto_settled(url)     # recover from SSO-redirect landings
            except Exception as e:
                self._json({"ok": False, "error": str(e)}); return
            self._json({"ok": ok, "url": PAGE.url})
            return
        if parsed.path == "/history":      # scrape ALL turns of a conversation in order
            if BUSY:
                self._json({"ok": False, "error": "busy"}); return
            url = (urllib.parse.parse_qs(parsed.query).get("url") or [""])[0]
            try:
                _reap_orphan_tabs()
                if url:
                    # bounded for an interactive READ: ~10s composer wait, 2 tries -> an unreachable
                    # conversation returns empty in ~25s instead of hanging the bridge for minutes.
                    _goto_settled(url, timeout=12000, tries=2, compose_wait=10)
                messages = _scrape_history()
                # A cold URL navigation sometimes lands on an un-hydrated conversation view
                # (the SPA shows the composer but never renders the prior turns), so the
                # first scrape comes back empty. Reload once and re-scrape before giving up.
                if url and not messages:
                    try:
                        PAGE.reload(wait_until="domcontentloaded")
                        _wait_composer()
                        PAGE.wait_for_timeout(1500)
                        messages = _scrape_history()
                    except Exception:
                        pass
            except Exception as e:
                self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}); return
            # Surface the truncation sentinel as a top-level field so callers can act on it
            # without having to scan the message list. The sentinel record itself is stripped
            # from the messages array (it is a meta record, not a real message).
            truncated_meta = None
            clean_messages = []
            for m in (messages or []):
                if m.get("role") == "__meta__":
                    truncated_meta = m
                else:
                    clean_messages.append(m)
            resp = {"ok": True, "url": PAGE.url, "messages": clean_messages}
            if truncated_meta:
                resp["truncated"] = True
                resp["captured"] = truncated_meta.get("captured", len(clean_messages))
            self._json(resp)
            return
        if parsed.path == "/delete":       # best-effort: delete the Copilot conversation
            if BUSY:
                self._json({"ok": False, "error": "busy"}); return
            q = urllib.parse.parse_qs(parsed.query)
            url = (q.get("url") or [""])[0]
            title = (q.get("title") or [""])[0]
            try:
                ok, reason = _try_delete_conversation(url, title)
            except Exception as e:
                ok, reason = False, str(e)
            # expose the reason under BOTH keys so the UI can surface it (it was dropped before)
            self._json({"ok": ok, "error": reason, "reason": reason, "guid": _conv_guid(url)})
            return
        if parsed.path == "/agent_conversations":
            # READ-ONLY: scrape the agent's own conversation rail (guid + title). Lists
            # orphans not in the local registry. Deletes nothing. Scope = current agent.
            if BUSY:
                self._json({"ok": False, "error": "busy"}); return
            try:
                if AGENT_URL:
                    PAGE.goto(AGENT_URL, wait_until="domcontentloaded", timeout=25000)
                    _wait_composer()
                rows = _scrape_agent_rail()
                base = AGENT_URL or ((PAGE.url or "").split("/conversation/")[0])
                convs = [{"guid": r["guid"], "title": r.get("title", ""),
                          "url": (base.rstrip("/") + "/conversation/" + r["guid"]) if base else ""}
                         for r in rows]
            except Exception as e:
                self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}); return
            self._json({"ok": True, "count": len(convs), "conversations": convs})
            return
        if parsed.path == "/upload":       # attach a local file/image to the composer
            path = (urllib.parse.parse_qs(parsed.query).get("path") or [""])[0]
            try:
                if not path or not os.path.isfile(path):
                    self._json({"ok": False, "error": "file not found"}); return
                inp = PAGE.locator('input[type="file"][accept*="csv"]').first
                if inp.count() == 0:
                    inp = PAGE.locator('input[type="file"]').first
                inp.set_input_files(path)
                PAGE.wait_for_timeout(2200)     # let the attachment chip register
                self._json({"ok": True, "name": os.path.basename(path)})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
            return
        self.send_response(404)
        self.end_headers()

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, data: dict, event: str | None = None):
        chunk = (f"event: {event}\n" if event else "") + f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    def _ping(self):
        # SSE comment line -- EventSource IGNORES it on the client, but the write RAISES the moment
        # the client hangs up (the user pressed Esc/Stop), so we detect the disconnect within one
        # tick even while Copilot is silently "thinking" and no delta is flowing.
        self.wfile.write(b": ping\n\n")
        self.wfile.flush()

    def _command(self, cmd):
        head = cmd.split(None, 1)[0].lower()
        arg = cmd[len(cmd.split(None, 1)[0]):].strip()
        # normalise: case-insensitive, tolerate a missing leading slash
        token = head.lstrip("/")
        if token in ("help", "?", "commands"):
            self._sse({"delta": HELP_TEXT}); self._sse({}, "done"); return
        if token in ("research", "deepresearch", "dr"):
            self._delegate("researcher", arg); return
        if token in ("analyze", "an"):
            self._delegate("analyst", arg); return
        if token in PROMPT_TEMPLATES:           # prompt-template -> normal streaming path
            usage, build = PROMPT_TEMPLATES[token]
            if not arg.strip():
                self._sse({"delta": "使い方: " + usage}); self._sse({}, "done"); return
            self._stream_text(build(arg.strip()))
            return
        self._sse({"delta": "未知のコマンド `" + head + "`。`/help` で一覧を表示します。"})
        self._sse({}, "done")

    def _delegate(self, kind, arg):
        """Run a /research or /analyze command by delegating to the Researcher
        (Claude) or Analyst agent on a side page; stream the report back."""
        global BUSY
        if not arg:
            usage = "/research <調べたいこと>" if kind == "researcher" else "/analyze <絶対パス> | <分析指示>"
            self._sse({"delta": "使い方: " + usage}); self._sse({}, "done"); return
        if BUSY:
            self._sse({"delta": "[busy: 直前の処理を実行中です]"}); self._sse({}, "done"); return
        BUSY = True
        page = None
        try:
            from relay.agent_profiles import ANALYST, RESEARCHER, analyze, ask_agent
            page = PAGE.context.new_page()
            if kind == "researcher":
                self._sse({"delta": "🔬 Claude で deep research を実行中…（確認→承認→本実行、数分かかります）\n\n"})
                res = ask_agent(page, arg, RESEARCHER, model_name="Claude")
            else:
                if "|" in arg:
                    path, instr = arg.split("|", 1); path = path.strip(); instr = instr.strip()
                else:
                    path, instr = arg.strip(), "添付データを分析し、要点を短くまとめてください。"
                self._sse({"delta": "📊 アナリストで分析中…（" + path + "）\n\n"})
                res = analyze(page, path, instr, ANALYST)
            if res.get("ok"):
                self._sse({"delta": res.get("result", "") or "(空の結果)"})
            else:
                self._sse({"delta": "[失敗: " + str(res.get("error", "")) + "]"})
            self._sse({}, "done")
        except Exception as e:
            try:
                self._sse({"delta": "[command error: " + type(e).__name__ + ": " + str(e) + "]"})
                self._sse({}, "done")
            except Exception:
                pass
        finally:
            try:
                if page is not None:
                    page.close()
            except Exception:
                pass
            BUSY = False

    def _stream(self, msg: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        if not msg.strip():
            self._sse({}, "done")
            return
        if msg.strip().startswith("/"):     # slash commands (/research, /analyze, /help)
            self._command(msg.strip())
            return
        # PLAIN main-chat message: prepend BRIDGE_DISCIPLINE -- it suppresses the impl agent's
        # advisor/lecturer/ego persona (the leak the user hit on main-chat turns) WITHOUT the
        # full discipline's "answer concisely and stop" clause, which was halting autonomous
        # multi-step tasks after the first tool result. Slash / prompt-template commands keep
        # their own framing and are NOT wrapped.
        self._stream_text(BRIDGE_DISCIPLINE + msg)

    def _stream_text(self, msg: str):
        """Send `msg` to the agent and stream the answer back over the ALREADY-open
        SSE response (the normal send/stream path). Used both for plain messages and
        for prompt-template slash commands so templated answers stream like a normal
        turn. Respects the BUSY guard; always emits a terminating `done` event."""
        global BUSY
        if BUSY:
            self._sse({"delta": "[busy: 直前の応答を生成中です]"})
            self._sse({}, "done")
            return
        BUSY = True
        try:
            DRIVER.send(msg)
            sent = 0
            t0 = time.time()
            while time.time() - t0 < 600:
                # PARTIAL comes from the CLEAN body (markdown-reply) so loading
                # placeholders / citations can never be the prefix. When the clean
                # body doesn't exist yet, fall back to LOADING -- which the guard
                # below filters if it's a status/placeholder line.
                _pc = _clean_answer_text(); partial = _pc if _pc else _text(LOADING)
                _cleaned = _clean_answer_text(); final = _cleaned if _cleaned else _text(LASTMSG)
                # stream growing partial (skip "処理中" AND search-status lines)
                if not _is_proc(partial) and not _is_search_status(partial) and len(partial) > sent:
                    self._sse({"delta": partial[sent:]})
                    sent = len(partial)
                # lastChatMessage populated -- but it can KEEP GROWING after it first
                # appears, so finishing immediately truncates the tail. Stream its growth
                # and only finish once it has been STABLE for ~1.2s.
                if final and not _is_proc(final):
                    stable_text, stable_since = final, time.time()
                    while time.time() - t0 < 600:
                        if len(final) > sent:
                            self._sse({"delta": final[sent:]})
                            sent = len(final)
                        # Finish ONLY when generation has ACTUALLY stopped (Copilot's Stop button
                        # is gone) AND the text has then settled. Text-stability alone is not
                        # enough: Copilot pauses mid-generation (slow tokens / thinking) for >1.2s,
                        # which the old check mistook for completion -> it truncated the tail ("…ま")
                        # AND left Copilot generating server-side, so the NEXT send hit the 240s
                        # generation gate and surfaced "[bridge error: GenerationInProgress …]".
                        gen_active = False
                        try:
                            gen_active = DRIVER._is_generating()
                        except Exception:
                            gen_active = False
                        if final == stable_text and not gen_active:
                            if time.time() - stable_since >= 1.2:
                                break
                        else:
                            # text still growing OR Copilot still generating -> reset settle window
                            stable_text, stable_since = final, time.time()
                        time.sleep(0.3)
                        self._ping()             # detect Esc/Stop disconnect promptly
                        _cleaned2 = _clean_answer_text(); final = _cleaned2 if _cleaned2 else _text(LASTMSG)
                        if _is_proc(final):
                            final = stable_text
                    # authoritative final: send the CLEAN body as a REPLACE so the
                    # settled message is correct regardless of any streaming artifacts
                    # (placeholder->answer cursor corruption, leaked loading lines).
                    _finalclean = _clean_answer_text()
                    if _finalclean:
                        self._sse({"replace": _finalclean})
                    self._sse({}, "done")
                    return
                time.sleep(0.3)
                self._ping()                     # detect Esc/Stop disconnect promptly
            # outer-loop timeout end: same authoritative final replace
            _finalclean = _clean_answer_text()
            if _finalclean:
                self._sse({"replace": _finalclean})
            self._sse({}, "done")
        except Exception as e:
            # The client hung up (the user pressed Esc/Stop) OR a real error -- either way, click
            # Copilot's OWN stop button so the SERVER-SIDE generation actually halts. Before this,
            # Esc only closed our local stream while Copilot kept generating.
            try:
                PAGE.locator(COPILOT_SELECTORS["stop_button"]).first.click(timeout=3000)
            except Exception:
                pass
            try:
                self._sse({"delta": f"\n[bridge error: {type(e).__name__}: {e}]"})
                self._sse({}, "done")
            except Exception:
                pass
        finally:
            BUSY = False


def _find_or_open_agent(ctx):
    url = os.environ.get("MCP_IMPL_AGENT_URL", "").strip()
    if url:
        pg = ctx.new_page()
        pg.goto(url, wait_until="domcontentloaded")
        for _ in range(40):
            pg.wait_for_timeout(1000)
            if pg.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                break
        return pg
    for pg in ctx.pages:                       # fall back to any open agent tab
        if "/chat/agent/" in (pg.url or "") and pg.locator(COPILOT_SELECTORS["composer"]).count() > 0:
            return pg
    raise SystemExit("No agent page. Set MCP_IMPL_AGENT_URL in .env or open an agent tab in Edge.")


def main():
    global PAGE, DRIVER, AGENT_URL
    cdp = os.environ.get("MCP_CDP_URL", "http://localhost:9222")
    port = int(os.environ.get("MCP_BRIDGE_PORT", "8765"))
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = p.chromium.connect_over_cdp(cdp)
        ctx = br.contexts[0] if br.contexts else br.new_context()
        PAGE = _find_or_open_agent(ctx)
        DRIVER = CopilotWebDriver(PAGE)
        # bare agent URL (a fresh chat) for /new; strip any /conversation/<id> suffix
        AGENT_URL = os.environ.get("MCP_IMPL_AGENT_URL", "").strip()
        if not AGENT_URL and "/chat/agent/" in (PAGE.url or ""):
            AGENT_URL = (PAGE.url or "").split("/conversation/")[0]
        # single-threaded ON PURPOSE: Playwright sync objects are not thread-safe,
        # so every request must run in the same thread that owns the page.
        srv = HTTPServer(("127.0.0.1", port), Handler)
        print(f"copilot bridge: http://127.0.0.1:{port}  (driving {PAGE.url[-40:]})", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()

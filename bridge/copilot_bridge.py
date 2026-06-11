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

import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
from relay.copilot_autopilot_relay import COPILOT_SELECTORS, CopilotWebDriver, PROCESSING_MARKERS

load_dotenv()

LOADING = '[data-testid="loading-message"]'   # holds the GROWING partial answer
LASTMSG = '[data-testid="lastChatMessage"]'    # populates when the turn is DONE

PAGE = None      # set at startup
DRIVER = None
BUSY = False     # single conversation -> serialize requests
AGENT_URL = ""   # bare agent URL (a fresh chat); set at startup


def _wait_composer(timeout=40):
    for _ in range(timeout):
        PAGE.wait_for_timeout(1000)
        if PAGE.locator(COPILOT_SELECTORS["composer"]).count() > 0:
            return True
    return False


def _try_delete_conversation(url):
    """Best-effort delete of the backing Copilot conversation. KNOWN LIMITATION: in
    the agent-scoped view, the per-conversation menus surface the agent switcher,
    not a delete -- so this commonly returns (False, reason) and the caller falls
    back to opening the conversation for a manual delete. Honest about failure;
    never deletes a different conversation (we only act on the active one)."""
    if not url:
        return False, "no url"
    PAGE.goto(url, wait_until="domcontentloaded")
    _wait_composer()
    PAGE.wait_for_timeout(800)
    # try the active conversation's own options menu, then a delete item by text
    triggers = ['button[data-testid="overflow-menu-button"]',
                'button[aria-label*="オプション"]', 'button[aria-label*="More actions"]',
                'button[aria-label*="More options"]']
    del_texts = ["会話を削除", "チャットを削除", "この会話を削除", "Delete conversation", "削除", "Delete"]
    confirm_texts = ["削除", "Delete", "はい", "OK"]
    for sel in triggers:
        loc = PAGE.locator(sel)
        if loc.count() == 0:
            continue
        try:
            loc.first.click(force=True, timeout=2500)
            PAGE.wait_for_timeout(600)
            for txt in del_texts:
                mi = PAGE.get_by_role("menuitem", name=txt, exact=False)
                if mi.count() > 0:
                    mi.first.click(timeout=2500)
                    PAGE.wait_for_timeout(600)
                    for ct in confirm_texts:    # confirmation dialog, if any
                        cb = PAGE.get_by_role("button", name=ct, exact=False)
                        if cb.count() > 0:
                            try:
                                cb.first.click(timeout=2000)
                            except Exception:
                                pass
                            break
                    return True, "deleted"
            try:
                PAGE.keyboard.press("Escape")
            except Exception:
                pass
        except Exception:
            pass
    return False, "no per-conversation delete control in this view"


def _is_proc(t: str) -> bool:
    t = (t or "").strip()
    return (not t) or (any(m in t for m in PROCESSING_MARKERS) and len(t) < 40)


def _text(sel: str) -> str:
    try:
        loc = PAGE.locator(sel)
        return (loc.last.inner_text() or "").strip() if loc.count() else ""
    except Exception:
        return ""


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
  es.onmessage=e=>{const d=JSON.parse(e.data);if(d.delta){out.textContent+=d.delta;window.scrollTo(0,document.body.scrollHeight);}};
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
                if url:
                    PAGE.goto(url, wait_until="domcontentloaded")
                    ok = _wait_composer()
            except Exception as e:
                self._json({"ok": False, "error": str(e)}); return
            self._json({"ok": ok, "url": PAGE.url})
            return
        if parsed.path == "/delete":       # best-effort: delete the Copilot conversation
            if BUSY:
                self._json({"ok": False, "error": "busy"}); return
            url = (urllib.parse.parse_qs(parsed.query).get("url") or [""])[0]
            try:
                ok, reason = _try_delete_conversation(url)
            except Exception as e:
                ok, reason = False, str(e)
            self._json({"ok": ok, "error": reason})
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

    def _stream(self, msg: str):
        global BUSY
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        if not msg.strip():
            self._sse({}, "done")
            return
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
                partial = _text(LOADING)
                final = _text(LASTMSG)
                # stream growing partial (skip the brief "処理中" placeholder)
                if not _is_proc(partial) and len(partial) > sent:
                    self._sse({"delta": partial[sent:]})
                    sent = len(partial)
                # done: lastChatMessage populated (final answer)
                if final and not _is_proc(final):
                    if len(final) > sent:
                        self._sse({"delta": final[sent:]})
                    self._sse({}, "done")
                    return
                time.sleep(0.3)
            self._sse({}, "done")
        except Exception as e:
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

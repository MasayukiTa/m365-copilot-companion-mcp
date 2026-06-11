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
            return True
        # the dedicated Edge runs hidden in the background -- if a sign-in page shows up,
        # bring it to the foreground once so the user can authenticate.
        if not surfaced:
            try:
                from relay.edge_recover import surface, looks_like_login
                if looks_like_login(PAGE.url):
                    surface()
                    surfaced = True
            except Exception:
                pass
    return False


def _try_delete_conversation(url):
    """Delete the backing Copilot conversation for real, by id, via the GENERAL
    /chat view (its history lists conversations from every agent as bare-UUID rows
    and -- unlike the agent-scoped view -- exposes a working per-conversation delete).
    Targets ONLY the exact conversation id; never touches another. Restores the
    bridge to a fresh agent chat afterward. Returns (ok, reason)."""
    if not url or "/conversation/" not in url:
        return False, "no conversation id in url"
    cid = url.split("/conversation/")[1].split("?")[0].split("#")[0]
    ok, reason = False, "not run"
    try:
        PAGE.goto("https://m365.cloud.microsoft/chat", wait_until="domcontentloaded")
        PAGE.wait_for_timeout(8000)             # let the history sidebar populate
        try:
            PAGE.keyboard.press("Escape")        # dismiss any stale grounding popup
        except Exception:
            pass
        row = PAGE.locator('button[id="%s"]' % cid)
        if row.count() == 0:                     # one reload in case the list was stale
            PAGE.reload(wait_until="domcontentloaded")
            PAGE.wait_for_timeout(6000)
            row = PAGE.locator('button[id="%s"]' % cid)
        if row.count() == 0:
            ok, reason = False, "conversation row not found in /chat history"
        else:
            row.first.scroll_into_view_if_needed()
            row.first.hover()
            PAGE.wait_for_timeout(800)
            # element-dispatch click the row's "More" button -- a coordinate click
            # lands on the agent-switcher/grounding menu instead (the old failure).
            handle = PAGE.evaluate_handle(
                """(cid) => { const b = document.getElementById(cid); if (!b) return null;
                    const p = b.parentElement; if (!p) return null;
                    return [].slice.call(p.querySelectorAll('button')).find(
                        function (x) { return x !== b && x.getAttribute('aria-haspopup') === 'menu'; }) || null; }""",
                cid)
            el = handle.as_element()
            if el is None:
                ok, reason = False, "More button not found for row"
            else:
                el.click()
                PAGE.wait_for_timeout(700)
                mi = PAGE.locator('[role="menuitem"][aria-label="削除"]')
                if mi.count() == 0:
                    mi = PAGE.get_by_role("menuitem", name="削除", exact=True)
                mi.first.click(timeout=3000)
                PAGE.wait_for_timeout(700)
                cb = PAGE.locator('[role="alertdialog"] button:has-text("削除する")')
                if cb.count() == 0:
                    cb = PAGE.get_by_role("button", name="削除する")
                cb.first.click(timeout=3000)
                PAGE.wait_for_timeout(1300)
                gone = PAGE.evaluate("(cid) => !document.getElementById(cid)", cid)
                ok = bool(gone)
                reason = "deleted" if ok else "delete did not apply"
    except Exception as e:
        try:
            PAGE.keyboard.press("Escape")
        except Exception:
            pass
        ok, reason = False, "%s: %s" % (type(e).__name__, str(e))
    # restore the bridge to a fresh agent chat so the next message goes to the agent
    try:
        if AGENT_URL:
            PAGE.goto(AGENT_URL, wait_until="domcontentloaded")
            _wait_composer()
    except Exception:
        pass
    return ok, reason


def _is_proc(t: str) -> bool:
    t = (t or "").strip()
    return (not t) or (any(m in t for m in PROCESSING_MARKERS) and len(t) < 40)


def _text(sel: str) -> str:
    try:
        loc = PAGE.locator(sel)
        return (loc.last.inner_text() or "").strip() if loc.count() else ""
    except Exception:
        return ""


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
    Returns a list of {"role": "...", "text": "..."}. Raises on a page error."""
    _wait_turns()
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
        if parsed.path == "/history":      # scrape ALL turns of a conversation in order
            if BUSY:
                self._json({"ok": False, "error": "busy"}); return
            url = (urllib.parse.parse_qs(parsed.query).get("url") or [""])[0]
            try:
                if url:
                    PAGE.goto(url, wait_until="domcontentloaded")
                    _wait_composer()
                messages = _scrape_history()
            except Exception as e:
                self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}); return
            self._json({"ok": True, "url": PAGE.url, "messages": messages})
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
        self._stream_text(msg)

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
                partial = _text(LOADING)
                final = _text(LASTMSG)
                # stream growing partial (skip the brief "処理中" placeholder)
                if not _is_proc(partial) and len(partial) > sent:
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
                        if final == stable_text:
                            if time.time() - stable_since >= 1.2:
                                break
                        else:
                            stable_text, stable_since = final, time.time()
                        time.sleep(0.3)
                        final = _text(LASTMSG)
                        if _is_proc(final):
                            final = stable_text
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

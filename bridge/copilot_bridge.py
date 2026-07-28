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
import http.client
import json
import logging
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DELETE_LOG = REPO / ".fleet" / "delete_log.jsonl"
FLEET_CONVS_PATH = REPO / ".fleet" / "conversations.json"
GUID_RE = re.compile(r"/conversation/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
# A bare GUID (not URL-embedded) -- e.g. a sidebar row's id/conversationId, as found live on
# the ?titleId=... general-chat page shape (see SESSREF_PREFIX below for why this matters).
BARE_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# EMPIRICAL FINDING (live probe, 2026-07-06, bridge Edge :9223, MCP_IMPL_AGENT_URL shaped
# "https://m365.cloud.microsoft/chat/?titleId=T_..."): page.url NEVER carries a conversation
# identifier on this page shape -- it stays "https://m365.cloud.microsoft/chat/?redirfrom=
# CsrToSSR&auth=2" (or the bare titleId URL) before AND after a full send/reply exchange, and
# even a manual sidebar-row click that visibly switches the main pane's messages does not
# change page.url. So mechanism (a) (page.url after reply) is NOT usable here -- confirmed
# by direct observation, not assumption.
#
# What IS durable: each conversation has a stable conversationId GUID that appears as the
# `id` attribute on its sidebar row button (button[id=<guid>][aria-label=<title>], with a
# sibling "More"/"その他のオプション" button -- the same row shape _delete_by_guid already
# expects on the agent-rail page shape) AND is mirrored in
# localStorage["<...>-insights"].state.unpinnedHistoryItemsList[].sessionChat.conversationId.
# The most-recently-updated conversation sorts first in that list, so right after a send it
# is reliably OUR conversation. Since there is no real navigable URL for it, we store a
# SYNTHETIC reference "sess:<guid>" in the session's conv_url field; resume means: load
# AGENT_URL (so the sidebar renders), then click button[id=<guid>] to switch the main pane
# to that conversation -- NOT page.goto(), which this page shape has no use for.
SESSREF_PREFIX = "sess:"


def _conv_guid(url):
    """Extract the /conversation/<guid> id from an agent conversation URL, or ''."""
    if not url:
        return ""
    m = GUID_RE.search(url)
    return m.group(1) if m else ""


def make_sessref(guid):
    """Build the synthetic conv_url value we persist for a bare conversationId GUID."""
    guid = (guid or "").strip()
    return (SESSREF_PREFIX + guid) if guid else ""


def sessref_guid(ref):
    """Extract the GUID back out of a 'sess:<guid>' reference, or '' if not that shape."""
    ref = (ref or "").strip()
    if ref.startswith(SESSREF_PREFIX):
        return ref[len(SESSREF_PREFIX):]
    return ""


def classify_conv_ref(ref):
    """Pure classifier for a stored conv_url/reference string. Returns one of:
      "sessref"   -- our synthetic "sess:<guid>" scheme (resume = click sidebar row)
      "conv_url"  -- a real navigable URL carrying /conversation/<guid> (resume = page.goto)
      "bare_url"  -- some other non-empty URL/string (not reliably reattachable)
      "empty"     -- nothing stored
    No browser access -- pure string logic, so this is fully unit-testable."""
    ref = (ref or "").strip()
    if not ref:
        return "empty"
    if sessref_guid(ref):
        return "sessref"
    if _conv_guid(ref):
        return "conv_url"
    return "bare_url"


def should_autoresume(sess, fresh_flag=False):
    """Pure decision function for startup auto-resume: given a session dict (or None) and
    the --fresh CLI flag, decide whether main() should attempt to reattach. No browser
    access -- fully unit-testable. Returns (should, reason)."""
    if fresh_flag:
        return False, "fresh flag set"
    if not sess:
        return False, "no prior session"
    ref = sess.get("conv_url") or ""
    if classify_conv_ref(ref) == "empty":
        return False, "session has no conv_url"
    return True, "resumable session found"


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


# ── unify-the-view: register bridge sessions into .fleet/conversations.json ────────────────
# relay/fleet_runner.py's _register_convs (~line 1026) is the ONE existing writer of this file
# today (fleet workers only, source="fleet"); FleetCockpit's (C#) history viewer reads it as-is.
# We add ourselves as a SECOND, concurrent writer (source="chat") in fleet_runner's exact entry
# shape, so the cockpit shows bridge/chat sessions too with zero C# change. fleet_runner
# rewrites the whole file at run end, so every write here must be atomic (tmp+os.replace) and
# merge-dedup against whatever is on disk AT WRITE TIME (read-modify-write, not blind append),
# and must tolerate the file being briefly absent, non-list, or corrupt mid-rewrite by the other
# writer -- never raise out of a chat turn over this.

def merge_fleet_conversations(existing, new_entries):
    """PURE merge/dedup function: list-in/list-out, no file I/O (fully unit-testable).

    `existing` -- whatever was just read from .fleet/conversations.json (should be a list of
    dicts, but may be anything if the file is corrupt/foreign-shaped -- non-dict/malformed
    items are dropped rather than raising).
    `new_entries` -- the entries we want present, in fleet_runner's exact shape:
        {"url": str, "title": str, "source": "chat", "transcript": str, "name": str, "ts": float}

    Dedup mirrors fleet_runner._register_convs: keyed by "url" when non-empty. Entries whose
    url is "" (a sess:<guid> session has no real navigable URL) cannot be deduped by url, so
    they are instead deduped by ("source", "name") -- our own sid is unique per session, so this
    never collides with a real fleet worker entry (fleet entries always carry a name like "w0"
    but source=="fleet", never "chat"). An existing entry for the same key is UPDATED in place
    (so re-registering after a later conv_url capture refreshes the row) rather than duplicated.
    Returns the merged list (existing entries not touched by new_entries are preserved
    untouched, including foreign/other-source shapes)."""
    clean = [e for e in (existing or []) if isinstance(e, dict)]
    by_url = {}
    by_source_name = {}
    for idx, e in enumerate(clean):
        u = e.get("url") or ""
        if u:
            by_url[u] = idx
        else:
            key = (e.get("source"), e.get("name"))
            by_source_name[key] = idx
    for entry in (new_entries or []):
        if not isinstance(entry, dict):
            continue
        u = entry.get("url") or ""
        if u and u in by_url:
            clean[by_url[u]] = entry
            continue
        if not u:
            key = (entry.get("source"), entry.get("name"))
            if key in by_source_name:
                clean[by_source_name[key]] = entry
                continue
            by_source_name[key] = len(clean)
            clean.append(entry)
            continue
        by_url[u] = len(clean)
        clean.append(entry)
    return clean


def _read_fleet_conversations_raw():
    """Best-effort read of .fleet/conversations.json -> list, or [] on any problem (missing
    file, non-list JSON, corrupt JSON). Never raises. BOM-tolerant (fleet_runner's own C#-side
    consumer note: 'tolerate C# BOM')."""
    try:
        if not FLEET_CONVS_PATH.is_file():
            return []
        with open(FLEET_CONVS_PATH, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        logger.warning("fleet conversations.json unreadable/corrupt; skipping merge", exc_info=True)
        return []


def _write_fleet_conversations_atomic(entries):
    """Atomic tmp+os.replace write of the FULL entries list. Never raises (a registration
    hiccup must never crash a chat turn)."""
    try:
        FLEET_CONVS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(FLEET_CONVS_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, ensure_ascii=False)
        os.replace(tmp, str(FLEET_CONVS_PATH))
    except Exception:
        logger.warning("fleet conversations.json write failed; skipping registration", exc_info=True)


def register_bridge_session_in_fleet_convs(sid, title, conv_url, transcript_rel):
    """Register/refresh ONE bridge session as a "chat" entry in .fleet/conversations.json,
    in fleet_runner._register_convs's exact shape. Called from _persist_exchange whenever a
    session first gains (or refreshes) a non-empty conv_url. url is the raw conversation URL
    when known, else "" for a sess:<guid> reference (NOT a url -- cockpit tolerates empty urls
    today, see .fleet/status.json precedent). Read-merge-write is used (not blind append) so a
    concurrent fleet_runner rewrite between our read and write only risks losing OUR OWN entry
    to a benign re-registration next turn, never corrupting the file or the other writer's rows.
    Exception-guarded end-to-end; logs are ASCII-only."""
    try:
        kind = classify_conv_ref(conv_url)
        url_field = conv_url if kind == "conv_url" else ""
        entry = {
            "url": url_field,
            "title": (title or sid)[:60],
            "source": "chat",
            "transcript": transcript_rel or "",
            "name": sid,
            "ts": time.time(),
        }
        existing = _read_fleet_conversations_raw()
        merged = merge_fleet_conversations(existing, [entry])
        _write_fleet_conversations_atomic(merged)
    except Exception:
        logger.warning("register_bridge_session_in_fleet_convs failed for sid=%s", sid, exc_info=True)


def _load_fleet_sessions_view():
    """Read .fleet/conversations.json and map entries whose source != "chat" (i.e. genuine
    fleet-registered conversations, not our own chat rows already covered by S.list_sessions())
    into the /sessions?all=1 shape: {"sid":"", "title":..., "conv_url":..., "last_active_ts":...,
    "turns": None, "source": "fleet"}. Pure w.r.t. the filesystem read (no PAGE access) -- read-
    only, best-effort, never raises; returns [] on any problem."""
    out = []
    for e in _read_fleet_conversations_raw():
        if not isinstance(e, dict):
            continue
        if e.get("source") == "chat":
            continue
        out.append({
            "sid": "",
            "title": e.get("title") or e.get("name") or "",
            "conv_url": e.get("url") or "",
            "last_active_ts": e.get("ts") or 0,
            "turns": None,
            "source": "fleet",
        })
    return out


from dotenv import load_dotenv
from relay.copilot_autopilot_relay import COPILOT_SELECTORS, CopilotWebDriver, PROCESSING_MARKERS
# CONSENT_MARKERS is the SAME substring list relay_fleet.RelayWorker uses to detect an MCP
# connection-consent card in a reply -- imported (not re-listed) so the bridge and fleet never
# drift apart on what counts as a consent card. See relay_fleet.py CONSENT_MARKERS (~line 90).
from relay.relay_fleet import CONSENT_MARKERS
from bridge import session_store as S
from bridge import review_command
from relay.skills import SkillError, SkillStore, format_skill_list
# tool_probe is stdlib-only (see its module docstring) -- cheap to import here regardless of
# the heavy relay chain already loaded above. Used by the idle tool-call self-probe, see the
# "tool-call self-probe" section near the bottom of this file.
from tools import tool_probe

load_dotenv()

SKILL_STORE = SkillStore(REPO)

def _p2c_review_level():
    """Return the on-demand P2c level (0=off, 1=deep, 2=full validation).

    Invalid values fail closed to 0.  Reading the file on demand keeps the existing
    behavior where editing .env takes effect without rebuilding the native UI.
    """
    raw_value = None
    try:
        env_path = REPO / ".env"
        if env_path.is_file():
            for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if line.startswith("MCP_REVIEW_P2C="):
                    raw_value = line.split("=", 1)[1].strip()
                    break
    except Exception:
        pass
    if raw_value is None:
        raw_value = os.environ.get("MCP_REVIEW_P2C", "0").strip()
    try:
        level = int(raw_value)
    except (TypeError, ValueError):
        return 0
    return level if level in (0, 1, 2) else 0


def _p2c_review_enabled():
    return _p2c_review_level() > 0


P2C_HELP_TEXT = (
    "- `/deep-review [diff|<path>]` — 深掘りレビュー: 拒否時に同一タスクを新規会話で再試行し、二度拒否された作業だけを上限付きで分割。\n"
    "- `/deep-security-review [diff|<path>]` — 同上、セキュリティ観点の深掘りレビュー。\n"
)

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
    "- `/analyze <ファイルの絶対パス> | <分析指示>` — アナリストにデータファイルを渡して分析（数値は鵜呑みにせず自分でも確かめて）。`/an` も同じ。\n"
    "- `/review [diff|<path>]` — 全ファイル（または diff／指定パス）をレビューし要約を返す（数分〜）。\n"
    "- `/security-review [diff|<path>]` — 同上、セキュリティ観点のレビュー。\n"
    + "- `/review-fix [high|verified]` — レビューの指摘を修正（2段階: まず計画表示→ /review-fix confirm で実行。"
    "ファイル編集あり・自動バックアップ＆ワンクリックundo付き）。\n\n"
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
    "### Skills\n"
    "- `/skills` — 利用可能な Skill と承認状態を一覧表示。\n"
    "- `/<skill-name> [引数]` — 承認済み Skill を明示実行。作成・取込・承認はローカル端末のみ。\n\n"
    "### その他\n"
    "- `/history` — （HTTP `GET /history?url=...` 経由）会話全文をロール付きで取得。\n"
    "- `/help` — このヘルプ。`/?` `/commands` も同じ。\n\n"
    "それ以外の文は、そのまま Copilot エージェントに送られます。"
)


def _current_help_text():
    if not _p2c_review_enabled():
        return HELP_TEXT
    marker = "- `/review-fix"
    return HELP_TEXT.replace(marker, P2C_HELP_TEXT + marker, 1)

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
AGENT_URL = ""   # bare agent URL (a fresh chat); set at startup

# BUG 2 fix: bounded, per-episode retry state for the foreground last-resort surface(), mirroring
# relay/relay_fleet.py's RelayWorker consent-surface bookkeeping (_consent_surfaced /
# _consent_surfaced_ok / CONSENT_SURFACE_RETRY_MAX) as closely as this file's single-PAGE (not
# per-worker) model allows. WAS: a single module-global _CONSENT_SURFACED bool latched True
# UNCONDITIONALLY before surface() was even attempted, so ANY transient failure on that one
# lifetime attempt (slow headless->headed relaunch, a psutil-unavailable moment making
# edge_recover._headed_process_present always return False, a CDP race) permanently disabled
# surfacing for the rest of the process's -- possibly multi-day -- uptime.
#   - _CONSENT_SURFACE_OK latches True ONLY after a surface() call TRUTHFULLY succeeds (set
#     from the real `ok` result, never before the attempt) -- a failed attempt stays retryable.
#   - _CONSENT_SURFACE_ATTEMPTS bounds surface() work (both failed attempts and post-success
#     "still waiting for the user" checks) to CONSENT_SURFACE_RETRY_MAX within one episode, so a
#     run of failures still terminates with an honest message instead of retrying forever.
#   - _CONSENT_SURFACE_TERMINAL_SENT guards the one-time TERMINAL HONESTY announcement (see
#     _record_consent_unrecoverable) so it fires once per exhausted episode, not on every
#     subsequent call while still exhausted.
#   - An episode ENDS (state resets, see _reset_consent_surface_episode) the moment consent is
#     confirmed resolved -- auto-consent succeeds, or a turn answers normally again -- so a
#     LATER, genuinely new consent card gets its own full retry budget instead of inheriting an
#     exhausted one.
_CONSENT_SURFACE_OK = False
_CONSENT_SURFACE_ATTEMPTS = 0
_CONSENT_SURFACE_TERMINAL_SENT = False
CONSENT_SURFACE_RETRY_MAX = int(os.environ.get("MCP_CONSENT_SURFACE_RETRY_MAX", "3"))

# BUG 4b fix: bounded safety net so a surface()'d dedicated Edge can NEVER stay foreground
# forever, even when the normal rehide()-on-resolution pairing is missed. threading.Timer is
# one-shot (not a persistent daemon loop) -- started right after every surface() call and
# cancelled if a real rehide() fires first. Env-tunable like the file's other windows.
CONSENT_SURFACE_FORCE_REHIDE_SEC = float(os.environ.get("MCP_FORCE_REHIDE_SEC", "90"))


def _schedule_force_rehide(timeout=None):
    """Start a one-shot background timer that force-rehides the dedicated Edge after
    `timeout` seconds (default CONSENT_SURFACE_FORCE_REHIDE_SEC). Safety net for BUG 4a/4b:
    covers every surface() call site (consent last-resort AND the sign-in surfaces, as
    defense-in-depth) so the window can never stay foreground indefinitely even if the
    caller's own rehide() is skipped by an unexpected code path. Exception-guarded; the
    Timer thread is daemon=True so it never blocks process exit. Returns the Timer so the
    caller can _cancel_force_rehide() it once a normal rehide() has already happened."""
    t = CONSENT_SURFACE_FORCE_REHIDE_SEC if timeout is None else timeout

    def _safe_rehide():
        try:
            from relay.edge_recover import rehide
            rehide()
            logger.info("force-rehide safety net fired after %.0fs", t)
        except Exception:
            logger.warning("force-rehide safety net: rehide() raised", exc_info=True)

    timer = threading.Timer(max(0.0, t), _safe_rehide)
    timer.daemon = True
    timer.start()
    return timer


def _cancel_force_rehide(timer):
    """Best-effort cancel of a pending force-rehide timer (no-op if None or already fired)."""
    if timer is not None:
        try:
            timer.cancel()
        except Exception:
            pass


def _reset_consent_surface_episode():
    """Call whenever consent is confirmed resolved -- auto-consent succeeded, or a turn
    answered normally again -- so a LATER, genuinely new consent card starts its own full
    CONSENT_SURFACE_RETRY_MAX budget instead of inheriting an already-exhausted one. Never
    raises (pure in-process state reset)."""
    global _CONSENT_SURFACE_OK, _CONSENT_SURFACE_ATTEMPTS, _CONSENT_SURFACE_TERMINAL_SENT
    _CONSENT_SURFACE_OK = False
    _CONSENT_SURFACE_ATTEMPTS = 0
    _CONSENT_SURFACE_TERMINAL_SENT = False


def _record_consent_unrecoverable(detail: str = ""):
    """TERMINAL HONESTY: both auto-consent and the bounded surface() retries have genuinely
    failed for this episode. Makes that state unmissable without new infra: persists
    kind="consent_unrecoverable" to tools.tool_probe's EXISTING .fleet/tool_probe.json sidecar
    (extends its documented `kind` field only -- no schema change) so the cockpit's Tool health
    dot goes red with an actionable state, and logs the exact manual recovery command. Fires at
    most once per exhausted episode (guarded by _CONSENT_SURFACE_TERMINAL_SENT). Best-effort;
    never raises (tool_probe.record_probe already swallows all I/O errors itself)."""
    global _CONSENT_SURFACE_TERMINAL_SENT
    if _CONSENT_SURFACE_TERMINAL_SENT:
        return
    _CONSENT_SURFACE_TERMINAL_SENT = True
    try:
        tool_probe.record_probe(False, "consent_unrecoverable", detail=(detail or "")[:200])
    except Exception:
        pass
    logger.warning(
        "consent unrecoverable: auto-consent and surface() both exhausted (%s). Manual "
        "recovery: python -m relay.edge_reconnect --cdp-url http://127.0.0.1:9223", detail)


def _consent_surface_attempt(detail: str = "") -> bool:
    """Core bounded-retry, success-only-latching surface() attempt (BUG 2 fix). Shared by BOTH
    an interactive turn (Handler._consent_last_resort_surface wraps this with the user-facing
    SSE message) and the idle tool-probe's consent recovery (_run_tool_probe, which has no live
    SSE consumer to write to) -- see the module docstring above _CONSENT_SURFACE_OK for the
    full design. Returns True iff the dedicated Edge is truthfully up (freshly surfaced this
    call, OR already surfaced earlier this episode and still within CONSENT_SURFACE_RETRY_MAX).
    Never raises into the caller."""
    global _CONSENT_SURFACE_OK, _CONSENT_SURFACE_ATTEMPTS
    if _CONSENT_SURFACE_ATTEMPTS >= CONSENT_SURFACE_RETRY_MAX:
        # Bounded retry budget for this episode is exhausted -- stop yanking/re-checking, and
        # make sure the terminal state was announced (idempotent past the first call).
        _record_consent_unrecoverable(detail)
        return False
    _CONSENT_SURFACE_ATTEMPTS += 1
    if _CONSENT_SURFACE_OK:
        # Already truthfully surfaced this episode -- the window should still be up (or was
        # force-rehidden after CONSENT_SURFACE_FORCE_REHIDE_SEC); don't yank it again. Report
        # "surfaced" so the caller keeps waiting quietly, within the bounded budget above.
        return True
    ok = False
    try:
        from relay.edge_recover import surface
        port = int(os.environ.get("MCP_BRIDGE_CDP_PORT", "9223"))
        target = AGENT_URL or ((PAGE.url or "") if PAGE is not None else "")
        ok = bool(surface(port=port, open_url=target))
        if ok:
            # BUG 4a/4b fix: this surface() had no paired rehide() at all -- fire-and-forget,
            # so the window stayed foreground until the process died. Precisely detecting
            # "consent resolved" here is hard (the caller decides success on the NEXT turn),
            # so instead schedule the bounded safety net: force the window back down on its
            # own after CONSENT_SURFACE_FORCE_REHIDE_SEC regardless of what the user does.
            _schedule_force_rehide()
    except Exception:
        logger.warning("_consent_surface_attempt: surface() raised", exc_info=True)
        ok = False
    if ok:
        _CONSENT_SURFACE_OK = True   # SUCCESS-ONLY LATCH -- was latched True unconditionally
                                      # BEFORE the attempt; a failure now leaves this False so
                                      # the next call (bounded above) retries instead of being
                                      # permanently disabled.
    elif _CONSENT_SURFACE_ATTEMPTS >= CONSENT_SURFACE_RETRY_MAX:
        _record_consent_unrecoverable(detail)
    return ok


# ── concurrency (work mode) ──────────────────────────────────────────────────────────────────
# The server used to be a plain (single-threaded) HTTPServer -- fine while every request was
# short, but a /goal run can occupy the page for many turns over minutes, and during that time
# /send (steering) and /stop MUST still get through immediately. So the server is now a
# ThreadingHTTPServer variant (see _SingleBindHTTPServer below).
#
# THREAD AFFINITY (the reason a plain "just add threads" change is NOT enough): Playwright's
# SYNC API is bound to the OS thread that created it (sync_playwright()'s greenlet-based
# dispatch loop lives on that one thread) -- calling PAGE/DRIVER methods from a DIFFERENT
# thread raises "Cannot switch to a different thread" (confirmed live while building this: a
# /new request handled on a second request-thread hit exactly that error against a PAGE
# created in main()'s thread). So it is not enough to lock around PAGE calls; those calls must
# physically RUN on the one thread that owns PAGE. PageExecutor is that thread: main() creates
# PAGE/DRIVER INSIDE PageExecutor.run() (so page creation and every later page call share the
# same thread), and every request thread that needs the page calls run_on_page_thread(fn),
# which enqueues fn and blocks the CALLING thread until PageExecutor has run it on the owner
# thread and posted back the result/exception. Store-only endpoints (/send, /stop, /sessions,
# /) never call run_on_page_thread, so they stay responsive even while a long /goal turn is
# mid-flight on the page thread.
#
# PAGE_LOCK is a SEPARATE, higher-level concern: logical exclusivity between REQUESTS (so two
# /goal calls, or a /goal and a /history, never interleave their multi-step operations against
# each other's CAPTURE_BASELINE/ACTIVE_SID state), not raw thread-safety (PageExecutor already
# guarantees that by construction -- only one fn runs on the page thread at a time). /goal,
# /stream, /new, /resume, /switch, /history try-acquire it (non-blocking) and return
# {"ok":false,"error":"busy"} immediately if another PAGE-touching request already holds it.
PAGE_LOCK = threading.Lock()
# Guards S.queue_input/S.pop_input call sites in THIS process (session_store.py itself is
# untouched/unlocked -- its atomic os.replace() writes are safe across processes, but within one
# process a bridge worker thread draining the queue and a /send thread enqueuing into it could
# otherwise interleave read-modify-write in a way that drops an entry).
INPUT_LOCK = threading.Lock()
# Cooperative stop flag for the running /goal loop: /stop sets it; the loop checks it at each
# turn boundary and, once seen, finishes the CURRENT turn and reports outcome="stopped". Reset
# to False at the start of every new /goal run.
STOP_REQUESTED = False
# Wallclock of the most recent REAL user/goal turn, stamped by _run_one_turn (the single choke
# point both _stream_text and _run_work_phase call through). The tool-call self-probe (see
# "tool-call self-probe" section near the bottom of this file) reads this to skip itself while
# the user is actively working, so a probe can never compete with -- or be mistaken for -- live
# use, and never burns the user's agent context mid-task. 0.0 (epoch) at startup so a probe is
# allowed to run before the very first real turn.
_LAST_USER_TURN_TS = 0.0
# /review-fix is DESTRUCTIVE (it edits the user's own files), unlike /review and
# /security-review which only ever read. It is therefore a MANDATORY two-step confirm:
#   step 1: "/review-fix [high|verified]" runs bench/review_fix.py --dry-run (no files
#           touched) and shows the plan, then ARMS _REVIEW_FIX_PENDING with the parsed
#           filters and the current wallclock time.
#   step 2: "/review-fix confirm" only actually runs the fix if it lands within
#           REVIEW_FIX_CONFIRM_WINDOW_SEC seconds of that arm -- otherwise the user is told
#           to re-run "/review-fix" first to see a fresh plan. A successful confirm clears
#           the pending state immediately (single-use: one plan arms at most one execute).
# Module-level (not per-session) is deliberate and matches STOP_REQUESTED/ACTIVE_SID above --
# this bridge serves one interactive user at a time from one browser tab.
_REVIEW_FIX_PENDING = {"ts": 0.0, "parsed": None}
REVIEW_FIX_CONFIRM_WINDOW_SEC = 120


class PageExecutor:
    """Runs every Playwright-touching callable on ONE dedicated OS thread (the thread that
    also CREATES PAGE/DRIVER -- see run()), so request-handler threads (which may be any of
    the ThreadingMixIn pool) never call into Playwright's sync API directly. submit(fn) may be
    called from any thread; it blocks the CALLER until fn has run on the owner thread and
    returns fn's result (or re-raises fn's exception in the caller's thread/traceback context).

    No PAGE/browser access in THIS class itself (only generic queue/thread plumbing), so its
    queueing behavior is exercised by a hermetic unit test using a plain callable instead of a
    real page."""

    def __init__(self):
        self._q: "queue.Queue" = queue.Queue()
        self._thread = None

    def start(self, target):
        """Start the owner thread running `target()` (main()'s page-setup-then-serve
        function). `target` is responsible for calling drain_once()/run_forever() itself once
        PAGE/DRIVER are ready, so page CREATION and every later page CALL share one thread."""
        self._thread = threading.Thread(target=target, daemon=True, name="page-executor")
        self._thread.start()

    def submit(self, fn, *args, **kwargs):
        """Enqueue fn(*args, **kwargs) for the owner thread and block until it completes.
        Re-raises fn's exception in the calling thread if it raised. Must not be called FROM
        the owner thread itself (that would deadlock waiting on its own queue item)."""
        done = threading.Event()
        box = {}

        def _job():
            try:
                box["result"] = fn(*args, **kwargs)
            except BaseException as e:  # noqa: BLE001 -- must propagate ANY exception to the caller
                box["error"] = e
            finally:
                done.set()

        self._q.put(_job)
        done.wait()
        if "error" in box:
            raise box["error"]
        return box["result"]

    def submit_bounded(self, timeout, fn, *args, **kwargs):
        """Like submit(), but fail closed if the owner thread stops servicing its queue.

        This is intentionally reserved for liveness probes. A timed-out Playwright callable cannot
        be cancelled safely, so callers must terminate the bridge process and let the keepalive
        supervisor rebuild the page rather than continuing with a possibly wedged owner thread.
        """
        done = threading.Event()
        box = {}

        def _job():
            try:
                box["result"] = fn(*args, **kwargs)
            except BaseException as e:  # noqa: BLE001 -- match submit() propagation semantics
                box["error"] = e
            finally:
                done.set()

        self._q.put(_job)
        if not done.wait(timeout=max(0.0, float(timeout))):
            raise TimeoutError("page executor did not complete its liveness probe")
        if "error" in box:
            raise box["error"]
        return box["result"]

    def run_forever(self):
        """Owner-thread loop: pull and run jobs until the process exits. Call this from
        INSIDE `target` (see start()) after PAGE/DRIVER are constructed on this same thread."""
        while True:
            job = self._q.get()
            job()


PAGE_EXECUTOR = PageExecutor()


def run_on_page_thread(fn, *args, **kwargs):
    """Thin wrapper so call sites read as an ordinary function call. See PageExecutor's
    docstring for why this indirection exists (Playwright sync-API thread affinity)."""
    return PAGE_EXECUTOR.submit(fn, *args, **kwargs)

# ── session lifecycle (durable, resumable) ──────────────────────────────────────────────────
# ONE active session at a time, matching the bridge's single-PAGE design (no multi-session
# concurrency -- see session_store.py's docstring: M365 Copilot keeps conversation context
# server-side, so a "session" here is just a local label + conv_url + transcript around
# whichever ONE Copilot conversation this bridge currently drives).
ACTIVE_SID = None
# Change-based capture baseline: {"cur": <aria-current guid or "">, "known": set(<guids>)}
# snapshotted BEFORE a conversation-creating send (see _record_capture_baseline). None until
# first recorded.
CAPTURE_BASELINE = None


# ── WORK MODE (autonomous multi-turn /goal loop) ─────────────────────────────────────────────
# Pure logic only in this section -- no PAGE/browser access -- so it is fully hermetically
# unit-testable. Wording mirrors relay/relay_fleet.py's proven CONTINUE_JOB/_task_anchor style
# (concise instruction + explicit terminal-marker contract + OUTPUT_DISCIPLINE-flavored brevity
# clause) rather than inventing a divergent convention; the one deliberate deviation from the
# fleet's tail-line "DONE"/"CONTINUE"/"STUCK: reason" word convention is the literal
# "===DONE===" sentinel line, which is what the FROZEN HTTP contract (session_cli.py's /goal
# consumer) and this mission spec require.
WORK_MODE_DONE_MARKER = "===DONE==="

# Turn-1 wrapper: states the goal, the autonomous/stepwise expectation (mirrors PROTOCOL's
# "ツールを使い自律的に進める" framing), and the exact terminal-marker contract.
WORK_MODE_GOAL_PREFIX = (
    "次のゴールを、必要なツールを使いながら自律的に一歩ずつ進めてください。"
    "ゴール全体が完了するまで、こちらから促さなくても続けてください。"
    "ゴールが完全に完了したら、最後の行に厳密に次の一行だけを出力してください（他の文字を続けない）: "
    + WORK_MODE_DONE_MARKER + "\n\n--- ゴール ---\n"
)

# Continue nudge (no queued steering input): mirrors CONTINUE_JOB's "次のステップを実行して
# ください" wording, restating the terminal-marker contract every turn (same rationale as
# _task_anchor -- a long-running loop must not forget the contract).
WORK_MODE_CONTINUE_NUDGE = (
    "続けてください。ゴール全体が完了したら最後の行に厳密に "
    + WORK_MODE_DONE_MARKER + " のみを出力してください。"
)

# Resume-from-interruption nudge (used as turn 1's message for /goal?resume=1 instead of the
# original goal text -- the goal itself is already known to the conversation from before the
# crash/restart; this just re-anchors the agent on continuing it).
WORK_MODE_RESUME_NUDGE = (
    "直前の作業が中断されました。中断前のゴールに関する作業を続けてください。"
    "ゴール全体が完了したら最後の行に厳密に " + WORK_MODE_DONE_MARKER + " のみを出力してください。"
)

DEFAULT_MAX_TURNS = 30
MAX_CONSECUTIVE_TURN_ERRORS = 2


def wrap_goal_text(goal_text: str) -> str:
    """Build turn 1's message for a fresh /goal run: the goal text wrapped with the work-mode
    instruction + terminal-marker contract. Pure string logic."""
    return WORK_MODE_GOAL_PREFIX + (goal_text or "")


def detect_done(text: str):
    """Pure DONE-marker detector/stripper. Returns (is_done, stripped_text).

    A turn is DONE iff WORK_MODE_DONE_MARKER appears as its OWN line (surrounding whitespace
    tolerated) -- not merely as a substring somewhere in prose, so a turn that merely
    *mentions* the marker in passing is not mistaken for completion. When done, the marker
    line (and anything after it -- the spec says nothing should follow it, but a wayward
    trailing newline/space from the model is tolerated) is stripped from the returned text so
    neither the emitted SSE nor the persisted transcript carries the sentinel."""
    t = text or ""
    lines = t.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == WORK_MODE_DONE_MARKER:
            stripped = "\n".join(lines[:i]).rstrip()
            return True, stripped
    return False, t


def select_next_message(queued, continue_nudge=WORK_MODE_CONTINUE_NUDGE):
    """Pure next-message selector for the loop's turn N+1 (N not done).

    `queued` is the list of inputs popped from S.pop_input this turn boundary (oldest-first,
    already drained by the caller -- this function does no popping itself). Returns
    (next_message, steered_texts):
      - queued non-empty -> the inputs joined with newlines is the next message; steered_texts
        is the same list (caller emits one {"steered": ...} SSE event per entry).
      - queued empty -> the continue nudge is the next message; steered_texts is [].
    """
    if queued:
        return "\n".join(queued), list(queued)
    return continue_nudge, []


def decide_outcome(done, stop_requested, turn, max_turns, consecutive_errors,
                    max_consecutive_errors=MAX_CONSECUTIVE_TURN_ERRORS):
    """Pure outcome decision for the loop's end-of-turn check. Priority order (matches the
    mission spec's stop-conditions list): DONE > too many consecutive errors > stop flag >
    max_turns reached > None (keep looping). Returns one of
    "done" | "error" | "stopped" | "max_turns" | None."""
    if done:
        return "done"
    if consecutive_errors >= max_consecutive_errors:
        return "error"
    if stop_requested:
        return "stopped"
    if max_turns and turn >= max_turns:
        return "max_turns"
    return None


def resume_eligibility(sess):
    """Pure eligibility check for GET /goal?resume=1: the session must exist, carry
    mode=="interrupted" (set by startup auto-resume when a crash was detected -- see main()),
    and have a stored goal text to resume. Returns (ok, reason_or_goal_text) where the second
    element is the error reason on failure or the goal text to resume on success."""
    if not sess:
        return False, "no session"
    if sess.get("mode") != "interrupted":
        return False, "session is not in an interrupted goal (mode=%s)" % sess.get("mode")
    goal_text = sess.get("goal") or ""
    if not goal_text:
        return False, "no stored goal to resume"
    return True, goal_text


# ── VERIFIED LOOP (docs/loop-engineering.md sec5.3 producer/critic split) ───────────────────
# Pure logic only in this section -- no PAGE/browser access -- so it is fully hermetically
# unit-testable, same discipline as the WORK MODE section above. The critic runs in an
# UNCONTAMINATED conversation (a brand-new /new page that never touches ACTIVE_SID/session
# bookkeeping -- see _run_critic_pass near the Handler methods for the browser-touching half).
DEFAULT_MAX_LOOPS = 3

# Fixed rubric wording (spec SS5.1.3 / SS5.3): the critic gets ONLY the AC and the deliverable,
# is required to answer in STRICT JSON, and is explicitly forbidden from free-form improvement
# suggestions (SS5.3: "critics that offer free-form improvements breed overcorrection"). The
# shape and wording are a module-level constant on purpose -- a fixed rubric is what makes the
# critic's pass/fail a repeatable external signal rather than an ad hoc, rephrased-every-time
# judgment call.
# NOTE: built by CONCATENATION (not str.format) in build_rubric_prompt below -- the JSON
# example embeds literal { } braces that would collide with .format()'s placeholder syntax.
RUBRIC_PROMPT_HEADER = (
    "あなたはこれから提示する成果物を、以下の受け入れ基準(Acceptance Criteria)だけを根拠に判定する"
    "検証者です。あなたはこの作業の実装には関与していません。改善案・提案・アドバイスは一切書かないで"
    "ください。出力は次の形の厳密な JSON オブジェクト一つだけにしてください。前後に説明文・"
    "コードフェンス・その他の文字を一切付けないでください:\n"
    '{"pass": true または false, "failed_ac": ["未達のAC-idの配列（全て満たしていれば空配列）"], '
    '"reasons": ["各未達AC-idについての短い理由（全て満たしていれば空配列）"]}\n\n'
)

# Nudge sent (in the SAME critic conversation) when the first reply could not be parsed as the
# required JSON -- retried ONCE per the mission spec ("retry ONCE with a 'JSON only' nudge").
RUBRIC_JSON_ONLY_NUDGE = (
    "JSON のみを出力してください。説明文もコードフェンスも不要です。次の形だけを出力してください: "
    '{"pass": true または false, "failed_ac": [...], "reasons": [...]}'
)

# Continuation message sent back into the WORKING conversation after a fail verdict that is
# neither exhausted nor oscillating (spec step 4's "else" branch): only the failed_ac + reasons,
# nothing else -- the working agent already has its own context, it just needs to know what the
# critic flagged.
VERIFY_CONTINUATION_TEMPLATE = (
    "検証で以下が未達: {items}\n修正して、完了したら " + WORK_MODE_DONE_MARKER + " を最後の行に出力してください。"
)


def build_rubric_prompt(ac: str, deliverable: str) -> str:
    """Pure prompt builder: AC + deliverable -> the fixed rubric prompt string. Concatenation
    (not str.format) because RUBRIC_PROMPT_HEADER embeds literal JSON braces. Both sections are
    included VERBATIM -- verified directly by tests, not just by convention."""
    return (RUBRIC_PROMPT_HEADER
            + "--- 受け入れ基準 (Acceptance Criteria) ---\n" + (ac or "") + "\n\n"
            + "--- 成果物 (Deliverable) ---\n" + (deliverable or "") + "\n")


def build_continuation_message(failed_ac, reasons) -> str:
    """Pure builder for the fail-but-keep-looping continuation message sent back into the
    WORKING conversation. `failed_ac` and `reasons` are parallel-ish lists (not required to be
    the same length -- zipped defensively); renders as a compact bulleted block."""
    failed_ac = list(failed_ac or [])
    reasons = list(reasons or [])
    items = []
    for i, ac_id in enumerate(failed_ac):
        reason = reasons[i] if i < len(reasons) else ""
        items.append(f"{ac_id}: {reason}" if reason else str(ac_id))
    if not items:
        items = reasons or ["(理由不明)"]
    return VERIFY_CONTINUATION_TEMPLATE.format(items="; ".join(items))


def _extract_first_json_object(text: str):
    """Find the first balanced {...} block in `text` and return it as a raw substring, or None
    if no balanced brace block exists. Brace-counting (not regex) so nested objects (e.g. a
    "reasons" array containing braces some model hallucinated) do not truncate early. Pure
    string logic -- tolerates prose before/after the JSON, exactly the "embedded JSON in prose"
    case the mission spec calls out."""
    t = text or ""
    start = t.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(t)):
        ch = t[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return t[start:i + 1]
    return None


def parse_verdict(text: str):
    """Pure, tolerant verdict parser. Returns a dict:
      {"ok": bool, "pass": bool, "failed_ac": [...], "reasons": [...], "needs_retry": bool}

    "ok" is True iff a JSON object with at least a boolean "pass" key was found (embedded in
    prose or standalone -- both accepted). "needs_retry" is True when nothing parseable was
    found at all (the caller retries once with the JSON-only nudge); when ok is False AND
    needs_retry is False that means a SECOND parse attempt already failed (final, non-retryable
    -- caller treats it as a fail verdict with reason 'critic output unparseable', per spec
    step 3). failed_ac/reasons default to [] when absent or wrong-typed so a malformed-but-
    present JSON object never raises downstream."""
    blob = _extract_first_json_object(text)
    if blob is None:
        return {"ok": False, "pass": False, "failed_ac": [], "reasons": [], "needs_retry": True}
    try:
        obj = json.loads(blob)
    except Exception:
        return {"ok": False, "pass": False, "failed_ac": [], "reasons": [], "needs_retry": True}
    if not isinstance(obj, dict) or "pass" not in obj:
        return {"ok": False, "pass": False, "failed_ac": [], "reasons": [], "needs_retry": True}
    passed = bool(obj.get("pass"))
    failed_ac = obj.get("failed_ac")
    failed_ac = list(failed_ac) if isinstance(failed_ac, list) else []
    reasons = obj.get("reasons")
    reasons = list(reasons) if isinstance(reasons, list) else []
    return {"ok": True, "pass": passed, "failed_ac": failed_ac, "reasons": reasons,
            "needs_retry": False}


def is_oscillating(prev_failed_ac, cur_failed_ac) -> bool:
    """Pure oscillation detector (spec outcome "escalate_oscillation"): True iff both are
    non-empty AND the SET of failed AC ids is identical between the previous and current
    verdict (order-independent -- a model that lists the same failures in a different order
    is still oscillating, not making progress). A first verdict (prev is None/empty) is never
    oscillating -- there is nothing to compare against yet."""
    if not prev_failed_ac or not cur_failed_ac:
        return False
    return set(prev_failed_ac) == set(cur_failed_ac)


def decide_verify_outcome(verdict_pass: bool, loop_n: int, max_loops: int, oscillating: bool):
    """Pure outcome decision table for one verify loop iteration (spec step 4). Priority order:
    pass wins outright; else oscillation is checked BEFORE the loop-budget check (an
    oscillating fail on the very last allowed loop is still an oscillation -- the operator
    should know the failures were IDENTICAL, not merely that the budget ran out); else budget
    exhaustion; else None (keep looping: reattach to the working conversation and continue).
    Returns "done_verified" | "escalate_oscillation" | "verify_failed" | None."""
    if verdict_pass:
        return "done_verified"
    if oscillating:
        return "escalate_oscillation"
    if max_loops and loop_n >= max_loops:
        return "verify_failed"
    return None


def _next_pending(sid):
    """Thin wrapper around S.pop_input so callers/tests can substitute a fake pop function
    without importing session_store's file I/O. Returns None if sid is falsy. Locked with
    INPUT_LOCK so a drain (this) and an enqueue (_queue_input_locked, from /send) in this
    process can never interleave a read-modify-write on the same session's pending list."""
    if not sid:
        return None
    with INPUT_LOCK:
        return S.pop_input(sid)


def _queue_input_locked(sid, text):
    """Thin, lock-guarded wrapper around S.queue_input -- the /send endpoint's enqueue side of
    the same INPUT_LOCK that guards _next_pending's pop side."""
    with INPUT_LOCK:
        S.queue_input(sid, text)


def drain_pending_once(sid, pop_fn=None, max_n=50):
    """Pop up to `max_n` queued inputs for `sid` via `pop_fn` (defaults to _next_pending, which
    is INPUT_LOCK-guarded) and return them as a list, oldest-first. Pure w.r.t. control flow --
    `pop_fn` is injected so this is unit-testable with a fake FIFO (no real session_store file
    I/O needed in tests). Stops early once pop_fn returns None (queue empty) or max_n is
    reached (safety bound against a runaway queue)."""
    pop_fn = pop_fn or _next_pending
    out = []
    if not sid:
        return out
    for _ in range(max(0, max_n)):
        item = pop_fn(sid)
        if item is None:
            break
        out.append(item)
    return out


def _wait_composer(timeout=40):
    surfaced = False
    force_timer = None
    for _ in range(timeout):
        PAGE.wait_for_timeout(1000)
        if PAGE.locator(COPILOT_SELECTORS["composer"]).count() > 0:
            # If we surfaced the hidden Edge for sign-in, auth is now done (the
            # composer rendered) -> drop the window back to the background at once.
            if surfaced:
                _cancel_force_rehide(force_timer)  # real rehide is happening now; drop the net
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
                    # surface() now returns a TRUTHFUL bool (a headed process was actually
                    # verified) -- there is no notify/toast mechanism in this file to gate,
                    # but log the real outcome so a failed auto-surface is visible in logs
                    # rather than silently assumed to have worked. Pass AGENT_URL (the bare
                    # agent URL this bridge drives) so a headed relaunch lands on the agent
                    # conversation instead of the launcher's default generic top page.
                    # port 9223: the bridge drives its OWN Edge (copilot-bridge-edge), NOT the
                    # fleet's :9222 -- omitting port would surface the wrong browser.
                    if not surface(port=int(os.environ.get("MCP_BRIDGE_CDP_PORT", "9223")), open_url=AGENT_URL):
                        logger.warning("_wait_composer: surface() could not confirm a headed "
                                       "bridge Edge; sign-in prompt may still be hidden. "
                                       "Manual: powershell -NoProfile -ExecutionPolicy Bypass "
                                       "-File scripts\\start_companion_edge.ps1 -Foreground -Port 9223")
                    surfaced = True
                    # BUG 4b safety net: this function can return False (composer never
                    # rendered within `timeout`) with NO rehide() of its own -- schedule the
                    # bounded force-rehide so the window still comes back down eventually.
                    force_timer = _schedule_force_rehide()
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
    force_timer = None
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
                _cancel_force_rehide(force_timer)  # real rehide is happening now; drop the net
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
                    # surface() now returns a TRUTHFUL bool (verified headed process) -- no
                    # notify/toast mechanism exists in this file, but log a real failure so
                    # it is visible rather than silently assumed to have worked. Pass the
                    # actual target `url` so a headed relaunch lands on the conversation this
                    # call was trying to reach, instead of the launcher's default top page.
                    # port 9223: surface the bridge's own Edge, not the fleet's :9222.
                    if not surface(port=int(os.environ.get("MCP_BRIDGE_CDP_PORT", "9223")), open_url=url):
                        logger.warning("_goto_settled: surface() could not confirm a headed "
                                       "bridge Edge; sign-in prompt may still be hidden. "
                                       "Manual: powershell -NoProfile -ExecutionPolicy Bypass "
                                       "-File scripts\\start_companion_edge.ps1 -Foreground")
                    surfaced = True
                    # BUG 4b safety net -- see _wait_composer's identical comment.
                    force_timer = _schedule_force_rehide()
                touch_pause()
        except Exception:
            pass
        PAGE.wait_for_timeout(1500)
    settled = not _looks_redirected(PAGE.url or "", url)
    # If we surfaced but the wall never cleared, still rehide so a failed/abandoned
    # sign-in does not leave the companion Edge stuck in the foreground.
    if surfaced:
        _cancel_force_rehide(force_timer)
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


# ── conversation-identity capture + resume (durable session support) ───────────────────────
# EMPIRICAL (see SESSREF_PREFIX comment near the top): on the ?titleId=... general-chat page
# shape this bridge actually drives (MCP_IMPL_AGENT_URL in .env), page.url never carries a
# conversation id -- so capture reads the conversationId out of the SAME localStorage
# "insights" blob the M365 Copilot SPA itself uses to render the history sidebar, and resume
# clicks that conversation's sidebar row by id (a plain button[id=<guid>], NOT scoped to
# #m365-copilot-chats-section -- that section only exists on the /chat/agent/<id> page shape,
# which is NOT what this bridge's .env points at). Both paths are best-effort and exception-
# guarded: a failure here must never break a chat turn.
_INSIGHTS_JS = r"""
() => {
  for (var i = 0; i < localStorage.length; i++) {
    var k = localStorage.key(i);
    if (k.indexOf('insights') === -1) continue;
    try {
      var parsed = JSON.parse(localStorage.getItem(k));
      var items = (parsed && parsed.state && parsed.state.unpinnedHistoryItemsList) || [];
      return items.map(function(it) {
        var sc = (it && it.sessionChat) || {};
        return {conversationId: sc.conversationId || '', updateTimeUtc: sc.updateTimeUtc || 0,
                preview: (sc.preview || '').slice(0, 80)};
      });
    } catch (e) {
      return [];
    }
  }
  return [];
}
"""


# The OPEN conversation's sidebar row carries aria-current="page" (confirmed live: after a
# row-click swap, the target row reports aria-current="page"; chrome nav rows like "new
# chat" also use aria-current but their id is not a GUID, so filtering to GUID-shaped ids
# isolates the conversation row). This is the direct DOM signal for "which conversation is
# the main pane actually showing".
_CURRENT_ROW_JS = r"""
() => {
  var rows = document.querySelectorAll('button[id][aria-current="page"]');
  for (var i = 0; i < rows.length; i++) {
    var id = rows[i].id || '';
    if (/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(id)) {
      return id;
    }
  }
  return '';
}
"""


def _current_row_guid():
    """The GUID of the sidebar row marked aria-current="page" (the OPEN conversation),
    or "" if none. Never raises."""
    try:
        guid = PAGE.evaluate(_CURRENT_ROW_JS) or ""
    except Exception:
        return ""
    return guid if BARE_GUID_RE.match(guid) else ""


# ── change-based conversation capture ──────────────────────────────────────────────────────
# WHY change-based: right after /new + first send, the NEW conversation often has no sidebar
# row yet, and aria-current="page" REMAINS on the PREVIOUSLY-open row (observed live: after
# resume-to-A then /new + teach-in-B, aria-current still pointed at A and the old capture
# misattributed B's session to A's guid). Likewise the localStorage cache lags fresh
# conversations by seconds. So capture must be CHANGE-based: snapshot what exists BEFORE the
# conversation-creating send (the baseline), then accept only a guid that demonstrably
# CHANGED/appeared relative to that baseline. Ambiguity = empty: a wrong resume is strictly
# worse than no resume, so no "most recent entry" or stale-marker fallback is allowed.

# All GUID-shaped sidebar row ids currently in the DOM (any state, not just aria-current).
_ALL_ROW_GUIDS_JS = r"""
() => {
  var out = [];
  var rows = document.querySelectorAll('button[id]');
  for (var i = 0; i < rows.length; i++) {
    var id = rows[i].id || '';
    if (/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(id)) {
      out.push(id);
    }
  }
  return out;
}
"""


def _known_conv_guids():
    """The set of ALL conversation GUIDs currently known to the page: sidebar row ids plus
    the SPA's localStorage history cache. Used for the capture baseline and for detecting a
    newly appeared conversation. Never raises; partial results on error."""
    guids = set()
    try:
        for g in (PAGE.evaluate(_ALL_ROW_GUIDS_JS) or []):
            if BARE_GUID_RE.match(g or ""):
                guids.add(g)
    except Exception:
        pass
    try:
        for it in (PAGE.evaluate(_INSIGHTS_JS) or []):
            g = (it.get("conversationId") or "").strip()
            if BARE_GUID_RE.match(g):
                guids.add(g)
    except Exception:
        pass
    return guids


def select_changed_conv_guid(baseline_cur, baseline_known, now_cur, now_known):
    """PURE change-based capture selection (no browser access -- fully unit-testable).

    Given the pre-send baseline (the aria-current guid at that moment, possibly '', and the
    set of ALL conversation guids known at that moment) and the current observation, return
    the guid of the conversation that was newly created since the baseline, or '' when it
    cannot be determined unambiguously.

    Accept rules (in priority order):
      (a) the pane's aria-current guid moved to a row that did NOT exist at baseline;
      (b) exactly ONE brand-new guid appeared since the baseline.
    A stale aria-current (same as baseline) is never accepted; multiple new guids are
    ambiguous -> ''. Ambiguity = empty on purpose: a wrong resume is strictly worse than no
    resume."""
    known = set(baseline_known or ())
    if baseline_cur:
        known.add(baseline_cur)
    if now_cur and now_cur != (baseline_cur or "") and now_cur not in known:
        return now_cur
    fresh = []
    seen = set()
    for g in (now_known or ()):
        if g and g not in known and g not in seen:
            seen.add(g)
            fresh.append(g)
    if len(fresh) == 1:
        return fresh[0]
    return ""


def _record_capture_baseline():
    """Snapshot the change-based capture baseline off the live page: the current
    aria-current guid (may be '' on a truly bare pane, or a STALE previous conversation --
    that is exactly why it is recorded) and all currently-known conversation guids. Stored
    module-level (CAPTURE_BASELINE, next to ACTIVE_SID). Called when /new or startup-fresh
    puts the pane on the bare agent page, and refreshed immediately before any send for a
    session that has no conv_url yet. Never raises."""
    global CAPTURE_BASELINE
    try:
        CAPTURE_BASELINE = {"cur": _current_row_guid(), "known": _known_conv_guids()}
    except Exception:
        CAPTURE_BASELINE = {"cur": "", "known": set()}


def _capture_changed_conv_ref(poll_s=20):
    """Post-exchange capture: poll up to `poll_s` seconds for a guid that CHANGED/appeared
    relative to CAPTURE_BASELINE (see select_changed_conv_guid). Returns "sess:<guid>" or
    "" when no unambiguous new conversation could be determined (caller leaves conv_url
    empty and logs -- never guesses). Never raises."""
    base = CAPTURE_BASELINE or {"cur": "", "known": set()}
    deadline = time.time() + max(1, poll_s)
    while time.time() < deadline:
        try:
            guid = select_changed_conv_guid(
                base.get("cur", ""), base.get("known") or set(),
                _current_row_guid(), _known_conv_guids())
        except Exception:
            guid = ""
        if guid:
            return make_sessref(guid)
        try:
            PAGE.wait_for_timeout(1000)
        except Exception:
            break
    return ""


def _prepare_capture_baseline(sid):
    """Refresh the change-based capture baseline IMMEDIATELY before a send, but only for a
    session that has no conv_url yet (an already-captured session never re-captures, see
    _persist_exchange). Recording right before the conversation-creating send makes the
    baseline immune to anything that moved the pane since /new (a /history or /switch call,
    a prior session's exchange, ...). Never raises."""
    try:
        sess = S.load(sid) or {}
        if not (sess.get("conv_url") or ""):
            _record_capture_baseline()
    except Exception:
        logger.warning("prepare_capture_baseline failed for sid=%s", sid, exc_info=True)


def _persist_exchange(sid, user_msg, final_text):
    """Persist one completed exchange to the session ledger, maintaining conv_url via
    CHANGE-BASED capture. Rules (all ASCII logs):
      * session has NO conv_url: capture only a guid that changed/appeared vs the pre-send
        baseline; if ambiguous, leave conv_url EMPTY and warn (a wrong resume is strictly
        worse than no resume -- no stale-marker or most-recent-entry fallback).
      * session HAS conv_url: never overwrite. Verify the pane's aria-current still matches
        and warn on mismatch.
    Exception-guarded: a persistence hiccup must never break the chat turn."""
    try:
        S.append_turn(sid, "user", user_msg)
        S.append_turn(sid, "assistant", final_text)
        sess = S.load(sid) or {}
        existing = sess.get("conv_url") or ""
        if existing:
            expected = sessref_guid(existing)
            if expected:
                cur = _current_row_guid()
                if cur and cur != expected:
                    logger.warning(
                        "pane guid %s.. does not match session conv_url guid %s.. "
                        "(sid=%s); keeping stored conv_url", cur[:5], expected[:5], sid)
            S.touch(sid, status="active")
        else:
            ref = _capture_changed_conv_ref()
            fields = {"status": "active"}
            if ref:
                fields["conv_url"] = ref
            else:
                logger.warning(
                    "conversation capture ambiguous for sid=%s; conv_url left empty "
                    "(no changed/new guid vs baseline)", sid)
            new_sess = S.touch(sid, **fields)
            if ref:
                # This session just gained a non-empty conv_url for the first time --
                # register/refresh it in .fleet/conversations.json so FleetCockpit's
                # existing history viewer shows it too (source="chat"), with zero C#
                # change. Best-effort/exception-guarded inside the helper itself.
                register_bridge_session_in_fleet_convs(
                    sid, new_sess.get("title") or "", ref, new_sess.get("transcript") or "")
    except Exception:
        logger.warning("session persistence failed for sid=%s", sid, exc_info=True)


def _verify_pane_on_guid(guid, cur_wait=10, turns_wait=20):
    """True once the main pane VERIFIABLY shows conversation `guid`: its sidebar row
    reports aria-current="page" AND the conversation's turn blocks have rendered
    (_wait_turns, which also lets the last assistant bubble settle). Click-then-hope is not
    enough: a force-click on a still-hydrating sidebar lands but silently no-ops (observed
    live: startup resume 'succeeded', yet the next send went to a brand-new conversation
    because the pane never actually swapped). Never raises."""
    ok = False
    for _ in range(max(1, cur_wait)):
        if _current_row_guid() == guid:
            ok = True
            break
        try:
            PAGE.wait_for_timeout(500)
        except Exception:
            return False
    if not ok:
        return False
    try:
        return bool(_wait_turns(timeout=turns_wait))
    except Exception:
        return False


def _resume_to_ref(ref, settle=20):
    """Reattach PAGE to the conversation named by `ref` (a "sess:<guid>" reference from
    S.load()/S.latest_active()). Loads AGENT_URL first (so the sidebar with history rows
    renders), then clicks the sidebar row whose id == guid -- the same click mechanism
    confirmed live to switch the main pane without changing page.url. Returns (ok, reason);
    never raises.

    VERIFIED RESUME: returns ok=True only once _verify_pane_on_guid confirms the pane
    actually swapped (row aria-current="page" + turn blocks rendered). The row click is
    retried up to 3x (on a freshly loaded page the row can render BEFORE its React handler
    attaches, so an early force-click lands but does nothing -- the root cause of the
    silent startup-resume failure), and the whole navigate+find+click+verify cycle is
    retried once more on top. The turn-block wait doubles as the SEND-READINESS gate: by
    the time this returns True the conversation view is hydrated, so the composer's editor
    model is attached and the proven DRIVER.send() type/arm-wait/force-click discipline
    works. Sending into a still-hydrating view left text in the contenteditable that the
    editor model never registered -- the Send button then never truly armed and the send
    failed with 'composer still holds text after 3 attempts'."""
    guid = sessref_guid(ref)
    if not guid:
        return False, "not a sess: reference"
    last = "unknown"
    for _nav_attempt in range(2):
        try:
            if AGENT_URL:
                PAGE.goto(AGENT_URL, wait_until="domcontentloaded", timeout=25000)
            _wait_composer()
        except Exception as e:
            last = "agent page load failed: %s: %s" % (type(e).__name__, e)
            continue
        row = None
        for _ in range(max(1, settle)):
            try:
                loc = PAGE.locator('button[id="%s"]' % guid)
                if loc.count() > 0:
                    row = loc.first
                    break
            except Exception:
                pass
            PAGE.wait_for_timeout(500)
        if row is None:
            last = "conversation row not found in sidebar (guid=%s)" % guid
            continue
        click_err = None
        for _click_attempt in range(3):
            try:
                row.click(timeout=4000, force=True)
            except Exception as e:
                click_err = "%s: %s" % (type(e).__name__, e)
                try:
                    PAGE.wait_for_timeout(800)
                except Exception:
                    pass
                continue
            if _verify_pane_on_guid(guid):
                try:
                    _wait_composer(10)   # composer present post-swap before we return
                except Exception:
                    pass
                return True, "resumed"
            try:
                PAGE.wait_for_timeout(1000)
            except Exception:
                pass
        if click_err is not None:
            last = "row click failed: %s" % click_err
        else:
            last = "row clicked but pane did not swap to guid=%s" % guid
    return False, last


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


def _looks_like_consent(text: str) -> bool:
    """True if `text` (a Copilot reply) is an MCP connection-consent card -- the connector's
    connection-SELECT confirm (接続マネージャーを開く / 許可 / レビュー→送信する), NOT a
    credential sign-in. Uses the SAME markers as relay_fleet.RelayWorker (imported, not
    re-listed) so the bridge and fleet never drift on what counts as a consent card."""
    t = text or ""
    tl = t.lower()
    return any(m in t for m in CONSENT_MARKERS) or any(m in tl for m in CONSENT_MARKERS)


def _bridge_auto_consent() -> bool:
    """Auto-approve an MCP connection-consent card on the bridge's PAGE, mirroring
    RelayWorker._auto_consent's three tiers -- this is a connection-SELECT confirm, NOT a
    credential entry, so it is safe to click through with NO surface()/foreground prompt.
    REUSES relay.edge_reconnect's shared, already-proven helpers; no click logic is
    reimplemented here. Returns True iff a commit happened. Fully exception-guarded --
    never raises (a failure here must not break the chat turn)."""
    try:
        from relay.edge_reconnect import (
            click_through_consent, fix_all_stale_connections, load_conn_url,
        )
    except Exception:
        logger.warning("_bridge_auto_consent: could not import relay.edge_reconnect helpers")
        return False
    pg = PAGE
    if pg is None:
        return False
    # Tier 0: an Allow (許可/Allow) button directly on the current page/card -> one click.
    try:
        for label in ("許可", "Allow"):
            btn = pg.locator('button:has-text("%s")' % label)
            if btn.count():
                btn.first.click()
                pg.wait_for_timeout(4000)
                return True
    except Exception:
        pass
    # Tier 1: DIRECT-HIT a cached connection-manager URL (skip if it now redirects to login --
    # that would be a genuine sign-in event, which this path must never surface for).
    try:
        url = load_conn_url()
        if url:
            from relay.edge_recover import looks_like_login
            np = None
            try:
                np = pg.context.new_page()
                np.goto(url, wait_until="domcontentloaded", timeout=45000)
                np.wait_for_timeout(6000)
                if not looks_like_login(np.url or ""):
                    res = fix_all_stale_connections(np)
                    if res.get("submitted", 0) > 0 or not res.get("stale_left", True):
                        try:
                            pg.bring_to_front()
                        except Exception:
                            pass
                        return True
                # cached URL 404'd / redirected to login -> fall through to Tier 2, which
                # refreshes the cache. Deliberately do NOT clear the cache here.
            finally:
                if np is not None:
                    try:
                        np.close()
                    except Exception:
                        pass
    except Exception:
        pass
    # Tier 2: popup flow (also (re)caches the URL and fixes ALL stale rows).
    try:
        return bool(click_through_consent(pg))
    except Exception:
        return False


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
        global ACTIVE_SID
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
            # DELIBERATE PAGE_LOCK BYPASS: /review and /security-review shell out to
            # bench/review_run.py, which fans its work out over the M365 fleet on the
            # SEPARATE :9222 Edge (relay.fleet_runner) -- it never touches this bridge's own
            # PAGE/DRIVER (:9223). Routing a multi-minute review through
            # run_on_page_thread + PAGE_LOCK would needlessly freeze every other /stream,
            # /goal, and /history request behind it for the whole run, for no reason (there
            # is no shared page state to protect). Handle it directly on this request
            # thread instead, before the PAGE_LOCK acquire below.
            _peek = msg.strip().lower()
            # ORDER MATTERS: "/review-fix".startswith("/review") is True, so the more-specific
            # /review-fix (and /reviewfix) prefix MUST be checked before the plain /review
            # prefix below, or a naive /review check would swallow every /review-fix call.
            if _peek.startswith("/review-fix") or _peek.startswith("/reviewfix"):
                self._review_fix_stream(msg)
                return
            if (_peek.startswith("/review") or _peek.startswith("/security-review")
                    or _peek.startswith("/securityreview")):
                self._review_stream(msg)
                return
            if not PAGE_LOCK.acquire(blocking=False):
                self._json({"ok": False, "error": "busy"}); return
            try:
                # PAGE-touching: _stream ends up calling _send_and_stream_once, which polls
                # PAGE/DRIVER AND writes SSE chunks to self.wfile in the same loop -- so the
                # whole call runs on the page-owner thread (see PageExecutor's docstring for
                # why: Playwright's sync API is thread-bound). self.wfile.write is a plain
                # socket call, safe to run from that thread too.
                run_on_page_thread(self._stream, msg)
            finally:
                PAGE_LOCK.release()
            return
        if parsed.path == "/goal":         # WORK MODE: autonomous multi-turn loop (SSE)
            if not PAGE_LOCK.acquire(blocking=False):
                self._json({"ok": False, "error": "busy"}); return
            try:
                qs = urllib.parse.parse_qs(parsed.query)
                resume_flag = (qs.get("resume") or [""])[0] in ("1", "true", "True")
                text = (qs.get("text") or [""])[0]
                max_turns_raw = (qs.get("max_turns") or [""])[0]
                try:
                    max_turns = int(max_turns_raw) if max_turns_raw else DEFAULT_MAX_TURNS
                except ValueError:
                    max_turns = DEFAULT_MAX_TURNS
                # FROZEN CONTRACT ADDITIONS: &ac=<urlencoded acceptance criteria> and
                # &max_loops=<int, default 3>. Verification runs only when ac is non-empty
                # (see _goal's docstring) -- an absent/empty ac is fully backward compatible.
                ac = (qs.get("ac") or [""])[0]
                max_loops_raw = (qs.get("max_loops") or [""])[0]
                try:
                    max_loops = int(max_loops_raw) if max_loops_raw else DEFAULT_MAX_LOOPS
                except ValueError:
                    max_loops = DEFAULT_MAX_LOOPS
                run_on_page_thread(self._goal, text, max_turns, resume_flag, ac, max_loops)
            finally:
                PAGE_LOCK.release()
            return
        if parsed.path == "/stop":         # cooperative stop for the running /goal loop
            global STOP_REQUESTED
            STOP_REQUESTED = True
            self._json({"ok": True})
            return
        if parsed.path == "/new":          # start a fresh Copilot conversation
            if not PAGE_LOCK.acquire(blocking=False):
                self._json({"ok": False, "error": "busy"}); return
            try:
                run_on_page_thread(self._do_new, parsed)
            finally:
                PAGE_LOCK.release()
            return
        if parsed.path == "/conv":         # current conversation URL (for saving)
            if not PAGE_LOCK.acquire(blocking=False):
                self._json({"ok": False, "error": "busy"}); return
            try:
                run_on_page_thread(lambda: self._json({"url": PAGE.url}))
            finally:
                PAGE_LOCK.release()
            return
        if parsed.path == "/switch":       # continue a saved conversation
            if not PAGE_LOCK.acquire(blocking=False):
                self._json({"ok": False, "error": "busy"}); return
            try:
                run_on_page_thread(self._do_switch, parsed)
            finally:
                PAGE_LOCK.release()
            return
        if parsed.path == "/sessions":     # list known sessions (newest-first, capped)
            qs = urllib.parse.parse_qs(parsed.query)
            want_all = (qs.get("all") or [""])[0] in ("1", "true", "True")
            try:
                sessions = S.list_sessions()[:50]
                for s in sessions:
                    s.setdefault("source", "chat")
                if want_all:
                    sessions = sessions + _load_fleet_sessions_view()
                    sessions.sort(key=lambda s: s.get("last_active_ts", 0), reverse=True)
                    sessions = sessions[:50]
            except Exception as e:
                self._json({"ok": False, "error": str(e)}); return
            self._json({"sessions": sessions})
            return
        if parsed.path == "/adopt":        # adopt an external (typically fleet) conversation
            if not PAGE_LOCK.acquire(blocking=False):
                self._json({"ok": False, "error": "busy"}); return
            try:
                run_on_page_thread(self._do_adopt, parsed)
            finally:
                PAGE_LOCK.release()
            return
        if parsed.path == "/resume":       # reattach to a known session by sid
            if not PAGE_LOCK.acquire(blocking=False):
                self._json({"ok": False, "error": "busy"}); return
            try:
                run_on_page_thread(self._do_resume, parsed)
            finally:
                PAGE_LOCK.release()
            return
        if parsed.path == "/send":         # STORE-ONLY: always queues, never touches PAGE
            sid = (urllib.parse.parse_qs(parsed.query).get("sid") or [""])[0]
            msg = (urllib.parse.parse_qs(parsed.query).get("msg") or [""])[0]
            if not msg.strip():
                self._json({"ok": False, "error": "empty msg"}); return
            sid = sid or ACTIVE_SID
            if not sid:
                sid = S.new_session()["sid"]
                ACTIVE_SID = sid
            # /send is a STORE-ONLY endpoint (per the concurrency contract: /send, /stop,
            # /sessions, / never touch PAGE and run lock-free) -- it must stay responsive
            # while a long /goal run (or any other PAGE-touching request) holds PAGE_LOCK.
            # It ALWAYS queues via session_store and returns immediately; a running /goal
            # loop drains the queue at its next turn boundary (steering), and a plain
            # /stream turn drains it via _drain_pending_queue right after that turn
            # completes -- both existing drain paths are unchanged. No run_on_page_thread
            # here: this endpoint never touches PAGE/DRIVER.
            try:
                _queue_input_locked(sid, msg)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}); return
            self._json({"ok": True, "queued": True, "sid": sid})
            return
        if parsed.path == "/history":      # scrape ALL turns of a conversation in order
            if not PAGE_LOCK.acquire(blocking=False):
                self._json({"ok": False, "error": "busy"}); return
            try:
                run_on_page_thread(self._do_history, parsed)
            finally:
                PAGE_LOCK.release()
            return
        if parsed.path == "/delete":       # best-effort: delete the Copilot conversation
            if not PAGE_LOCK.acquire(blocking=False):
                self._json({"ok": False, "error": "busy"}); return
            try:
                run_on_page_thread(self._do_delete, parsed)
            finally:
                PAGE_LOCK.release()
            return
        if parsed.path == "/agent_conversations":
            # READ-ONLY: scrape the agent's own conversation rail (guid + title). Lists
            # orphans not in the local registry. Deletes nothing. Scope = current agent.
            if not PAGE_LOCK.acquire(blocking=False):
                self._json({"ok": False, "error": "busy"}); return
            try:
                run_on_page_thread(self._do_agent_conversations)
            finally:
                PAGE_LOCK.release()
            return
        if parsed.path == "/upload":       # attach a local file/image to the composer
            if not PAGE_LOCK.acquire(blocking=False):
                self._json({"ok": False, "error": "busy"}); return
            try:
                run_on_page_thread(self._do_upload, parsed)
            finally:
                PAGE_LOCK.release()
            return
        self.send_response(404)
        self.end_headers()

    # ── PAGE-touching branch bodies, each run on the page-owner thread via
    # run_on_page_thread (see PageExecutor's docstring). Split out of do_GET one-per-endpoint
    # purely to keep do_GET's dispatch table readable; behavior is unchanged from the inline
    # bodies this replaced.

    def _do_new(self, parsed):
        global ACTIVE_SID
        title = (urllib.parse.parse_qs(parsed.query).get("title") or [""])[0]
        ok = False
        try:
            if AGENT_URL:
                PAGE.goto(AGENT_URL, wait_until="domcontentloaded")
                ok = _wait_composer()
        except Exception as e:
            self._json({"ok": False, "error": str(e)}); return
        # Change-based capture baseline: the pane is now on the bare agent page, but
        # aria-current can STILL mark the previously-open conversation's row (observed
        # live) -- that stale marker plus all currently-known guids IS the baseline the
        # post-send capture diffs against. Refreshed again right before the send.
        _record_capture_baseline()
        sess = S.new_session(title=title)
        ACTIVE_SID = sess["sid"]
        self._json({"ok": ok, "url": PAGE.url, "sid": ACTIVE_SID})

    def _do_switch(self, parsed):
        url = (urllib.parse.parse_qs(parsed.query).get("url") or [""])[0]
        ok = False
        try:
            _reap_orphan_tabs()
            if url:
                ok = _goto_settled(url)     # recover from SSO-redirect landings
        except Exception as e:
            self._json({"ok": False, "error": str(e)}); return
        self._json({"ok": ok, "url": PAGE.url})

    def _do_adopt(self, parsed):
        """GET /adopt?url=<urlencoded>&title=<t> -- adopt an EXTERNAL conversation
        (typically a fleet one, from .fleet/conversations.json) into a brand-new
        interactive session. Navigates via the existing _goto_settled machinery (the
        same recovery path /switch and /resume's conv_url branch already use -- no new
        navigation logic), then tries to capture the sidebar's aria-current guid so
        future resumes use the fast/verified sess:<guid> path; falls back to the raw
        URL (still resumable via _do_resume's kind=="conv_url" branch -> _goto_settled)
        if no guid can be confirmed. Returns {"ok":true,"sid":...,"ref_kind":"guid"|"url"}
        or {"ok":false,"error":...}."""
        global ACTIVE_SID
        qs = urllib.parse.parse_qs(parsed.query)
        url = (qs.get("url") or [""])[0]
        title = (qs.get("title") or [""])[0]
        if not url:
            self._json({"ok": False, "error": "missing url"}); return
        try:
            _reap_orphan_tabs()
            ok = _goto_settled(url)
        except Exception as e:
            self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}); return
        if not ok:
            self._json({"ok": False, "error": "navigation did not settle on the conversation"})
            return
        # A URL-opened conversation should mark its sidebar row aria-current="page" --
        # reuse the existing capture helper (no new DOM logic). Best-effort: absence of
        # a guid is not a failure, just a less-durable stored reference.
        guid = ""
        try:
            guid = _current_row_guid()
        except Exception:
            guid = ""
        ref_kind = "guid" if guid else "url"
        conv_ref = make_sessref(guid) if guid else url
        sess = S.new_session(title=title or "")
        sid = sess["sid"]
        new_sess = S.touch(sid, conv_url=conv_ref, status="active", source="chat")
        ACTIVE_SID = sid
        # This session gained a non-empty conv_url immediately (not via the normal
        # exchange path) -- register it into .fleet/conversations.json the same way
        # _persist_exchange does, so it shows up in FleetCockpit's history viewer too.
        register_bridge_session_in_fleet_convs(
            sid, new_sess.get("title") or "", conv_ref, new_sess.get("transcript") or "")
        self._json({"ok": True, "sid": sid, "ref_kind": ref_kind})

    def _do_resume(self, parsed):
        global ACTIVE_SID
        sid = (urllib.parse.parse_qs(parsed.query).get("sid") or [""])[0]
        if not sid:
            self._json({"ok": False, "error": "missing sid"}); return
        sess = S.load(sid)
        if sess is None:
            self._json({"ok": False, "error": "unknown sid"}); return
        ref = sess.get("conv_url") or ""
        kind = classify_conv_ref(ref)
        try:
            _reap_orphan_tabs()
            if kind == "sessref":
                ok, reason = _resume_to_ref(ref)
            elif kind == "conv_url":
                ok = _goto_settled(ref)
                reason = "ok" if ok else "navigation did not settle on the conversation"
            else:
                ok, reason = False, "session has no reattachable conv_url"
        except Exception as e:
            self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}); return
        if ok:
            ACTIVE_SID = sid
            S.touch(sid, status="active")
            self._json({"ok": True, "sid": sid})
        else:
            self._json({"ok": False, "error": reason})

    def _do_history(self, parsed):
        url = (urllib.parse.parse_qs(parsed.query).get("url") or [""])[0]
        try:
            _reap_orphan_tabs()
            if url:
                # bounded for an interactive READ: ~10s composer wait, 2 tries -> an
                # unreachable conversation returns empty in ~25s instead of hanging
                # the bridge for minutes.
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
        # Surface the truncation sentinel as a top-level field so callers can act on
        # it without having to scan the message list. The sentinel record itself is
        # stripped from the messages array (it is a meta record, not a real message).
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

    def _do_delete(self, parsed):
        q = urllib.parse.parse_qs(parsed.query)
        url = (q.get("url") or [""])[0]
        title = (q.get("title") or [""])[0]
        try:
            ok, reason = _try_delete_conversation(url, title)
        except Exception as e:
            ok, reason = False, str(e)
        # expose the reason under BOTH keys so the UI can surface it (it was dropped before)
        self._json({"ok": ok, "error": reason, "reason": reason, "guid": _conv_guid(url)})

    def _do_agent_conversations(self):
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

    def _do_upload(self, parsed):
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
            self._sse({"delta": _current_help_text()}); self._sse({}, "done"); return
        if token == "skills":
            self._sse({"delta": format_skill_list(SKILL_STORE.list_metadata())})
            self._sse({}, "done"); return
        if token in ("skill-approve", "skill-import", "skill-create", "gate-answer"):
            self._sse({"delta": (
                "この管理コマンドは、人間だけが操作できるローカル端末で実行してください。"
                "モデルやWeb会話から承認状態を変更することはできません。"
            )})
            self._sse({}, "done"); return
        if token in ("research", "deepresearch", "dr"):
            self._delegate("researcher", arg); return
        if token in ("analyze", "an"):
            self._delegate("analyst", arg); return
        if token in ("review-fix", "reviewfix"):
            # Defensive fallback only -- see the review/security-review branch below for why:
            # the normal entry point is do_GET's /stream peek (checked BEFORE the plain
            # /review peek there), which calls _review_fix_stream directly.
            self._review_fix_stream(cmd if cmd.startswith("/") else "/" + cmd)
            return
        if token in ("review", "security-review", "securityreview",
                     "deep-review", "deepreview", "deep-security-review", "deepsecurityreview",
                     "review-2", "review2", "security-review-2", "securityreview-2",
                     "securityreview2"):
            # Defensive fallback only: the normal entry point is do_GET's /stream peek,
            # which calls _review_stream directly (bypassing PAGE_LOCK) before ever reaching
            # _command. This branch only fires if some other caller routes a review command
            # through _command/_stream instead -- it must not double-dispatch with the
            # do_GET peek, which always returns before falling through to _stream/_command.
            self._review_stream(cmd if cmd.startswith("/") else "/" + cmd)
            return
        if token in PROMPT_TEMPLATES:           # prompt-template -> normal streaming path
            usage, build = PROMPT_TEMPLATES[token]
            if not arg.strip():
                self._sse({"delta": "使い方: " + usage}); self._sse({}, "done"); return
            self._stream_text(build(arg.strip()))
            return
        try:
            skill = SKILL_STORE.get(token)
        except SkillError:
            skill = None
        if skill is not None:
            if skill.metadata.get("user-invocable", True) is False:
                self._sse({"delta": f"Skill /{token} は明示呼び出しが無効です。"})
                self._sse({}, "done"); return
            try:
                prompt = SKILL_STORE.render(token, arg)
            except SkillError as exc:
                self._sse({"delta": f"Skill /{token} を読み込めません: {exc}"})
                self._sse({}, "done"); return
            self._stream_text(BRIDGE_DISCIPLINE + prompt)
            return
        self._sse({"delta": "未知のコマンド `" + head + "`。`/help` で一覧を表示します。"})
        self._sse({}, "done")

    def _delegate(self, kind, arg):
        """Run a /research or /analyze command by delegating to the Researcher
        (Claude) or Analyst agent on a side page; stream the report back.

        No BUSY re-entrancy guard needed here: the caller chain (do_GET's /stream and /goal
        handlers) already holds PAGE_LOCK for the whole request, so a second concurrent call
        into this method can never happen -- PAGE_LOCK IS the serialization point now."""
        if not arg:
            usage = "/research <調べたいこと>" if kind == "researcher" else "/analyze <絶対パス> | <分析指示>"
            self._sse({"delta": "使い方: " + usage}); self._sse({}, "done"); return
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
        matched = SKILL_STORE.match(msg)
        if matched:
            try:
                skill_prompt = SKILL_STORE.render(matched["name"], msg)
                self._stream_text(
                    BRIDGE_DISCIPLINE + skill_prompt + "\n\nOriginal user request:\n" + msg
                )
                return
            except SkillError:
                # A concurrent edit can invalidate the digest between match and load.
                pass
        self._stream_text(BRIDGE_DISCIPLINE + msg)

    def _review_stream(self, msg: str):
        """Handle /review and /security-review: shell out to bench/review_run.py, which
        fans the work out over the M365 fleet on the separate :9222 Edge, and stream a
        compact summary back into the chat once it finishes.

        Called DIRECTLY from do_GET's /stream peek, on the request thread -- NOT via
        run_on_page_thread, and NOT under PAGE_LOCK (see the bypass comment at that call
        site). This method never touches PAGE/DRIVER; it only launches and reads a
        subprocess, so it is safe to run concurrently with ordinary /stream, /goal, and
        /history requests. Writes the SAME SSE preamble as _stream so the client side is
        indistinguishable. Exception-guarded end-to-end: nothing here is allowed to raise
        into the request thread.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            venvpy = os.path.join(repo_root, ".venv", "Scripts", "python.exe")

            if not os.path.exists(venvpy):
                self._sse({"delta": ".venv python が見つかりません。quickstart.bat を実行してください。"})
                self._sse({}, "done")
                return

            parsed = review_command.parse_review_command(msg)
            p2c_level = _p2c_review_level()
            if parsed.get("resilience") and p2c_level == 0:
                self._sse({"delta": (
                    "深掘りレビューは無効です。.env の MCP_REVIEW_P2C=1（深掘り）または "
                    "2（フル検証）に変更して "
                    "start_all.bat を再実行すると /deep-review と /deep-security-review が使えます。"
                )})
                self._sse({}, "done")
                return
            argv = review_command.build_review_argv(
                parsed, repo_root, venvpy, p2c_level=p2c_level)

            self._sse({"delta": "レビューを開始します（数分〜）...\n"})

            proc = subprocess.Popen(
                argv, cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )

            # Read the subprocess's stdout on a background thread so this thread can keep
            # pinging the SSE connection (to detect an Esc/Stop disconnect promptly, exactly
            # like _send_and_stream_once) even while review_run.py is silently working
            # between print()s for minutes at a time.
            line_q: "queue.Queue[str | None]" = queue.Queue()

            def _reader():
                try:
                    for line in proc.stdout:
                        line_q.put(line)
                except Exception:
                    pass
                finally:
                    line_q.put(None)   # sentinel: stdout closed / reader done

            reader_thread = threading.Thread(target=_reader, daemon=True)
            reader_thread.start()

            forward_prefixes = ("fleet:", "launching", "goals:", "report:", "summary:")
            full_lines = []
            while True:
                try:
                    line = line_q.get(timeout=5.0)
                except queue.Empty:
                    self._ping()             # detect Esc/Stop disconnect promptly
                    continue
                if line is None:
                    break
                full_lines.append(line)
                stripped = line.strip()
                if stripped.lower().startswith(forward_prefixes):
                    self._sse({"delta": stripped + "\n"})

            proc.wait()
            full_stdout = "".join(full_lines)

            info = review_command.parse_run_output(full_stdout)
            report_md = info.get("report_md")
            agg_json = None
            if report_md:
                try:
                    json_path = report_md[:-3] + ".json" if report_md.endswith(".md") \
                        else report_md + ".json"
                    with open(json_path, encoding="utf-8") as f:
                        agg_json = json.load(f)
                except Exception:
                    agg_json = None

            summary_text = review_command.format_review_summary(
                parsed.get("kind", "review"), info.get("counts") or {}, agg_json, report_md,
            )
            self._sse({"delta": summary_text})
            self._sse({}, "done")
        except Exception as e:
            try:
                self._sse({"delta": "レビュー実行中にエラー: " + type(e).__name__})
                self._sse({}, "done")
            except Exception:
                pass

    def _run_fix_subprocess(self, argv, repo_root, forward_prefixes):
        """Launch `argv` (a bench/review_fix.py invocation) as a subprocess, forward stdout
        lines whose lowercased/stripped text starts with any of forward_prefixes as SSE
        deltas, and return the FULL captured stdout (every line, forwarded or not) as one
        string once the process exits.

        Mirrors _review_stream's reader-thread/ping loop (read the subprocess's stdout on a
        background thread so this thread can keep pinging the SSE connection every 5s --
        detecting an Esc/Stop disconnect promptly -- even while review_fix.py is silently
        working between print()s for minutes at a time). Factored out here (rather than
        duplicated inline) because /review-fix needs to run a subprocess TWICE per full
        cycle (the --dry-run plan, then later the real fix) and both need identical
        streaming behavior, just different forward_prefixes.
        """
        proc = subprocess.Popen(
            argv, cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        line_q: "queue.Queue[str | None]" = queue.Queue()

        def _reader():
            try:
                for line in proc.stdout:
                    line_q.put(line)
            except Exception:
                pass
            finally:
                line_q.put(None)   # sentinel: stdout closed / reader done

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        full_lines = []
        while True:
            try:
                line = line_q.get(timeout=5.0)
            except queue.Empty:
                self._ping()             # detect Esc/Stop disconnect promptly
                continue
            if line is None:
                break
            full_lines.append(line)
            stripped = line.strip()
            if stripped.lower().startswith(forward_prefixes):
                self._sse({"delta": stripped + "\n"})

        proc.wait()
        return "".join(full_lines)

    def _review_fix_stream(self, msg: str):
        """Handle /review-fix: a two-step-confirm wrapper around bench/review_fix.py, which
        (unlike /review and /security-review) actually EDITS the user's files -- so it is
        NEVER allowed to run on the first message.

        Called DIRECTLY from do_GET's /stream peek (checked BEFORE the plain /review peek
        there -- see the ordering comment at that call site, since
        "/review-fix".startswith("/review") is true), on the request thread -- NOT via
        run_on_page_thread, and NOT under PAGE_LOCK, for the same reason /review bypasses
        it: review_fix.py shells out to the M365 fleet on the separate :9222 Edge and
        never touches this bridge's own PAGE/DRIVER (:9223). Writes the SAME SSE preamble as
        _stream/_review_stream so the client side is indistinguishable.

        Two-step confirm (see _REVIEW_FIX_PENDING's module-level docstring for the state
        shape):
          - parsed["confirm"] is False (STEP 1, plan): runs bench/review_fix.py --dry-run,
            which review_fix.py's own --dry-run branch guarantees is read-only (plan only,
            no backup, no git, fleet NOT launched -- see its "DRY RUN -- plan only..." print).
            Streams the plan, then ARMS _REVIEW_FIX_PENDING = {"ts": now, "parsed": parsed}
            and appends the confirm instruction. NO file edits happen on this path, ever.
          - parsed["confirm"] is True (STEP 2, execute): only proceeds if
            _REVIEW_FIX_PENDING was armed within REVIEW_FIX_CONFIRM_WINDOW_SEC seconds of
            now; otherwise tells the user to re-run /review-fix first (no fix runs). If
            still within the window, clears _REVIEW_FIX_PENDING FIRST -- before launching
            the subprocess -- so a duplicate/concurrent "/review-fix confirm" can never
            double-launch a second real fix off the same armed plan (single-use arm).
            Then runs the REAL (non-dry-run) fix and posts the final structured summary
            (files changed, backup path, exact undo instruction) built from its stdout.

        Exception-guarded end-to-end: nothing here is allowed to raise into the request
        thread.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            global _REVIEW_FIX_PENDING
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            venvpy = os.path.join(repo_root, ".venv", "Scripts", "python.exe")

            if not os.path.exists(venvpy):
                self._sse({"delta": ".venv python が見つかりません。quickstart.bat を実行してください。"})
                self._sse({}, "done")
                return

            parsed = review_command.parse_review_fix_command(msg)

            if not parsed["confirm"]:
                # STEP 1: plan only, via --dry-run. review_fix.py's --dry-run branch never
                # writes a backup, never touches git, and never launches the fleet -- so
                # this whole branch is guaranteed read-only regardless of what its stdout
                # says.
                argv = review_command.build_review_fix_argv(
                    parsed, repo_root, venvpy, dry_run=True)
                self._sse({"delta": "修正プランを作成します（--dry-run、ファイルは変更されません）...\n"})
                full_stdout = self._run_fix_subprocess(argv, repo_root, (
                    "dry run", "report:", "findings", "goals", "fleet cmd:", "git bonus",
                ))
                if not full_stdout.strip():
                    self._sse({"delta": "(出力なし。まず /review でレポートを作成してください。)\n"})

                self._sse({"delta": (
                    "\nバックアップ先: .fleet/review_fix/backup_<timestamp>/ "
                    "(実行時に自動作成、undo_<timestamp>.bat をダブルクリックで元に戻せます)\n"
                )})

                _REVIEW_FIX_PENDING = {"ts": time.time(), "parsed": parsed}
                self._sse({"delta": (
                    "⚠ これはファイルを編集します。実行するには %d 秒以内に "
                    "`/review-fix confirm` と入力してください。"
                ) % REVIEW_FIX_CONFIRM_WINDOW_SEC})
                self._sse({}, "done")
                return

            # STEP 2: execute for real -- but only within the confirm window of an armed plan.
            pending = _REVIEW_FIX_PENDING
            now = time.time()
            if not pending.get("parsed") or \
                    (now - pending.get("ts", 0.0)) > REVIEW_FIX_CONFIRM_WINDOW_SEC:
                self._sse({"delta": "先に `/review-fix` を実行して内容を確認してください。"})
                self._sse({}, "done")
                return

            fix_parsed = pending["parsed"]
            _REVIEW_FIX_PENDING = {"ts": 0.0, "parsed": None}   # single-use: clear before running

            argv = review_command.build_review_fix_argv(
                fix_parsed, repo_root, venvpy, dry_run=False)
            self._sse({"delta": "修正を実行します（ファイルを編集します。数分〜）...\n"})
            full_stdout = self._run_fix_subprocess(argv, repo_root, (
                "fleet:", "launching", "goals:", "backup", "backed", "undo", "report",
                "fix report", "warning", "summary", "変更", "バックアップ",
            ))

            info = review_command.parse_fix_run_output(full_stdout)
            summary_text = review_command.format_fix_summary(info)
            self._sse({"delta": summary_text})
            self._sse({}, "done")
        except Exception as e:
            try:
                self._sse({"delta": "修正実行中にエラー: " + type(e).__name__})
                self._sse({}, "done")
            except Exception:
                pass

    def _send_and_stream_once(self, msg: str, stream_out: bool = True) -> "str | None":
        """Send `msg`, stream the growing/settled answer over SSE (unless stream_out=False,
        used for the SILENT re-send after auto-consent so the consent card's own text is
        never shown to the user), and return the final cleaned answer text (or None on the
        outer-loop timeout / empty-body path). Raises on a driver/page error -- the caller
        (_stream_text) owns the Esc/Stop-button and error-SSE handling, exactly as before
        this was split out of _stream_text's inline loop."""
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
            if stream_out and not _is_proc(partial) and not _is_search_status(partial) and len(partial) > sent:
                self._sse({"delta": partial[sent:]})
                sent = len(partial)
            # lastChatMessage populated -- but it can KEEP GROWING after it first
            # appears, so finishing immediately truncates the tail. Stream its growth
            # and only finish once it has been STABLE for ~1.2s.
            if final and not _is_proc(final):
                stable_text, stable_since = final, time.time()
                while time.time() - t0 < 600:
                    if stream_out and len(final) > sent:
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
                # authoritative final: the CLEAN body, regardless of any streaming artifacts
                # (placeholder->answer cursor corruption, leaked loading lines).
                return _clean_answer_text() or final
            time.sleep(0.3)
            self._ping()                     # detect Esc/Stop disconnect promptly
        # outer-loop timeout end: same authoritative-final read
        return _clean_answer_text()

    def _consent_last_resort_surface(self) -> bool:
        """LAST RESORT for MCP connection-consent, fired only after every automatic tier
        (_bridge_auto_consent's three click-through tiers, tried once, plus one silent
        auto-approved retry) has genuinely failed. Surfaces the BRIDGE's own dedicated Edge
        (port 9223, profile copilot-bridge-edge -- NOT the fleet's :9222) pointed at the
        conversation this bridge is actually driving, so the user can approve by hand on the
        right chat rather than the launcher's default top page.

        BUG 2 fix: this used to be one-shot per PROCESS (a module bool latched True before the
        attempt even ran), so a single transient failure permanently disabled surfacing for the
        rest of the process's -- possibly multi-day -- uptime. It now delegates the actual
        attempt to _consent_surface_attempt(), which latches "already surfaced" ONLY on a
        TRUTHFUL success and bounds retries to CONSENT_SURFACE_RETRY_MAX per consent episode
        (see that function's docstring). This method's own job is just the user-facing SSE
        messaging around that shared core. Returns True iff a headed window was truthfully
        confirmed up (gates the SSE message: only ever claims "surfaced" when surface() actually
        returned True, or a prior call this episode already did) -- the caller falls back to its
        own honest chat error when this returns False. Never raises into the turn."""
        ok = _consent_surface_attempt("interactive /stream turn")
        try:
            if ok:
                self._sse({"replace": "接続の自動承認に失敗しました。専用Edge (:9223) を前面に出しました。"
                                       "表示された画面で接続を許可してから、もう一度お試しください。"})
            else:
                # TERMINAL HONESTY: exact manual recovery command, unmissable in the chat line
                # (also recorded to .fleet/tool_probe.json kind="consent_unrecoverable" once
                # the retry budget is truly exhausted -- see _record_consent_unrecoverable).
                self._sse({"replace": "接続の自動承認・自動表示に失敗しました。手動で次のコマンドを"
                                       "実行してください: python -m relay.edge_reconnect --cdp-url "
                                       "http://127.0.0.1:9223 （または PowerShell から scripts\\"
                                       "start_companion_edge.ps1 -Foreground -Port 9223 を実行して"
                                       "専用Edge(:9223)で接続を許可してください）。その後、もう一度"
                                       "お試しください。"})
            self._sse({}, "done")
        except Exception:
            pass
        return ok

    def _run_one_turn(self, sid, msg, stream_out=True):
        """Send `msg`, stream the growing/settled answer over the already-open SSE response
        (unless stream_out=False), and return the final answer text -- or a sentinel dict
        {"consent_failed": True} if an MCP connection-consent card could not be auto-approved
        (caller decides how to surface that; WORK MODE routes it into an error-turn instead of
        an SSE 'done' + honest chat error / surface(), which is /stream's own UI-facing
        behavior and would be wrong to run mid-goal-loop).

        This is the shared completion-machinery helper _stream_text and the work-mode loop
        both call: send + stream + the exact same three-tier consent auto-approval as before
        (reusing relay.edge_reconnect's shared click-through helpers, no click logic
        reimplemented). It does NOT persist the exchange and does NOT emit the SSE 'done'
        event -- those differ between a single /stream turn and a /goal turn, so callers own
        both. Raises on a driver/page error, exactly as _send_and_stream_once did before this
        was split out of _stream_text -- callers own the Esc/Stop-button + error-SSE handling.
        """
        global _LAST_USER_TURN_TS
        _LAST_USER_TURN_TS = time.time()   # see its module-level docstring: the tool probe reads this
        _prepare_capture_baseline(sid)
        final = self._send_and_stream_once(msg, stream_out=stream_out)
        if final is not None and _looks_like_consent(final):
            # Consent card, not a real answer -- do NOT show it to the user; auto-approve and
            # retry SILENTLY first (this is a connection-SELECT confirm, not a credential
            # event, so the fully-automatic tiers are tried before any surface()).
            consented = False
            try:
                consented = _bridge_auto_consent()
            except Exception:
                logger.warning("_bridge_auto_consent raised; treating as failed", exc_info=True)
                consented = False
            if consented:
                try:
                    final = self._send_and_stream_once(msg, stream_out=stream_out)
                except Exception:
                    final = None
                if final is not None and _looks_like_consent(final):
                    logger.warning("consent card persisted after auto-consent retry")
                    return {"consent_failed": True}
                # BUG 2 support: consent is genuinely resolved now (auto-consent worked and the
                # retry did not hit another card) -- end the surface-retry episode so a LATER
                # consent card starts with its own full retry budget, not an exhausted one.
                _reset_consent_surface_episode()
            else:
                logger.warning("_bridge_auto_consent: all tiers failed for a consent card")
                return {"consent_failed": True}
        elif final is not None:
            # Normal answer, no consent issue at all -- also a clean episode boundary.
            _reset_consent_surface_episode()
        return final

    def _stream_text(self, msg: str):
        """Send `msg` to the agent and stream the answer back over the ALREADY-open
        SSE response (the normal send/stream path). Used both for plain messages and
        for prompt-template slash commands so templated answers stream like a normal
        turn. Always emits a terminating `done` event.

        No BUSY re-entrancy guard needed here: the caller chain (do_GET's /stream handler)
        already holds PAGE_LOCK for the whole request, so a second concurrent call into this
        method can never happen -- PAGE_LOCK IS the serialization point now.

        MCP CONNECTION-CONSENT: send/stream/auto-consent-retry is delegated to _run_one_turn
        (the machinery shared with the work-mode /goal loop). If _run_one_turn reports the
        consent card could not be auto-approved, this is the ONE caller that still surfaces
        the dedicated Edge as a last resort (a /goal turn instead reports it as an error-turn
        and keeps looping -- see _goal_run_turn)."""
        global ACTIVE_SID
        if not ACTIVE_SID:
            ACTIVE_SID = S.new_session()["sid"]
            logger.info("no active session -- created %s", ACTIVE_SID)
        sid = ACTIVE_SID
        try:
            final = self._run_one_turn(sid, msg)
            if isinstance(final, dict) and final.get("consent_failed"):
                if not self._consent_last_resort_surface():
                    self._sse({"replace": "接続の自動承認に失敗しました。再試行してください。"})
                    self._sse({}, "done")
                return
            if final:
                self._sse({"replace": final})
                # SESSION LIFECYCLE: only a genuine (non-consent-card) answer is worth
                # persisting. Change-based capture / verify / warn all live in
                # _persist_exchange (exception-guarded there).
                _persist_exchange(sid, msg, final)
            self._sse({}, "done")
            self._drain_pending_queue(sid)
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

    def _run_work_phase(self, sid, msg, max_turns, turn_start=0):
        """WORK MODE inner loop, factored out of _goal so the verified-loop can re-enter it
        (with a continuation message) after a fail verdict WITHOUT resetting the turn counter
        -- the mission spec requires "turn budget continues from where it was; max_turns still
        applies globally" across a work -> verify -> work re-entry. `turn_start` is the number
        of turns already consumed by earlier phases of THIS /goal call (0 on the first call).

        Returns (outcome, turn, last_turn_text): `outcome` is one of decide_outcome's values
        ("done"/"error"/"stopped"/"max_turns") -- note "done" here means a DONE-marker turn was
        produced, NOT that verification (if any) has run; the caller (_goal) decides what to do
        next. `last_turn_text` is the stripped text of the turn that ended the phase (the
        DELIVERABLE, when outcome=="done"), or "" if the phase ended some other way.

        Behavior for turn error / consent_failed / normal turns is UNCHANGED from the original
        inline loop this replaced -- only the turn-counter seeding and the return shape differ."""
        global STOP_REQUESTED
        turn = turn_start
        consecutive_errors = 0
        outcome = None
        last_turn_text = ""
        while True:
            turn += 1
            try:
                final = self._run_one_turn(sid, msg)
            except Exception as e:
                try:
                    PAGE.locator(COPILOT_SELECTORS["stop_button"]).first.click(timeout=3000)
                except Exception:
                    pass
                logger.warning("goal loop: turn %d raised: %s: %s", turn, type(e).__name__, e,
                               exc_info=True)
                consecutive_errors += 1
                self._sse({"delta": "\n[turn error: %s: %s]" % (type(e).__name__, e)})
                outcome = decide_outcome(False, STOP_REQUESTED, turn, max_turns,
                                         consecutive_errors)
                if outcome:
                    break
                msg = WORK_MODE_CONTINUE_NUDGE
                continue
            if isinstance(final, dict) and final.get("consent_failed"):
                # Work mode never surfaces the Edge mid-loop (that is /stream's own
                # UI-facing last resort) -- treat it as a turn error and keep looping
                # within the normal consecutive-error budget.
                consecutive_errors += 1
                self._sse({"delta": "\n[turn error: MCP connection-consent could not be "
                                     "auto-approved]"})
                outcome = decide_outcome(False, STOP_REQUESTED, turn, max_turns,
                                         consecutive_errors)
                if outcome:
                    break
                msg = WORK_MODE_CONTINUE_NUDGE
                continue
            consecutive_errors = 0
            final = final or ""
            done, turn_text = detect_done(final)
            if turn_text:
                _persist_exchange(sid, msg, turn_text)
            last_turn_text = turn_text
            self._sse({"turn_done": turn, "text": turn_text})
            outcome = decide_outcome(done, STOP_REQUESTED, turn, max_turns, consecutive_errors)
            if outcome:
                break
            queued = drain_pending_once(sid)
            msg, steered = select_next_message(queued, WORK_MODE_CONTINUE_NUDGE)
            for s_text in steered:
                self._sse({"steered": s_text})
        return outcome, turn, last_turn_text

    def _run_critic_pass(self, ac, deliverable):
        """UNCONTAMINATED critic pass (spec step 3): navigate the ONE shared PAGE to a brand
        new conversation (reusing the SAME /new machinery _do_new drives -- AGENT_URL + a fresh
        composer), send the fixed rubric prompt, and parse the verdict. Deliberately does NOT:
          * touch ACTIVE_SID or any S.* session bookkeeping (so the critic exchange is never
            appended to the working session's transcript -- this is what makes it
            "uncontaminated": the critic sees ONLY the rubric prompt, never the working
            conversation's history, and the working conversation never sees the rubric text
            either);
          * call _persist_exchange or _record_capture_baseline's session-linked callers.
        Uses _send_and_stream_once(..., stream_out=False) directly -- the shared
        send/stream/settle machinery -- with no SSE passthrough (the rubric prompt and the raw
        critic JSON reply are never shown to the /goal caller as delta/replace events; only the
        parsed verdict is surfaced, via the {"verdict": ...} SSE event _goal emits itself).

        Returns a verdict dict per parse_verdict's shape, plus "nav_ok": bool. On a navigation
        failure, or if BOTH the first attempt and the JSON-only retry come back unparseable,
        returns pass=False with a reason noting why (spec step 3: unparseable -> treat as fail
        with reason "critic output unparseable")."""
        try:
            if AGENT_URL:
                PAGE.goto(AGENT_URL, wait_until="domcontentloaded")
            if not _wait_composer():
                return {"ok": False, "pass": False, "failed_ac": [], "reasons": [
                    "critic navigation failed: composer did not render"], "needs_retry": False,
                    "nav_ok": False}
        except Exception as e:
            return {"ok": False, "pass": False, "failed_ac": [], "reasons": [
                "critic navigation failed: %s: %s" % (type(e).__name__, e)],
                "needs_retry": False, "nav_ok": False}
        prompt = build_rubric_prompt(ac, deliverable)
        try:
            reply = self._send_and_stream_once(prompt, stream_out=False) or ""
        except Exception as e:
            return {"ok": False, "pass": False, "failed_ac": [], "reasons": [
                "critic send failed: %s: %s" % (type(e).__name__, e)],
                "needs_retry": False, "nav_ok": True}
        verdict = parse_verdict(reply)
        if verdict["ok"] or not verdict["needs_retry"]:
            verdict["nav_ok"] = True
            return verdict
        # ONE retry in the SAME critic conversation with a "JSON only" nudge (spec step 3).
        try:
            reply2 = self._send_and_stream_once(RUBRIC_JSON_ONLY_NUDGE, stream_out=False) or ""
        except Exception as e:
            return {"ok": False, "pass": False, "failed_ac": [], "reasons": [
                "critic output unparseable (retry send failed: %s: %s)" % (type(e).__name__, e)],
                "needs_retry": False, "nav_ok": True}
        verdict2 = parse_verdict(reply2)
        if verdict2["ok"]:
            verdict2["nav_ok"] = True
            return verdict2
        return {"ok": False, "pass": False, "failed_ac": [], "reasons": [
            "critic output unparseable"], "needs_retry": False, "nav_ok": True}

    def _goal(self, text, max_turns, resume_flag, ac="", max_loops=DEFAULT_MAX_LOOPS):
        """WORK MODE: GET /goal -- an autonomous multi-turn loop on the ACTIVE session
        (creating one if none), per the frozen HTTP contract. SSE stream:
          * ordinary {"delta"}/{"replace"} events interleave while a turn streams (via
            _run_one_turn -> _send_and_stream_once, same as a normal /stream turn);
          * after each completed turn: {"turn_done": <int>, "text": "<final turn text>"};
          * when a queued steering input is injected for the next turn: one
            {"steered": "<text>"} event per input;
          * when `ac` is non-empty and a work phase reaches a genuine DONE: {"verify_start": n}
            then, once the critic responds, {"verdict": {"pass":..., "failed_ac":[...],
            "reasons":[...], "loop": n}};
          * at the end: {"goal_done": true, "outcome": ..., "turns": <int>} then event: done.

        resume_flag selects GET /goal?resume=1: continues a stored interrupted goal (mode==
        "interrupted" + a stored goal text -- see resume_eligibility), whose turn-1 message is
        the resume nudge instead of the original goal text (the goal itself is already in the
        live conversation from before the crash).

        VERIFICATION (only when `ac` is non-empty -- empty ac is 100% backward compatible with
        the pre-verification behavior, including all of decide_outcome's original outcomes):
        on a work-phase "done" outcome, run the producer/critic split from
        docs/loop-engineering.md SS5.3: capture the working conversation's ref (so we can
        reattach after the critic runs elsewhere), run _run_critic_pass in a FRESH,
        uncontaminated conversation, and act on decide_verify_outcome. See _run_critic_pass and
        decide_verify_outcome for the full per-branch behavior."""
        global ACTIVE_SID, STOP_REQUESTED
        STOP_REQUESTED = False
        sid = ACTIVE_SID
        if resume_flag:
            sess = S.load(sid) if sid else None
            ok, goal_or_reason = resume_eligibility(sess)
            if not ok:
                self._json({"ok": False, "error": goal_or_reason}); return
            goal_text = goal_or_reason
            first_msg = WORK_MODE_RESUME_NUDGE
        else:
            goal_text = text or ""
            if not sid:
                ACTIVE_SID = S.new_session()["sid"]
                sid = ACTIVE_SID
                logger.info("no active session -- created %s for /goal", sid)
            first_msg = wrap_goal_text(goal_text)

        # SSE headers -- same shape as /stream (this endpoint IS an SSE stream; only the
        # PAGE_LOCK-busy short-circuit in do_GET returns plain JSON before headers are sent).
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        S.touch(sid, mode="working", goal=goal_text)
        ac = (ac or "").strip()
        try:
            max_loops = int(max_loops)
        except (TypeError, ValueError):
            max_loops = DEFAULT_MAX_LOOPS
        turn = 0
        msg = first_msg
        outcome = None
        prev_failed_ac = None
        verify_loop_n = 0
        try:
            while True:
                outcome, turn, deliverable = self._run_work_phase(sid, msg, max_turns,
                                                                   turn_start=turn)
                if outcome != "done" or not ac:
                    # No verification requested, or the phase ended for a reason OTHER than a
                    # genuine DONE (error/stopped/max_turns) -- those keep their EXISTING
                    # outcome values unchanged (full backward compatibility).
                    break
                working_ref = ""
                try:
                    sess_now = S.load(sid) or {}
                    working_ref = sess_now.get("conv_url") or ""
                except Exception:
                    working_ref = ""
                if not working_ref:
                    # Cannot verify without a way back to the working conversation -- finish
                    # with the existing "done" outcome per the mission spec (ASCII warning).
                    self._sse({"delta": "\n[verify skipped: no conv_url captured for this "
                                         "session -- cannot reattach after the critic runs]"})
                    break
                verify_loop_n += 1
                self._sse({"verify_start": verify_loop_n})
                verdict = self._run_critic_pass(ac, deliverable)
                failed_ac = verdict.get("failed_ac") or []
                reasons = verdict.get("reasons") or []
                self._sse({"verdict": {"pass": bool(verdict.get("pass")),
                                        "failed_ac": failed_ac, "reasons": reasons,
                                        "loop": verify_loop_n}})
                oscillating = is_oscillating(prev_failed_ac, failed_ac) if not verdict.get(
                    "pass") else False
                verify_outcome = decide_verify_outcome(bool(verdict.get("pass")), verify_loop_n,
                                                        max_loops, oscillating)
                # Best-effort reattach in EVERY branch that continues talking to the working
                # conversation (pass -> resume before finishing; fail-continue -> resume before
                # sending the continuation). Failure is only logged, never fatal (spec step 4).
                if verify_outcome == "done_verified":
                    try:
                        ok_r, reason_r = _resume_to_ref(working_ref)
                        if not ok_r:
                            logger.warning("post-verify reattach failed for sid=%s: %s",
                                           sid, reason_r)
                    except Exception:
                        logger.warning("post-verify reattach raised for sid=%s", sid,
                                       exc_info=True)
                    outcome = "done_verified"
                    break
                if verify_outcome:   # "verify_failed" or "escalate_oscillation" -> stop here
                    outcome = verify_outcome
                    break
                # else: fail, not exhausted, not oscillating -> reattach to the WORKING
                # conversation and send a continuation message, then resume the work phase
                # (turn budget continues from `turn`, per the mission spec).
                prev_failed_ac = failed_ac
                try:
                    ok_r, reason_r = _resume_to_ref(working_ref)
                    if not ok_r:
                        logger.warning("reattach-to-working failed for sid=%s: %s",
                                       sid, reason_r)
                except Exception:
                    logger.warning("reattach-to-working raised for sid=%s", sid, exc_info=True)
                msg = build_continuation_message(failed_ac, reasons)
            self._sse({"goal_done": True, "outcome": outcome, "turns": turn})
            self._sse({}, "done")
        finally:
            S.touch(sid, mode="idle")

    def _drain_pending_queue(self, sid):
        """After a completed exchange, run any inputs that were queued (via /send) while a
        PAGE-touching request held PAGE_LOCK. PENDING QUEUE DESIGN NOTE: /send is now always
        store-only (it queues unconditionally -- see do_GET's /send handler), so anything
        queued while THIS request was running (or before it started) needs a drain pass right
        after the request that had the page finishes. Drains via the pure drain_pending_once()
        so the popping/looping logic itself is unit-testable without a browser. Each drained
        input is sent with NO SSE consumer (stream_out=False) but still persisted via
        S.append_turn/S.touch, same as a normal turn, so /history and the session transcript
        stay complete. Exception-guarded per item -- one bad queued input must not abandon the
        rest of the queue or crash the server loop."""
        for item in drain_pending_once(sid):
            try:
                _prepare_capture_baseline(sid)
                final = self._send_and_stream_once(item, stream_out=False)
                if final is not None and _looks_like_consent(final):
                    if _bridge_auto_consent():
                        try:
                            final = self._send_and_stream_once(item, stream_out=False)
                        except Exception:
                            final = None
                if final:
                    _persist_exchange(sid, item, final)
            except Exception:
                logger.warning("drain_pending_queue: queued send failed for sid=%s", sid, exc_info=True)


def _agent_tab_matches(pg, base_url):
    """BUG 4c helper: True if `pg` is already parked on the agent surface `base_url` drives
    (or, when base_url is empty, on ANY agent surface -- mirroring the old empty-URL fallback's
    own check). Used both to find a reusable tab and to identify duplicates left over from a
    prior bridge restart (start_bridge.ps1 -Keepalive used to open a fresh tab every restart,
    accumulating one authenticated tab per restart -- see _find_or_open_agent). Never raises:
    a closed/mid-navigation page can throw on .url or .locator()."""
    try:
        u = pg.url or ""
    except Exception:
        return False
    # The M365 SPA normalizes agent URLs: a tab opened at .../chat/agent/<id> or
    # .../chat/?titleId=T_<id> is rewritten in-place to the bare .../chat surface
    # (often .../chat/?redirfrom=CsrToSSR&auth=2), so matching on "/chat/agent/" alone
    # never recognizes a live, settled agent tab. Recognize the whole M365 chat surface
    # instead; the composer-present check below is what actually confirms it is a usable
    # agent tab rather than a blank/redirect page.
    if "m365.cloud.microsoft/chat" not in u and "/chat/agent/" not in u:
        return False
    try:
        if pg.locator(COPILOT_SELECTORS["composer"]).count() <= 0:
            return False
    except Exception:
        return False
    return True


def _close_duplicate_agent_tabs(ctx, keep_pg, base_url):
    """BUG 4c self-healing: close every OTHER tab already on this same agent surface, keeping
    only `keep_pg`. Guards against closing non-agent tabs (only closes pages that
    _agent_tab_matches recognizes) and never closes keep_pg itself. Exception-guarded per tab;
    never raises -- a failure here must not break startup. ASCII-only log."""
    try:
        pages = list(ctx.pages)
    except Exception:
        return
    closed = 0
    for pg in pages:
        if pg is keep_pg:
            continue
        try:
            if _agent_tab_matches(pg, base_url):
                pg.close()
                closed += 1
        except Exception:
            continue
    if closed:
        logger.info("_find_or_open_agent: closed %d duplicate agent tab(s) left over from "
                    "prior restart(s)", closed)


def _find_or_open_agent(ctx):
    url = os.environ.get("MCP_IMPL_AGENT_URL", "").strip()
    if url:
        # BUG 4c fix: reuse an existing tab already on this agent surface (e.g. left over from
        # a prior bridge restart via start_bridge.ps1 -Keepalive) instead of ALWAYS opening a
        # new one -- every restart used to open another authenticated tab, and they accumulated
        # forever. Only open a new tab when no reusable one exists.
        reused = None
        for pg in ctx.pages:
            if _agent_tab_matches(pg, url):
                reused = pg
                break
        if reused is not None:
            pg = reused
            try:
                if (pg.url or "") != url:
                    pg.goto(url, wait_until="domcontentloaded")
            except Exception:
                pass
            logger.info("_find_or_open_agent: reused existing agent tab instead of opening a new one")
        else:
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded")
            logger.info("_find_or_open_agent: no reusable agent tab found -- opened a new one")
        for _ in range(40):
            pg.wait_for_timeout(1000)
            if pg.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                break
        _close_duplicate_agent_tabs(ctx, pg, url)   # self-heal any tabs left over from before
        return pg
    for pg in ctx.pages:                       # fall back to any open agent tab
        if "/chat/agent/" in (pg.url or "") and pg.locator(COPILOT_SELECTORS["composer"]).count() > 0:
            _close_duplicate_agent_tabs(ctx, pg, url)
            return pg
    raise SystemExit("No agent page. Set MCP_IMPL_AGENT_URL in .env or open an agent tab in Edge.")


# ── tool-call self-probe (idle-only, opt-out) ───────────────────────────────────────────────
# See tools/tool_probe.py's module docstring for the full incident: FleetCockpit's health strip
# probes :9222 (the FLEET Edge) and :8000/tunnel but NEVER verifies that THIS bridge's agent
# (Edge profile copilot-bridge-edge, CDP :9223) can actually CALL an MCP tool end-to-end -- so a
# stale connector consent or dead CDP session here silently kills tool calls while every cockpit
# dot stays green. This sends a tiny synthetic probe turn through DRIVER.send/wait_for_idle/
# read_last_response -- the SAME CopilotWebDriver instance (module-global DRIVER) and SAME
# send/read primitives _send_and_stream_once ultimately drives, so no new page or CDP connection
# is opened, and no click/consent logic is reimplemented. Runs ONLY when idle (see PAGE_LOCK
# try-acquire and _LAST_USER_TURN_TS check below), never inside a real user turn.

# 0 disables the probe entirely (opt-out); default 600s (10 min) matches the module docstring.
MCP_TOOL_PROBE_SEC = float(os.environ.get("MCP_TOOL_PROBE_SEC", "600"))
# Never fire within this many seconds of a real user/goal turn (_LAST_USER_TURN_TS, stamped by
# _run_one_turn) -- a probe must not compete with, or be mistaken for, live work, and must not
# burn the user's agent context while they are actively using the bridge.
TOOL_PROBE_MIN_IDLE_SEC = 30.0
# How long to wait for the probe turn to settle before treating it as a "timeout" outcome --
# short on purpose (this is a tiny list_directory call, not a real task) so a wedged probe can
# never hold PAGE_LOCK for long.
TOOL_PROBE_TIMEOUT_SEC = 180

# Desktop path resolved at runtime (same construction relay/edge_reconnect.py's DEFAULT_PROBE
# uses) so the probe instruction works for any user, not just the one who wrote this file.
# Honors OneDrive Known Folder Move (Desktop redirected under "OneDrive - <org>\Desktop"),
# a common corporate M365 setup -- the plain USERPROFILE\Desktop join 404s there and made the
# probe itself fail (list_directory: not found), misreporting a real connector as broken.
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


_TOOL_PROBE_DESKTOP_DIR = _resolve_desktop_dir()
# The probe instruction: forces a real call_tool(list_directory) round-trip and asks the agent
# to emit tool_probe.PROBE_OK_TOKEN as the LAST line ONLY on success, so a canned/no-connector
# reply or a consent card (neither of which would emit the token) is distinguishable from a
# genuine tool-backed answer by tools.tool_probe.classify_probe_reply.
# Text for the FIRST probe. Every subsequent probe varies -- see tool_probe.
# next_probe_instruction for why re-sending one constant forever poisoned the conversation.
TOOL_PROBE_INSTRUCTION = tool_probe.next_probe_instruction(1, _TOOL_PROBE_DESKTOP_DIR)

_TOOL_PROBE_TIMER = None  # the pending threading.Timer, so _schedule_tool_probe can re-arm it
_TOOL_PROBE_SEQ = 0       # probes issued this process; feeds the varying instruction text


def _do_tool_probe_turn():
    """Runs ON the page-owner thread (via run_on_page_thread -- see PageExecutor's docstring
    for why Playwright calls must run there). Sends TOOL_PROBE_INSTRUCTION through the SAME
    module-global DRIVER a real turn uses and reads back the reply. Returns
    (agent_loaded, reply_text, timed_out). Never raises: a driver/page exception mid-probe is
    folded into (True, "", False) -- agent_loaded stays True because the composer check above
    it already passed, so classify_probe_reply naturally resolves this to kind="error" rather
    than the misleading "agent_unreachable" (which is reserved for the composer never having
    rendered at all)."""
    agent_loaded = False
    try:
        agent_loaded = PAGE is not None and PAGE.locator(COPILOT_SELECTORS["composer"]).count() > 0
    except Exception:
        agent_loaded = False
    if not agent_loaded:
        return False, "", False
    global _TOOL_PROBE_SEQ
    _TOOL_PROBE_SEQ += 1
    try:
        DRIVER.send(tool_probe.next_probe_instruction(_TOOL_PROBE_SEQ, _TOOL_PROBE_DESKTOP_DIR))
        idle_ok = DRIVER.wait_for_idle(timeout_s=TOOL_PROBE_TIMEOUT_SEC)
        reply = DRIVER.read_last_response() or ""
        return True, reply, (not idle_ok)
    except Exception:
        logger.warning("tool probe: turn raised on the page thread", exc_info=True)
        return True, "", False


# CDP can stay healthy while the M365 page object has gone stale (observed after long runs and
# network switches). The idle tool probe is the safest liveness signal because it already executes
# on the Playwright owner thread and only while PAGE_LOCK is free. Require consecutive failures so
# a single slow render never kills the process; exiting hands recovery to start_bridge -Keepalive.
PAGE_UNREACHABLE_RETRY_SEC = max(
    5.0, float(os.environ.get("MCP_PAGE_UNREACHABLE_RETRY_SEC", "20"))
)
PAGE_UNREACHABLE_FAILURES = max(
    2, int(os.environ.get("MCP_PAGE_UNREACHABLE_FAILURES", "3"))
)
PAGE_PROBE_EXECUTOR_TIMEOUT_SEC = max(
    TOOL_PROBE_TIMEOUT_SEC + 30.0,
    float(os.environ.get("MCP_PAGE_PROBE_EXECUTOR_TIMEOUT_SEC", "240")),
)
_PAGE_UNREACHABLE_STREAK = 0


def _page_probe_requires_restart(kind):
    global _PAGE_UNREACHABLE_STREAK
    if kind != "agent_unreachable":
        _PAGE_UNREACHABLE_STREAK = 0
        return False
    _PAGE_UNREACHABLE_STREAK += 1
    return _PAGE_UNREACHABLE_STREAK >= PAGE_UNREACHABLE_FAILURES


def _run_bounded_page_probe_call(fn):
    try:
        return PAGE_EXECUTOR.submit_bounded(PAGE_PROBE_EXECUTOR_TIMEOUT_SEC, fn)
    except TimeoutError:
        try:
            tool_probe.record_probe(
                False, "starting", detail="Playwright page thread wedged; restarting"
            )
        except Exception:
            pass
        logger.error(
            "tool probe: page-owner thread exceeded %.0fs; exiting for keepalive recovery",
            PAGE_PROBE_EXECUTOR_TIMEOUT_SEC,
        )
        os._exit(71)


def _run_tool_probe():
    """Idle-only self-probe entry point, called from the self-re-arming timer in
    _schedule_tool_probe(). Skips silently (no page touch, no record) if disabled, if a real
    user/goal turn happened recently, or if PAGE_LOCK is currently held by one -- reusing the
    EXACT SAME PAGE_LOCK try-acquire guard /stream and /goal already use (see PAGE_LOCK's
    module docstring), so this can never collide with a real user message. Exception-guarded
    end to end: a probe failure must never crash the bridge or stop future probes from being
    scheduled (see _schedule_tool_probe's finally-based re-arm).

    BUG 1 fix: a consent_card classification is no longer just RECORDED -- it now DRIVES the
    same recovery a live user turn gets (_bridge_auto_consent's tiers, then the bounded/
    retryable _consent_surface_attempt last resort), because on a fresh device the stale
    connector's consent card only ever renders as the RESULT of an actual tool call (see
    relay/edge_reconnect.py's module docstring); the startup proactive auto-consent in
    _page_main has nothing to click without one. Every PAGE-touching step below still runs on
    the page-owner thread (run_on_page_thread) and inside the SAME PAGE_LOCK this function
    already holds, so recovery can never race a real user turn."""
    try:
        if MCP_TOOL_PROBE_SEC <= 0:
            return  # opt-out
        since_user = time.time() - _LAST_USER_TURN_TS
        if since_user < TOOL_PROBE_MIN_IDLE_SEC:
            logger.debug("tool probe: skipped (user turn %.0fs ago)", since_user)
            return max(5.0, TOOL_PROBE_MIN_IDLE_SEC - since_user)
        if PAGE is None:
            # Startup not finished yet (or _page_main never got there) -- report this directly
            # instead of calling run_on_page_thread, which would block this timer thread
            # forever if the page-owner thread never reaches PAGE_EXECUTOR.run_forever().
            tool_probe.record_probe(False, "starting", detail="PAGE not initialized; retrying")
            logger.info("tool probe: starting (PAGE not initialized); short retry armed")
            return 15.0
        if not PAGE_LOCK.acquire(blocking=False):
            logger.debug("tool probe: skipped (page busy)")
            return 15.0
        try:
            # Persist an explicit transitional state BEFORE the potentially long M365 turn.
            # FleetCockpit renders this as a spinner, so a 30-180s real tool round-trip never
            # looks like an inert stale-red indicator.
            tool_probe.record_probe(False, "checking", detail="tool probe in progress")
            agent_loaded, reply, timed_out = _run_bounded_page_probe_call(_do_tool_probe_turn)
            if timed_out:
                ok, kind = False, "timeout"
            else:
                ok, kind = tool_probe.classify_probe_reply(reply, agent_loaded)
                if kind == "consent_card":
                    logger.info("tool probe: consent_card sighted -- driving recovery")
                    consented = False
                    try:
                        consented = _run_bounded_page_probe_call(_bridge_auto_consent)
                    except Exception:
                        logger.warning("tool probe: _bridge_auto_consent raised", exc_info=True)
                    if consented:
                        # consent resolved without needing surface() -- fresh episode for later.
                        _reset_consent_surface_episode()
                        try:
                            agent_loaded, reply, timed_out = _run_bounded_page_probe_call(
                                _do_tool_probe_turn
                            )
                            if timed_out:
                                ok, kind = False, "timeout"
                            else:
                                ok, kind = tool_probe.classify_probe_reply(reply, agent_loaded)
                        except Exception:
                            logger.warning("tool probe: re-probe after auto-consent raised",
                                           exc_info=True)
                    else:
                        logger.warning("tool probe: auto-consent failed for a consent card")
        finally:
            PAGE_LOCK.release()
        if kind == "consent_card":
            # auto-consent could not resolve it (failed outright, or the re-probe above still
            # saw a card) -- fall through to the bounded/retryable last-resort surface(), the
            # same recovery ladder an interactive turn gets. No live SSE consumer here, so
            # _consent_surface_attempt logs/records instead of streaming to a client.
            surfaced = _consent_surface_attempt("idle tool probe: consent card unresolved")
            logger.info("tool probe: last-resort surface attempt -> %s", surfaced)
        # Final record reflects whatever the LAST classification actually established (a
        # successful auto-consent's re-probe result if one ran, else the original outcome) --
        # this is the authoritative snapshot /health reads.
        tool_probe.record_probe(ok, kind, detail=(reply or "")[:200])
        logger.info("tool probe: ok=%s kind=%s", ok, kind)
        if _page_probe_requires_restart(kind):
            try:
                tool_probe.record_probe(
                    False, "starting", detail="Copilot page stale; supervisor restarting"
                )
            except Exception:
                pass
            logger.error(
                "tool probe: Copilot page remained unreachable for %d checks; exiting for "
                "keepalive recovery",
                _PAGE_UNREACHABLE_STREAK,
            )
            os._exit(71)
        if kind == "agent_unreachable":
            return PAGE_UNREACHABLE_RETRY_SEC
        return None
    except Exception:
        logger.warning("tool probe: _run_tool_probe raised", exc_info=True)
        try:
            tool_probe.record_probe(False, "error", detail="probe driver raised")
        except Exception:
            pass
        return 30.0


# BUG 1 fix: how long to wait before the FIRST probe after startup (short), vs. the normal idle
# cadence for every probe after that (MCP_TOOL_PROBE_SEC). On a fresh device the stale
# connector's consent card only ever renders as the RESULT of an actual tool call (see
# relay/edge_reconnect.py's module docstring) -- the startup proactive auto-consent in
# _page_main only inspects the DOM, so it is a structural no-op with nothing to click yet.
# Without an early probe the bridge would otherwise wait up to the full MCP_TOOL_PROBE_SEC
# (600s default) before ever forcing that card to render. Env-tunable; 0 falls back to the
# normal MCP_TOOL_PROBE_SEC for the first run too (no special-casing).
MCP_TOOL_PROBE_STARTUP_DELAY_SEC = float(os.environ.get("MCP_TOOL_PROBE_STARTUP_DELAY_SEC", "30"))


def _schedule_tool_probe(delay=None):
    """Self-re-arming threading.Timer: run _run_tool_probe(), then -- regardless of outcome --
    schedule the NEXT run MCP_TOOL_PROBE_SEC later, for as long as the process is alive. A
    one-shot Timer chained via its own callback (not a persistent daemon loop thread), matching
    _schedule_force_rehide's pattern elsewhere in this file. daemon=True so it never blocks
    process exit. Disabled entirely (never even arms once) when MCP_TOOL_PROBE_SEC<=0, so
    setting it to 0 truly opts out -- no timer, no probe turn, ever.

    `delay` overrides the wait before THIS arm's probe fires; only main() passes it (as
    MCP_TOOL_PROBE_STARTUP_DELAY_SEC, for the very first run right after startup -- see BUG 1
    fix above). Every self-re-arm from _tick() below omits it, so all SUBSEQUENT runs use the
    normal MCP_TOOL_PROBE_SEC idle cadence unchanged."""
    global _TOOL_PROBE_TIMER
    if MCP_TOOL_PROBE_SEC <= 0:
        logger.info("tool probe: disabled (MCP_TOOL_PROBE_SEC<=0)")
        return
    wait = MCP_TOOL_PROBE_SEC if delay is None else max(0.0, delay)

    def _tick():
        retry_delay = None
        try:
            retry_delay = _run_tool_probe()
        except Exception:
            logger.warning("tool probe: _tick raised", exc_info=True)
            retry_delay = 30.0
        finally:
            # Normal outcomes keep the configured cadence. Startup/page-busy/error outcomes
            # return a short delay so recovery is visible in seconds, not after the old 10 min.
            _schedule_tool_probe(delay=retry_delay)

    _TOOL_PROBE_TIMER = threading.Timer(wait, _tick)
    _TOOL_PROBE_TIMER.daemon = True
    _TOOL_PROBE_TIMER.start()


class _SingleBindHTTPServer(ThreadingMixIn, HTTPServer):
    """ThreadingHTTPServer (via ThreadingMixIn) that REFUSES to double-bind, combining BOTH
    concurrency requirements this bridge needs:

    (1) THREADED: a long-running /goal turn must not block /send (steering) or /stop from
        getting through -- see the module-level PAGE_LOCK docstring. ThreadingMixIn dispatches
        each request on its own thread (daemon_threads=True below so a stuck request thread
        never blocks process exit); PAGE_LOCK is the single serialization point ensuring only
        one thread ever touches the Playwright PAGE/DRIVER objects at a time (they are not
        thread-safe).

    (2) SINGLE-INSTANCE: the inherited allow_reuse_address=1 maps to SO_REUSEADDR, which on
        Windows lets a SECOND process silently bind the same port -- two live bridges then
        each drive their OWN Edge page while sharing the same session ledger, and request
        dispatch between them is effectively random (resume lands on one, the next send on
        the other). Windows does not need SO_REUSEADDR for a quick listener restart, so it is
        disabled outright: a second bind now raises instead of silently succeeding."""
    allow_reuse_address = False
    daemon_threads = True


def _port_already_served(port, timeout=2.0):
    """True if something is already accepting connections on 127.0.0.1:<port> (an existing
    bridge instance). A plain TCP connect probe -- no request is sent. Never raises."""
    try:
        probe = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        probe.close()
        return True
    except OSError:
        return False


# The PowerShell keepalive supervisor can only restart the bridge after this Python process
# exits. Previously, if the dedicated Edge (:9223) died while Python and :8765 stayed alive,
# the supervisor remained blocked forever inside `& python`: the cockpit showed Tool red, but
# neither process repaired the half-dead stack. This watchdog closes that gap. Three consecutive
# local CDP failures are required to ignore a brief Edge restart; exit 70 hands control back to
# start_bridge.ps1, which recreates Edge and relaunches the bridge. Session state is durable in
# SQLite, so the restart does not discard the conversation ledger.
CDP_WATCHDOG_SEC = max(2.0, float(os.environ.get("MCP_CDP_WATCHDOG_SEC", "10")))
CDP_WATCHDOG_FAILURES = max(2, int(os.environ.get("MCP_CDP_WATCHDOG_FAILURES", "3")))


def _cdp_healthy(cdp, timeout=2.0):
    try:
        parsed = urllib.parse.urlparse(cdp)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", "/json/version")
        response = conn.getresponse()
        response.read(256)
        conn.close()
        return response.status == 200
    except Exception:
        return False


def _start_cdp_watchdog(cdp):
    def _watch():
        failures = 0
        while True:
            time.sleep(CDP_WATCHDOG_SEC)
            if _cdp_healthy(cdp):
                if failures:
                    logger.info("CDP watchdog: recovered after %d failed check(s)", failures)
                failures = 0
                continue
            failures += 1
            logger.warning("CDP watchdog: %s failed (%d/%d)", cdp, failures,
                           CDP_WATCHDOG_FAILURES)
            if failures >= CDP_WATCHDOG_FAILURES:
                try:
                    tool_probe.record_probe(False, "starting",
                                            detail="bridge Edge lost; supervisor restarting")
                except Exception:
                    pass
                logger.error("CDP watchdog: dedicated Edge remained unavailable; exiting for "
                             "keepalive recovery")
                os._exit(70)

    thread = threading.Thread(target=_watch, daemon=True, name="cdp-watchdog")
    thread.start()
    return thread


def _page_main(cdp, fresh):
    """Runs ENTIRELY on PAGE_EXECUTOR's dedicated owner thread (see start() below): creates
    PAGE/DRIVER, runs startup auto-resume, then services PAGE_EXECUTOR's work queue forever.
    Every later PAGE/DRIVER call (from any HTTP request thread) is routed here via
    run_on_page_thread -- see PageExecutor's docstring for why page creation and every later
    page call must share this one thread (Playwright sync-API thread affinity)."""
    global PAGE, DRIVER, AGENT_URL, ACTIVE_SID
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

        # BUG 4d fix: proactively run the EXISTING auto-consent click-through once, right after
        # the composer is confirmed ready (post sign-in/tab-reuse), instead of ONLY reactively
        # from inside a turn after a real reply already contained consent markers. This makes a
        # connection-manager card that appears immediately after first sign-in get auto-clicked
        # instead of being left for the (much more disruptive) surface() last resort. Reuses
        # _bridge_auto_consent's existing three tiers unchanged -- no click logic reimplemented
        # here; Tier 0/2 themselves no-op safely when no consent card/link is actually present,
        # so this is safe to call unconditionally. Best-effort: PAGE/DRIVER are already live at
        # this point, so failures are logged and swallowed rather than blocking startup.
        try:
            if _bridge_auto_consent():
                logger.info("startup proactive auto-consent: click-through succeeded")
            else:
                logger.info("startup proactive auto-consent: no consent card handled "
                            "(none present, or all tiers found nothing to click)")
        except Exception:
            logger.warning("startup proactive auto-consent raised", exc_info=True)

        # STARTUP AUTO-RESUME: reattach to the most recently active session that has a
        # reattachable conv_url, so a bridge restart (including the -Keepalive restart
        # path in scripts/start_bridge.ps1) does not silently forget the running
        # conversation. --fresh on the command line skips this and starts clean (a
        # session is still lazily created on first /stream, same as before this change).
        latest = None
        try:
            latest = S.latest_active()
        except Exception:
            logger.warning("startup auto-resume: S.latest_active() failed", exc_info=True)
        do_resume, why = should_autoresume(latest, fresh_flag=fresh)
        if do_resume:
            try:
                ref = latest.get("conv_url") or ""
                kind = classify_conv_ref(ref)
                if kind == "sessref":
                    ok, reason = _resume_to_ref(ref)
                elif kind == "conv_url":
                    ok = _goto_settled(ref)
                    reason = "ok" if ok else "navigation did not settle"
                else:
                    ok, reason = False, "unresumable conv_url shape"
                if ok:
                    # ok=True is now a VERIFIED claim (_resume_to_ref only returns True
                    # once the pane demonstrably shows the target conversation).
                    ACTIVE_SID = latest["sid"]
                    S.touch(ACTIVE_SID, status="active")
                    print("resumed session %s" % ACTIVE_SID, flush=True)
                    # CRASH-RESUME (work mode): the resumed session was left mode=="working"
                    # by a /goal loop that never reached its own loop-end S.touch(mode="idle")
                    # -- i.e. the bridge process died mid-goal (crash, or the -Keepalive
                    # supervisor restarting it). Mark it "interrupted" and log the recovery
                    # path, but do NOT auto-continue unattended: the operator must explicitly
                    # hit GET /goal?resume=1 to pick the loop back up.
                    if latest.get("mode") == "working":
                        S.touch(ACTIVE_SID, mode="interrupted")
                        print("interrupted goal found; continue with /goal?resume=1", flush=True)
                else:
                    print("startup auto-resume failed (%s); falling back to fresh" % reason,
                          flush=True)
            except Exception as e:
                logger.warning("startup auto-resume failed", exc_info=True)
                print("startup auto-resume failed: %s: %s" % (type(e).__name__, e), flush=True)
        else:
            print("startup auto-resume: %s" % why, flush=True)
        if ACTIVE_SID is None:
            # STARTUP-FRESH: no session was resumed, so the pane is on the bare agent page
            # (or wherever the failed resume left it) -- record the change-based capture
            # baseline now so the first exchange of a lazily-created session can be
            # attributed correctly. Refreshed again right before each capture-needing send.
            _record_capture_baseline()

        print("copilot bridge: driving %s" % PAGE.url[-40:], flush=True)
        # Service PAGE_EXECUTOR's work queue forever ON THIS THREAD -- every later
        # run_on_page_thread(...) call from any HTTP request thread executes here, inside the
        # SAME `with sync_playwright()` context that created PAGE/DRIVER above. This call
        # blocks for the lifetime of the process (mirrors the old srv.serve_forever()).
        PAGE_EXECUTOR.run_forever()


def main():
    fresh = "--fresh" in sys.argv[1:]
    cdp = os.environ.get("MCP_CDP_URL", "http://localhost:9222")
    port = int(os.environ.get("MCP_BRIDGE_PORT", "8765"))
    # SINGLE-INSTANCE GUARD: if another bridge is already serving the port, exit at once
    # (before touching CDP / creating a page). Belt: this connect probe gives a clean exit
    # + log line. Suspenders: _SingleBindHTTPServer makes a double-bind raise even if two
    # instances race past this check simultaneously.
    if _port_already_served(port):
        print("bridge already serving port %d; exiting (single-instance guard)" % port, flush=True)
        return
    # Start the page-owner thread FIRST and let it finish PAGE/DRIVER setup (including
    # startup auto-resume) before the HTTP server starts accepting connections -- a request
    # arriving before PAGE exists would otherwise submit a job to an executor with nothing
    # to run it against. PAGE_EXECUTOR.submit() blocks the calling (request) thread until the
    # job runs, so simply starting the HTTP server after this call is enough serialization;
    # no separate "ready" event is needed because do_GET's first PAGE access is always via
    # run_on_page_thread, which is a no-op-until-queued blocking call.
    PAGE_EXECUTOR.start(lambda: _page_main(cdp, fresh))
    _start_cdp_watchdog(cdp)
    # Arm the idle tool-call self-probe AFTER the page-owner thread has been started (so PAGE
    # exists, or is about to, by the time the first tick fires -- see "tool-call self-probe"
    # section above _SingleBindHTTPServer). No-ops entirely when MCP_TOOL_PROBE_SEC<=0 (opt-out).
    # BUG 1 fix: the FIRST tick fires after the short MCP_TOOL_PROBE_STARTUP_DELAY_SEC, not the
    # full MCP_TOOL_PROBE_SEC -- a fresh device needs an early real tool call to force a stale
    # connector's consent card to render at all (see MCP_TOOL_PROBE_STARTUP_DELAY_SEC's
    # docstring above _schedule_tool_probe). If PAGE is not ready yet by then, _run_tool_probe
    # simply records agent_unreachable and the normal MCP_TOOL_PROBE_SEC cadence takes over from
    # the next re-arm.
    _schedule_tool_probe(delay=MCP_TOOL_PROBE_STARTUP_DELAY_SEC)
    # THREADED (via ThreadingMixIn in _SingleBindHTTPServer) so a long /goal run cannot
    # block /send (steering) or /stop from getting through. Playwright sync objects are
    # still not thread-safe -- PageExecutor (module-level PAGE_EXECUTOR) is the single
    # serialization point ensuring only the page-owner thread ever touches PAGE/DRIVER; see
    # its docstring near the top of this file.
    srv = _SingleBindHTTPServer(("127.0.0.1", port), Handler)
    print("copilot bridge: http://127.0.0.1:%d" % port, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

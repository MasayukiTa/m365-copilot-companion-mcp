"""copilot_autopilot_relay.py -- autonomous, hands-off relay for an M365 Copilot agent.

WHAT IT IS
  A standalone controller (the "frame") that drives a Microsoft 365 Copilot agent
  (the one wired to m365-copilot-companion-mcp) toward a goal, completely on its
  own -- no human and no second AI in the loop. Given a goal it:

      send the goal + a turn protocol  ->  loop:
          wait until the agent turn finishes   (DOM-state completion detection)
          read the agent's answer
          record it to memory + the run-log     (operators D and memory_ops)
          decide deterministically:
              "DONE"  in answer -> stop
              "FAIL"  in answer -> send a fix instruction
              else              -> send "continue"
      until DONE / max_turns / kill-switch.

WHY IT DOES NOT INTERFERE WITH YOUR OTHER WORK  (the important property)
  It drives the page through the Chrome DevTools Protocol (Playwright
  connect_over_cdp). Keystrokes and clicks are dispatched into the target tab via
  CDP -- they do NOT move your OS mouse cursor and do NOT steal your keyboard
  focus. So while the relay pumps a Copilot conversation in one tab, you can keep
  typing in other apps / windows. This is the whole point versus screen-scraping.

ONE-TIME SETUP  (no re-login, no Playwright browser download -- it attaches to
                 the Edge you already use and are already signed into)
  1. Close Edge, then relaunch it with the debug port from a terminal:
         & "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" --remote-debugging-port=9222
     (Chrome works too: chrome.exe --remote-debugging-port=9222)
  2. In that Edge, open M365 Copilot, pick your MCP agent, start a NEW chat with
     it, and copy the conversation URL from the address bar.
  3. Make sure the MCP server + tunnel are up (start.ps1 + supervisor.ps1) and the
     Copilot backend IP is unlocked once (see the project README).

RUN
    .venv\\Scripts\\python.exe relay\\copilot_autopilot_relay.py ^
        --conversation-url "https://m365.cloud.microsoft/chat/agent/.../conversation/..." ^
        --goal "copilot_loop_demo に data.csv(10行) を作り、合計と平均を出す stats.py を書き、self-test を足して PASS させ、SUMMARY.txt にまとめる" ^
        --max-turns 12

NOTES
  * Selectors below were captured from the live M365 Copilot DOM. Microsoft may
    change them; they are isolated in COPILOT_SELECTORS for easy patching.
  * The frame makes NO model calls. The only intelligence is the Copilot agent
    itself (the fixed oracle). The frame is deterministic plumbing.
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.acceptance import normalize_checks, run_all_blocking  # spec 3-3 verify gate
from tools.gate_ops import stop_check                     # operator E: kill-switch
from tools.memory_ops import memory_load, memory_save     # cross-session history
from tools.runlog_ops import runlog_append, runlog_summarize  # operator D: audit

class ConversationClosed(RuntimeError):
    """Raised by send() when the target tab/composer is already gone (the
    conversation ended) BEFORE we even try to submit. This is the TargetClosedError
    race seen in 28/72 send_failures records: the agent turn finished, the page was
    torn down, and the relay then tried to send into a dead target -- burning the full
    3-attempt retry budget every time. run_relay treats this as TERMINAL (no transient
    retry), since retrying a closed target can never succeed. Subclasses RuntimeError
    so any caller that only catches RuntimeError still behaves safely."""


class GenerationInProgress(RuntimeError):
    """Raised by send() when the PREVIOUS agent turn is STILL GENERATING (the Stop
    button is showing / the composer + Send are not operable) and it did not finish
    within send()'s generous internal wait. This is NOT a send failure: the composer was
    never touched, nothing was typed, no click was attempted. It means "the agent is just
    slow this turn" (e.g. a long django/sympy turn), which is exactly the W0
    (django__django-14730) STUCK: the relay tried to send into a still-generating turn,
    the click hung the default 30s, and 10 such retries burned the budget into STUCK.

    Callers MUST treat this as a NON-budget-consuming reschedule: wait and try again
    WITHOUT incrementing the transient-retry counter, so merely waiting out a slow turn
    can never push a healthy run to STUCK. Only a real failed submit (RuntimeError /
    ConversationClosed) consumes budget. Subclasses RuntimeError so a caller that only
    catches RuntimeError still behaves safely (it just won't get the no-budget benefit)."""


# --- Selectors captured from the live M365 Copilot DOM (2026-06) ------------
COPILOT_SELECTORS = {
    "composer": "#m365-chat-editor-target-element",          # contenteditable, role=textbox
    # The agent's reply lives in .fai-CopilotMessage (one per AGENT turn). This is
    # the reliable signal: its count rises only when the agent answers, and its
    # inner_text is the answer. NOTE: data-testid="chatOutput" was NOT reliable --
    # it can read back the user's own message, which broke STUCK detection.
    "assistant_msg": ".fai-CopilotMessage",
    "assistant_msg_fallback": '[data-testid="copilot-message-reply-div"]',
    # The Send button. Pressing Enter in this rich editor does NOT reliably submit
    # (the text just sits in the composer) -- clicking this button does.
    #
    # LIVE DOM (companion Edge, 2026-06-13, read-only CDP scrape + 72 send_failures
    # records): when the composer holds text, the button renders as
    #   <button aria-label="送信" ...>           (JP locale, visible, enabled)
    # in the same 40x40 toolbar slot that holds the dictation / voice buttons when
    # the composer is EMPTY (so the button simply does not exist until text is typed
    # AND has armed -- this is the slow-arm race that produced match_count=0). This
    # build exposes NO data-testid on the send button, so we anchor on the localized
    # aria-label (JP + EN) first, then fall back to structural signals that survive a
    # label/locale change: a submit-typed button, and the icon-bearing send glyph.
    # Multi-candidate + comma-separated so a Microsoft DOM tweak degrades gracefully
    # instead of going to match_count=0 again.
    "send_button": (
        'button[aria-label="送信"], '            # JP, observed live (14/72 snapshots)
        'button[aria-label="Send"], '            # EN locale
        'button[aria-label*="送信"], '           # JP, label with extra decoration
        'button[aria-label*="Send" i], '         # EN, e.g. "Send message"
        'button[data-testid="send-button"], '    # if MS ever adds a testid
        'button[data-testid*="send" i], '
        'button[name="send" i], '
        'button[type="submit"]'                  # last-ditch structural fallback
    ),
    # The STOP (square) button that REPLACES Send in the same toolbar slot while the
    # agent turn is generating. Its PRESENCE is the reliable "previous turn is still
    # running" signal (spec §7: judge by an element that only exists during generation).
    # We use it to gate send() so we never type/click into a turn that is still producing
    # output (the W0 django__django-14730 STUCK: send into a generating turn -> 30s click
    # hang x10 -> STUCK). Localized aria-labels (JP + EN) first, then structural fallbacks
    # that survive a label/locale change, so a Microsoft DOM tweak degrades gracefully.
    "stop_button": (
        'button[aria-label="停止"], '            # JP, stop-generating
        'button[aria-label="Stop"], '            # EN
        'button[aria-label*="停止"], '           # JP, label with extra decoration
        'button[aria-label*="Stop generating" i], '
        'button[aria-label*="応答を停止" i], '   # JP, "stop the response"
        'button[aria-label*="stop respon" i], '  # EN, "stop responding/response"
        'button[data-testid*="stop" i]'
    ),
    # Where the Copilot-generated conversation title renders. M365 surfaces the auto-
    # generated chat name in a few places depending on layout; we try each in order and
    # take the first non-empty, sane-looking string. Best-effort only -- every selector
    # here is allowed to miss, and conversation_title() falls back to document.title and
    # then to "" (the caller then falls back to the goal text). These are isolated here
    # so a Microsoft DOM change is a one-line patch, like the other selectors.
    "conv_title": (
        '[data-testid="conversation-title"], '
        '[data-testid="chat-title"], '
        '[data-testid="copilot-chat-header-title"], '
        'h1[class*="title"], '
        'header h1, header h2, '
        '[role="heading"][aria-level="1"]'
    ),
}

# Kept deliberately SHORT: this agent's model has a small effective input budget
# (the connected MCP tool schemas consume most of it), so a verbose framing alone
# overflows it (OpenAIModelTokenLimit) before any work. Every clause here is load-
# bearing; do not pad it back out.

# Output-discipline clause injected into every prompt turn to suppress the agent's
# "advisor/lecturer/ego" persona. Defined ONCE here; referenced from each prompt constant.
# The clamp is on UNSOLICITED advice and persona — not on doing the actual task (code,
# lists, explanations explicitly requested by the goal are still produced as normal).
OUTPUT_DISCIPLINE = (
    "【出力規律・厳守】あなたはタスク実行者であり、助言者・解説者ではない。"
    "求められた成果物・回答・操作結果のみを出す。"
    "禁止: 頼まれていない助言/一般論/感想/評価/価値判断、"
    "『まずは〜しろ』『〜を完璧に固めろ』式の命令調コーチング、"
    "『今の理解レベルだと』『初心者の9割は』等の上から目線・決めつけ、"
    "自我・人格・キャラ付け、頼まれていない箇条書きの講釈や前置きの長文。"
    "質問には直接かつ簡潔に答えて止まる。淡々と事実とタスク結果のみを書く。"
    "（タスクが明示的にコード・箇条書き・説明を要求している場合のみ、それを過不足なく出す。）"
)

PROTOCOL = (
    OUTPUT_DISCIPLINE + " "
    "ツールを使い自律的に進める。重い作業は小さく分割し1ターンに1〜数ステップ。"
    "ツールは call_tool ゲートウェイ経由: まず call_tool(name='') で一覧(名前+要約)を見て"
    "このタスクに必要なツールを見極め、call_tool(name='X') で使い方を確認、"
    "call_tool(name='X', arguments={...}) で実行する。"
    "初手はこの一覧確認＋対象フォルダ/ファイルの存在確認(call_tool で list_directory)から始め、"
    "いきなり絶対パス直行しない。パスは必ず「/」区切り(例 C:/dir/file)、バックスラッシュ禁止(\\t等に化ける)。"
    "ファイル/画像/データの大量処理は1ターンに1件だけ処理し、都度ディスク(Excel等)に保存して次へ"
    "(まとめて読むと OpenAIModelTokenLimit で失敗)。各ターン冒頭で保存済み状態を見て未処理の続きから。"
    "深い調査は行頭 `RESEARCH: 内容`、データ分析は `ANALYZE: 絶対パス | 指示`。"
    "各ターン最終行に必ず: 続行=CONTINUE、完了(検証も通過)=DONE、行き詰まり=STUCK: 理由。"
    "任意: 最終マーカーの直前に `NEXT: <次アクション1行>` と `CONFIDENCE: low|medium|high` を書いてよい。"
    "まず最初のステップを実行。\nGoal: "
)

CONTINUE_JOB = (
    "次のステップを実行してください。ゴール全体が完了したら最後の行に DONE、"
    "まだ続きがあれば CONTINUE、行き詰まったら STUCK: 理由 と書いてください。"
    " " + OUTPUT_DISCIPLINE
)
FIX_JOB = (
    "直前の失敗の原因を分析し、ツールで修正してから続けてください。"
    "どうしても無理なら最後の行に STUCK: 理由 と書いてください。"
    " " + OUTPUT_DISCIPLINE
)
# Sent back to the agent when it reported DONE but the frame's OWN acceptance check
# (spec 3-3 verification loop) failed. We hand it the GROUND TRUTH -- the real command
# output -- not a vague "you might be wrong", so it fixes the actual defect. %s = detail.
VERIFY_FIX_JOB = (
    "あなたは DONE と報告しましたが、こちらの自動検証（ローカルで実際に実行）で不合格でした。"
    "実際の検証結果は次のとおりです:\n"
    "--- 検証結果 ---\n%s\n--- 検証結果ここまで ---\n"
    "この結果を踏まえて原因を特定し、ツールで修正してください。"
    "修正後は可能なら自分でも同じ検証を実行して通ることを確かめ、"
    "通る状態になったら最後の行に再度 DONE と書いてください。"
    "どうしても無理なら最後の行に STUCK: 理由 と書いてください。"
    " 【規律】修正と結果のみ。解説・評価・助言は不要。"
)
# Sent back when an INDEPENDENT reviewer (operator B refuter) found a concrete defect in
# a claimed-done result. %s = the reviewer's concrete reason.
REFUTE_FIX_JOB = (
    "独立したレビュアーがあなたの完了報告を精査し、ゴールが達成されていない具体的な問題を"
    "指摘しました:\n--- レビュアーの指摘 ---\n%s\n--- 指摘ここまで ---\n"
    "この指摘が妥当かを自分で確認し、妥当なら修正してください。妥当でない（既に満たしている）"
    "と判断した場合はその根拠を示してください。"
    "ただし**調査してから動くこと**。すでに red→green で検証した編集を、それを否定する**具体的な"
    "失敗アサーションを名指しできない限り**、git checkout 等で破棄・巻き戻ししてはいけません。反証は"
    "『最初からやり直せ』ではなく**詰めるべき箇所の指し示し**です。"
    "対応後、ゴールが満たせていれば最後の行に再度 "
    "DONE、無理なら STUCK: 理由 と書いてください。"
    " 【規律】修正結果のみ報告。解説・評価・助言は不要。"
)
NUDGE_JOB = (
    "前のステップがまだ完了していないようです。今の状況を1行で報告し、"
    "可能なら次に進んでください。"
    " " + OUTPUT_DISCIPLINE
)
# Re-injected when the agent reported STUCK but it is likely a TRANSIENT failure (a tool
# call / network hiccup), so we retry the turn -- the relay analog of Claude Code retrying
# a failed network request rather than surfacing it.
RETRY_JOB = (
    "直前の操作が一時的な失敗（ネットワーク断やツール呼び出しの失敗）だった可能性があります。"
    "焦らず、同じ手順をもう一度実行してください（ファイルの読み書きやコマンドを再試行）。"
    "完了したら最後の行に DONE、本当に解決不能な場合のみ STUCK: と理由を書いてください。"
    " " + OUTPUT_DISCIPLINE
)

# Re-anchored as the FIRST message of a fresh conversation after the previous one hit the
# Copilot model token limit (OpenAIModelTokenLimit). The new chat has NO memory of prior
# turns, so the agent must re-derive progress from the actual artifacts on disk (the target
# Excel, output files, etc.) rather than from conversation history. Prefixed to the goal.
RECYCLE_PREFIX = (
    "【会話リセット】前会話がトークン上限のため新会話に切替。以前の記憶は無し。"
    "結果は出力先ファイル(Excel等)に保存済み。まず保存状態を読み直し未完了の続きから。\n--- 元のゴール ---\n"
)

# Claude-Code / Anthropic-SDK-style exponential backoff with jitter for transient-failure
# retries. The SDK's _calculate_retry_timeout uses: delay = min(initial * 2**attempt, cap),
# then subtracts up to 25% jitter (delay * (1 - 0.25*rand)); initial=0.5s, cap=8s. We mirror
# that exactly so the wait widens 0.5 -> 1 -> 2 -> 4 -> 8s (capped), instead of the old flat
# linear schedule. (The SDK also honors a Retry-After header when the server sends one; our
# failures are CDP/Edge/tool hiccups with no HTTP response, so there is no header to read --
# pass retry_after when a real one is ever available and it takes precedence, clamped to 60s.)
RETRY_INITIAL_DELAY = 0.5
RETRY_MAX_DELAY = 8.0
RETRY_MULTIPLIER = 2.0


def transient_backoff(n, retry_after=None,
                      initial=RETRY_INITIAL_DELAY, multiplier=RETRY_MULTIPLIER,
                      cap=RETRY_MAX_DELAY):
    """Seconds to wait before transient retry `n` (1-indexed). Exponential with -25% jitter,
    matching the Anthropic SDK. If a server Retry-After (seconds) is supplied and sane, it wins
    (clamped to 60s), as the SDK does."""
    if retry_after is not None:
        try:
            ra = float(retry_after)
            if 0 < ra <= 60:
                return ra
        except (TypeError, ValueError):
            pass
    attempt = max(0, int(n) - 1)
    base = min(initial * (multiplier ** attempt), cap)
    return base * (1.0 - 0.25 * random.random())


# Placeholder text Copilot shows in the answer block WHILE it is still working.
# Treat these as "not finished" so completion detection never stabilizes on them.
PROCESSING_MARKERS = ("処理中", "生成しています", "考えています", "working on it",
                      "thinking", "...")


# Phrases Copilot emits when it never actually received/registered the task and is asking what
# to do -- a DELIVERY failure, not a real dead-end. Seen on round 5 where a worker burned 10
# transient retries with the generic RETRY_JOB because the goal text never landed in the tab.
# When STUCK co-occurs with one of these, the right fix is to RESEND THE GOAL ITSELF, not a
# generic "try again" nudge.
_GOAL_NOT_SEEN_MARKERS = (
    "タスクが提示されていません", "タスクが提示されて", "タスクが見当たりません",
    "指示が提示されていません", "指示がありません", "ゴールが提示されていません",
    "課題が提示されていません", "依頼内容が確認できません", "何をすればよいか",
    "具体的なタスク", "提示してください", "no task", "no task was", "task was not provided",
    "wasn't provided a task", "haven't been given", "have not been given a task",
    "no instructions", "please provide the task", "please provide a task",
    "what would you like me to", "what task",
)


def goal_not_seen(resp: str) -> bool:
    """True when the agent's reply indicates it never received the actual task (so it is asking
    for one), as opposed to a genuine STUCK on the work. In that case the goal text should be
    RE-SENT verbatim rather than a generic retry nudge."""
    t = (resp or "").lower()
    return any(m.lower() in t for m in _GOAL_NOT_SEEN_MARKERS)


def reported_stuck(resp: str) -> bool:
    """True only when the agent really declared STUCK with the protocol marker
    ('STUCK:' / 'STUCK：'). A bare substring match on 'STUCK' false-fires when the agent
    merely mentions the word (e.g. 'STUCKではありません' or echoes the protocol), which was
    seen to abort a perfectly fine run."""
    up = (resp or "").upper()
    return "STUCK:" in up or "STUCK：" in up


def conversation_exhausted(resp: str) -> bool:
    """True when Copilot itself reports the conversation can no longer continue
    because it ran out of model token budget. The hands-off relay otherwise
    appends to ONE chat forever, so a long task (lots of OCR text / Excel rows)
    eventually trips this and EVERY later turn returns the same error. Detecting
    it lets run_relay recycle to a fresh conversation instead of dying.

    Anchored on Copilot's own error code/text (JP + EN) -- kept conservative so a
    normal answer that merely discusses tokens does not false-fire.
    """
    t = (resp or "")
    low = t.lower()
    if "openaimodeltokenlimit" in low:
        return True
    # Defensive variants Copilot has shown for the same condition.
    if ("トークン" in t and ("上限" in t or "制限" in t or "超え" in t)):
        return True
    if "maximum context length" in low or "context length exceeded" in low:
        return True
    return False


def bare_agent_url(url: str) -> str:
    """Best-effort: turn a specific conversation URL into the agent's base URL,
    which (per find_conversation_page) starts a NEW chat when loaded fresh.
    Strips a trailing '/conversation/<id>' segment; leaves other forms as-is so
    the caller's explicit agent_url (preferred) is what really drives recycling.
    """
    u = url or ""
    marker = "/conversation/"
    if marker in u:
        return u.split(marker, 1)[0]
    return u


def _is_processing(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    return any(m.lower() in t for m in PROCESSING_MARKERS) and len(t) < 40


# Protocol end-of-turn markers. The PROTOCOL/CONTINUE/etc. prompts REQUIRE the agent to
# write exactly one of these on the LAST line of every complete turn (DONE / CONTINUE /
# STUCK: / FAIL / RESEARCH: / ANALYZE: / PLAN_READY). Their presence on the tail line is a
# strong positive signal that the turn FINISHED -- conversely, a "stable" answer whose tail
# carries NO marker is a red flag that we may have locked onto a MID-STREAM pause (Copilot
# streams the reply token-by-token; if a chunk lands and the next is >dwell_s away -- a tool
# call, a model "thinking" gap, a network stall -- the partial text looks deceptively stable
# and gets captured mid-word with no marker, e.g. transcript turn5 "...隠し", 102 chars).
_END_MARKERS = ("DONE", "CONTINUE", "STUCK", "FAIL", "RESEARCH", "ANALYZE", "PLAN_READY")


def has_end_marker(text: str) -> bool:
    """True iff the LAST non-empty line of `text` carries a protocol end-of-turn marker
    (DONE / CONTINUE / STUCK / FAIL / RESEARCH / ANALYZE / PLAN_READY), in EN or with the
    JP full-width colon. Used by completion detection to distinguish a genuinely finished
    turn from a partial capture of a still-streaming reply (a stable-but-marker-less tail is
    treated as 'maybe still generating' and given extra settle time)."""
    if not text:
        return False
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    last = lines[-1].upper().replace("：", ":")
    return any(m in last for m in _END_MARKERS)


def extract_research(resp: str) -> str:
    """Pull the query out of a `RESEARCH: <...>` line if the agent asked for a
    deep-dive. Returns '' if no research was requested."""
    for line in (resp or "").splitlines():
        m = re.match(r"\s*RESEARCH\s*[:：]\s*(.+)", line, re.IGNORECASE)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def extract_analyze(resp: str):
    """Pull (file_path, instruction) out of an `ANALYZE: <path> | <instruction>`
    line. Returns None if no analysis was requested."""
    for line in (resp or "").splitlines():
        m = re.match(r"\s*ANALYZE\s*[:：]\s*(.+)", line, re.IGNORECASE)
        if m and m.group(1).strip():
            body = m.group(1).strip()
            if "|" in body:
                path, instr = body.split("|", 1)
                path, instr = path.strip(), instr.strip()
            else:
                path, instr = body, "添付データを分析し、要点を短くまとめてください。"
            if path:
                return path, instr
    return None


def extract_next(resp: str) -> str:
    """Pull the content of a `NEXT: <...>` line if present (informational only).
    Returns '' when not present."""
    for line in (resp or "").splitlines():
        m = re.match(r"\s*NEXT\s*[:：]\s*(.+)", line, re.IGNORECASE)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def extract_confidence(resp: str) -> str:
    """Pull the agent's self-assessment from `CONFIDENCE: low|medium|high` if present.
    Returns '' when not present or unrecognised."""
    for line in (resp or "").splitlines():
        m = re.match(r"\s*CONFIDENCE\s*[:：]\s*(low|medium|high)\b", line, re.IGNORECASE)
        if m:
            return m.group(1).lower()
    return ""


def extract_forge(resp: str):
    """Pull (tool_name, code) from an operator-A forge request: a `FORGE: <name>` line plus
    a fenced ```python ... ``` block carrying the full module source. Returns None if no
    forge was requested or no code block is present."""
    m = re.search(r"FORGE\s*[:：]\s*([A-Za-z_]\w*)", resp or "")
    if not m:
        return None
    cm = re.search(r"```(?:python)?\s*\n(.*?)```", resp or "", re.DOTALL)
    if not cm or not cm.group(1).strip():
        return None
    return m.group(1), cm.group(1)


# Appended to the goal only when forge mode is on, so tasks don't forge spuriously.
FORGE_HINT = (
    "\n（任意・operator A）処理の中で再利用可能な新しいツールがあると有効な場合は、その行に "
    "`FORGE: <関数名>` と書き、続けて ```python ... ``` に完全なモジュールソースを書いてください。"
    "こちらで構文検証して tools/auto/ に配置します（実行はされず、サーバ再起動後に Copilot から使えます）。"
)


def _adjust_backoff(ok, turn_elapsed, backoff_s, base_elapsed,
                    backoff_step_s, backoff_max_s, slow_factor):
    """Pure adaptive-throttle step (spec §6). Returns (new_backoff, new_base, reason).

      * a turn that timed out         -> raise backoff hard (2 steps)
      * a turn >slow_factor x the fastest healthy turn (and >20s) -> raise 1 step
      * an otherwise healthy turn      -> decay backoff by half a step
    `base_elapsed` tracks the fastest healthy turn = the "not throttled" baseline.
    """
    if not ok:
        return min(backoff_max_s, backoff_s + backoff_step_s * 2), base_elapsed, "turn_timeout"
    if base_elapsed is None or turn_elapsed < base_elapsed:
        base_elapsed = turn_elapsed
    if base_elapsed and turn_elapsed > base_elapsed * slow_factor and turn_elapsed > 20:
        return min(backoff_max_s, backoff_s + backoff_step_s), base_elapsed, "slow_turn"
    return max(0.0, backoff_s - backoff_step_s * 0.5), base_elapsed, "healthy"


def default_notify(title: str, body: str) -> None:
    """Best-effort Windows toast; never raises into the control loop."""
    try:
        from tools.notify_ops import notify_desktop
        notify_desktop(title, body[:240])
    except Exception:
        pass


class CopilotWebDriver:
    """Drives one M365 Copilot conversation tab over CDP. No OS input is used."""

    # Default per-action upper bound (ms) for EVERY Playwright locator action on this page
    # (click / is_visible / is_enabled / get_attribute auto-waits). Playwright's own default
    # is 30s -- that is what let one click hang 30s and, x10 retries, drove the W0
    # django__django-14730 STUCK. 8s is comfortably above a healthy action's settle time but
    # bounds a single stuck operation to seconds, not half a minute. Individual calls that
    # need a different bound still pass timeout=... explicitly (those win over this default).
    DEFAULT_ACTION_TIMEOUT_MS = 8000

    def __init__(self, page):
        self.page = page
        self._count_before = 0  # number of answer blocks before the current send
        # Cap the default action timeout so no single locator op can hang the worker for
        # Playwright's default 30s. Best-effort: a driver built on a mock/stub page (tests)
        # has no such method, so guard it.
        try:
            self.page.set_default_timeout(self.DEFAULT_ACTION_TIMEOUT_MS)
        except Exception:
            pass

    def _answers(self):
        return self.page.locator(COPILOT_SELECTORS["assistant_msg"])

    # Priority-ordered candidates: exact labels first, broad fallbacks LAST. The broad
    # substring candidates can match imposters -- observed live 2026-06-13: the
    # composer-expand toggle is labeled "[Copilot にメッセージを送信する] 入力ボックスを
    # 展開します", which contains 送信 and got clicked instead of Send, so every send
    # no-opped. _send_button() therefore (a) tries candidates in this order, not DOM
    # order, and (b) rejects any node whose label matches the imposter blacklist.
    SEND_BUTTON_CANDIDATES = (
        'button[aria-label="送信"]',
        'button[aria-label="Send"]',
        'button[data-testid="send-button"]',
        'button[name="send" i]',
        'button[aria-label*="送信"]',
        'button[aria-label*="Send" i]',
        'button[type="submit"]',
    )
    SEND_LABEL_BLACKLIST = ("展開", "折りたた", "expand", "collapse",
                            "ディクテーション", "dictation", "ボイス", "voice",
                            # feedback-submit imposter: W0 (django__django-14730) STUCK
                            # snapshot showed btn match 3 with `.first` =
                            # aria_label='フィードバックを送信' (a feedback button), NOT the
                            # message Send. Reject any feedback-submit so we never click it.
                            "フィードバック", "feedback",
                            "メッセージを送信する]")  # the expand toggle's bracketed prefix

    def _send_button(self):
        """First ACCEPTABLE send button by candidate priority, or None.

        Iterates SEND_BUTTON_CANDIDATES in order (exact labels first); within a
        candidate prefers visible matches; rejects imposters whose aria-label hits
        SEND_LABEL_BLACKLIST. Returns None when nothing acceptable matches -- callers
        treat that as 'not armed yet'."""
        for cand in self.SEND_BUTTON_CANDIDATES:
            try:
                loc = self.page.locator(cand)
                try:
                    vis = loc.locator("visible=true")
                    if vis.count() > 0:
                        loc = vis
                except Exception:
                    pass
                for i in range(min(loc.count(), 5)):
                    el = loc.nth(i)
                    try:
                        label = (el.get_attribute("aria-label") or "")
                    except Exception:
                        label = ""
                    low = label.lower()
                    if any(b.lower() in low for b in self.SEND_LABEL_BLACKLIST):
                        continue
                    return el
            except Exception:
                continue
        return None

    def _stop_button(self):
        """The visible STOP (square) button if the agent turn is generating, else None.

        Mirrors _send_button (priority-ordered, prefers visible matches) but for the
        stop control. Its presence is the positive "previous turn is still producing
        output" signal. Never raises -> None on any error (treated as 'not generating',
        which is the safe default: at worst we proceed to the normal arm-wait, which is
        itself guarded)."""
        for cand in (COPILOT_SELECTORS["stop_button"]).split(", "):
            cand = cand.strip()
            if not cand:
                continue
            try:
                loc = self.page.locator(cand)
                try:
                    vis = loc.locator("visible=true")
                    if vis.count() > 0:
                        loc = vis
                except Exception:
                    pass
                for i in range(min(loc.count(), 5)):
                    el = loc.nth(i)
                    try:
                        if el.is_visible():
                            return el
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    def _is_generating(self) -> bool:
        """True while the PREVIOUS agent turn is still generating (the Stop button is
        showing). This is the `_is_processing`-equivalent for the SEND path: it does NOT
        look at answer text (which lags), it looks at the live Stop control. Used to GATE
        send() so we never type/click into a turn that is still producing output -- the W0
        django__django-14730 STUCK was exactly send-into-generating (is_processing=True in
        the failure snapshot). Never raises -> False (safe: 'not generating')."""
        try:
            return self._stop_button() is not None
        except Exception:
            return False

    def _wait_generation_idle(self, timeout_s: float = 240.0,
                              poll_ms: int = 500) -> bool:
        """Wait until the previous agent turn has STOPPED generating (no Stop button),
        up to `timeout_s`. Returns True once idle, False if still generating at the
        deadline (or the tab died -- caller's dead-check turns that terminal).

        This is the gate that keeps a slow django/sympy turn from being mistaken for a
        send failure: while generation is in progress we simply WAIT (this wait is NOT a
        retry and must not consume the transient budget -- send() raises
        GenerationInProgress, not a send failure, if the wait is exhausted). Polls the
        kill-switch so STOP aborts promptly, and bails if the page dies mid-wait."""
        deadline = time.time() + max(0.0, timeout_s)
        while time.time() < deadline:
            if stop_check().startswith("STOP"):
                return False
            if not self._page_alive():
                return False
            if not self._is_generating():
                return True
            self.page.wait_for_timeout(poll_ms)
        # one last check at the deadline (the turn may have finished on the final tick)
        try:
            return not self._is_generating()
        except Exception:
            return False

    def _page_alive(self) -> bool:
        """Cheap liveness probe: is the tab still open AND the composer still present?

        Used as an early dead-check before a send so a conversation that ended (the
        page/composer was torn down -- the TargetClosedError race seen in
        send_failures.jsonl, 28/72) is treated as terminal IMMEDIATELY instead of
        burning the full 3-attempt x 12s retry budget against a dead target. Any
        exception (incl. TargetClosedError from page.is_closed/evaluate) -> not alive.
        Never raises."""
        try:
            if self.page.is_closed():
                return False
        except Exception:
            return False
        try:
            # touch the page; a closed/navigated-away target raises here
            return self.page.locator(COPILOT_SELECTORS["composer"]).count() > 0
        except Exception:
            return False

    def _composer_text(self) -> str:
        """Current composer text, minus zero-width junk."""
        try:
            t = self.page.locator(COPILOT_SELECTORS["composer"]).first.inner_text() or ""
        except Exception:
            t = ""
        return t.replace("​", "").replace("‌", "").strip()

    def _wait_send_armed(self, timeout_s: float = 12.0) -> bool:
        """Wait until the Send button is present AND enabled.

        Two facts this guards against, both learned the hard way:
          * The Send button only ARMS a beat after real text is typed -- clicking
            immediately after typing finds nothing and silently no-ops.
          * WHILE the agent turn is running, the Send button is REPLACED by the
            Stop (square) button, so `送信` is simply absent. Its (re)appearance is
            therefore the reliable "the turn is idle and ready" signal (spec §7:
            judge by an element that only exists after completion).
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            # If the tab/composer died mid-wait (conversation ended), stop waiting --
            # there is nothing to arm. The caller's dead-check turns this terminal.
            if not self._page_alive():
                return False
            # Re-resolve the locator each pass: it is lazy, so this re-queries the DOM
            # and picks up the send button the instant it renders. count() does NOT
            # auto-wait (that race was the root cause of match_count=0 in the failure
            # log -- the button armed a beat AFTER the synchronous count() check), so we
            # re-poll on a short cadence and also let is_visible/is_enabled settle.
            try:
                btn = self._send_button()
                if btn is not None and btn.is_visible() and btn.is_enabled():
                    return True
            except Exception:
                pass
            self.page.wait_for_timeout(300)
        return False

    def _snapshot_send_failure(self, attempt: int, phase: str) -> None:
        """Best-effort diagnostic snapshot written to .fleet/send_failures.jsonl.

        DIAGNOSTIC ONLY. This method is fully self-contained and TOTALLY wrapped in
        try/except: any failure (DOM eval, JSON, file IO, import) is swallowed so it
        can NEVER alter send()'s control flow, retry count, timing, or exception. Each
        DOM probe is individually guarded so one failed probe still records the rest,
        leaving "err:<type>" in place of the value that could not be read.
        """
        try:
            import json as _json
            import os as _os
            from datetime import datetime as _dt

            def _probe(fn):
                # Run one DOM/page probe under its own guard; on any error return a
                # short "err:<ExceptionType>" marker instead of failing the snapshot.
                try:
                    return fn()
                except Exception as _e:  # noqa: BLE001 - diagnostic must never raise
                    return "err:" + type(_e).__name__

            def _send_btn_loc():
                return self.page.locator(COPILOT_SELECTORS["send_button"])

            def _send_count():
                return _send_btn_loc().count()

            def _send_disabled():
                btn = _send_btn_loc().first
                # is_enabled() inverted; explicit so a None/odd state is still legible
                return not btn.is_enabled()

            def _send_aria_disabled():
                return _send_btn_loc().first.get_attribute("aria-disabled")

            def _send_aria_label():
                return _send_btn_loc().first.get_attribute("aria-label")

            def _send_visible():
                return _send_btn_loc().first.is_visible()

            def _composer_raw():
                return self.page.locator(COPILOT_SELECTORS["composer"]).first.inner_text() or ""

            def _composer_len():
                return len(_composer_raw())

            def _composer_head():
                return _composer_raw()[:80]

            def _visibility_state():
                return self.page.evaluate("() => document.visibilityState")

            def _has_focus():
                return self.page.evaluate("() => document.hasFocus()")

            def _conv_guid():
                # The conversation guid is the last path segment of the M365 chat URL.
                url = self.page.url or ""
                seg = url.rstrip("/").rsplit("/", 1)[-1]
                return seg or None

            def _is_processing_now():
                # _is_processing() applied to the current last-answer text: was a Stop
                # (generation-in-progress) indicator effectively showing?
                return _is_processing(self.read_last_response())

            def _page_url():
                return self.page.url

            record = {
                "ts": _probe(lambda: _dt.now().astimezone().isoformat()),
                "conv_guid": _probe(_conv_guid),
                "attempt": attempt,
                "phase": phase,
                "send_button": {
                    "match_count": _probe(_send_count),
                    "disabled": _probe(_send_disabled),
                    "aria_disabled": _probe(_send_aria_disabled),
                    "aria_label": _probe(_send_aria_label),
                    "visible": _probe(_send_visible),
                },
                "composer": {
                    "text_len": _probe(_composer_len),
                    "head80": _probe(_composer_head),
                },
                "tab": {
                    "visibility_state": _probe(_visibility_state),
                    "has_focus": _probe(_has_focus),
                },
                "is_processing": _probe(_is_processing_now),
                "page_url": _probe(_page_url),
            }

            out_dir = _os.path.join(str(REPO), ".fleet")
            try:
                _os.makedirs(out_dir, exist_ok=True)
            except Exception:
                pass
            out_path = _os.path.join(out_dir, "send_failures.jsonl")
            with open(out_path, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
        except Exception:
            # Diagnostic logging must never affect send(); swallow everything.
            pass

    # Default upper bound (seconds) send() will WAIT for a still-generating previous turn
    # to finish before it gives up with GenerationInProgress. Generous on purpose: slow
    # django/sympy turns legitimately run minutes, and this wait is NOT a failure (it does
    # not consume the transient/Stuck budget), so a too-short value here is what caused the
    # W0 STUCK (send into a generating turn). Per-call overridable via send(gen_wait_s=...).
    GEN_WAIT_S = 240.0

    def send(self, text: str, gen_wait_s: float | None = None) -> None:
        # EARLY DEAD-CHECK: if the tab/composer is already gone (conversation ended),
        # do NOT enter the type/arm/click retry loop -- it would just throw
        # TargetClosedError on every probe and waste the full 3x12s budget (the
        # TargetClosedError race, 28/72 of send_failures). Fail fast and terminally so
        # run_relay records a STUCK instead of spinning. This is a pure read; never
        # types or clicks.
        if not self._page_alive():
            raise ConversationClosed(
                "send aborted: conversation tab/composer is closed (dead target)")
        # GENERATION GATE: if the PREVIOUS turn is still generating (Stop button showing),
        # do NOT type/click into it -- that was the W0 django__django-14730 STUCK
        # (is_processing=True in the failure snapshot; the click then hung the default 30s).
        # WAIT for the turn to finish (slow django/sympy turns are legitimate, not failures).
        # If it is STILL generating after the generous window, raise GenerationInProgress so
        # the caller RESCHEDULES WITHOUT consuming the transient budget -- merely waiting out
        # a slow turn must never count toward STUCK. The composer is untouched here.
        gw = self.GEN_WAIT_S if gen_wait_s is None else gen_wait_s
        if self._is_generating():
            self._snapshot_send_failure(attempt=0, phase="waiting_processing")
            if not self._wait_generation_idle(timeout_s=gw):
                if not self._page_alive():
                    raise ConversationClosed(
                        "send aborted: tab/composer closed while waiting for the "
                        "previous turn to finish generating")
                raise GenerationInProgress(
                    "send deferred: previous turn still generating after "
                    "%.0fs wait (not a send failure)" % gw)
        # CRITICAL: a newline in the Copilot composer SUBMITS the message. Collapse
        # all whitespace (incl. newlines) to single spaces so the whole job is sent
        # as ONE message with a single trailing Enter.
        one_line = " ".join(str(text).split())
        # remember how many answer blocks exist now, so wait_for_idle can detect a
        # genuinely NEW one (rather than re-reading the previous turn's answer).
        try:
            self._count_before = self._answers().count()
        except Exception:
            self._count_before = 0
        composer = self.page.locator(COPILOT_SELECTORS["composer"]).first

        # Type -> wait for Send to ARM -> force-click -> verify composer emptied.
        # Retry a few times; if it never empties, RAISE so run_relay records a real
        # STUCK instead of pretending the turn was submitted.
        for attempt in range(3):
            # EXPLICIT timeout on the composer click (was unbounded = Playwright's default
            # 30s). The W0 STUCK had this hang the full 30s x10. A focus click should
            # resolve in well under 5s on a live composer; if it can't, fall through to the
            # arm-wait + Enter fallback rather than freezing the worker for 30s. force=True
            # skips actionability waits (the composer is already known present from the
            # dead-check) so this is essentially immediate.
            try:
                composer.click(force=True, timeout=5000)
            except Exception:
                pass
            self.page.keyboard.press("Control+a")   # clear via keyboard, not fill("")
            self.page.keyboard.press("Delete")       # -- fill("") leaves the editor
            self.page.wait_for_timeout(150)          #    in a state where Send won't arm
            # insert_text = ONE atomic Input.insertText (paste-like). Unlike type(), it
            # sends no per-key events, so the OS Japanese IME never intercepts it and a
            # long Japanese goal lands intact -> the Send button arms reliably. type()
            # was the cause of "Send button never submitted" on long JP goals.
            self.page.keyboard.insert_text(one_line)
            if self._wait_send_armed(timeout_s=12.0):
                try:
                    btn = self._send_button()
                    if btn is not None:
                        btn.click(force=True, timeout=4000)
                except Exception:
                    pass
            else:
                # Send never armed (rare): last-ditch Enter.
                try:
                    self.page.keyboard.press("Enter")
                except Exception:
                    pass
            # The submit + composer-clear is async and can take >1s (esp. under load /
            # right after a fresh page). POLL for the composer to empty instead of one
            # fixed 800ms check -- the short check was the real cause of the false
            # "Send button never submitted" failures (and the retry then double-typed).
            # Window is generous (12s) because under memory pressure the M365 SPA can take
            # many seconds to clear the composer; a too-short window both falsely fails AND
            # causes the retry to double-send. Re-click the Send button each second in case
            # it re-armed without submitting (a load-induced no-op click).
            for i in range(48):                  # up to ~12s
                self.page.wait_for_timeout(250)
                if not self._composer_text():
                    return  # composer emptied => message was submitted
                # STRONGER success signal: if a new answer block has appeared, the agent
                # is already replying, so the send DID go through -- even if the composer
                # is slow to visually clear under memory pressure. Without this, a laggy
                # composer caused false 'send failed' + a double-send on retry.
                try:
                    if self._answers().count() > self._count_before:
                        return
                except Exception:
                    pass
                if i and i % 4 == 0:             # ~every 1s, nudge a re-armed Send button
                    try:
                        btn = self._send_button()
                        if btn is not None and btn.is_enabled():
                            btn.click(force=True, timeout=2000)
                    except Exception:
                        pass
            # This attempt did not submit (no return above). Record a diagnostic
            # snapshot. Pure side effect, fully guarded -- does NOT change the loop.
            self._snapshot_send_failure(attempt=attempt, phase="attempt_failed")
        # All 3 attempts exhausted. One final snapshot just before the RuntimeError so
        # the terminal state is captured. Pure side effect, fully guarded.
        self._snapshot_send_failure(attempt=3, phase="final_before_raise")
        raise RuntimeError(
            "send failed: composer still holds text after 3 attempts "
            "(Send button never submitted the message)"
        )

    def wait_for_idle(self, timeout_s: int = 1800, dwell_s: float = 4.0,
                      appear_timeout_s: int = 180) -> bool:
        """Completion = a NEW answer block appears, the agent is NO LONGER GENERATING,
        and the answer text is STABLE. We do NOT rely on the loading indicator (it stays
        present/visible while idle, so it is useless). The completion signal is twofold:

          (1) the agent is not generating  -- the live Stop (square) button is GONE
              (`_is_generating()`); this is the authoritative "the turn finished"
              signal, the same control the SEND gate uses. Reading while it is still
              showing was the root cause of partial capture (transcript turn5: 102
              chars, mid-word "...隠し", no marker) -- a streaming pause longer than
              dwell_s made partial text look 'stable' and it was captured as final.
          (2) the answer text has stopped changing for `dwell_s`.

        BELT-AND-SUSPENDERS for the case where the Stop button briefly disappears between
        streamed chunks: a stable answer whose TAIL has NO protocol marker
        (DONE/CONTINUE/STUCK/FAIL/RESEARCH/ANALYZE/PLAN_READY) is treated as 'possibly
        still streaming' and must stay stable for an EXTENDED window (2x dwell) before we
        accept it. A marker-terminated tail is accepted at the normal dwell. Either way
        the wait is bounded by `timeout_s`, so this can never hang. Polls the kill-switch
        so STOP aborts promptly."""
        deadline = time.time() + timeout_s
        # 1) wait for a brand-new answer block to appear.
        appear_deadline = time.time() + min(appear_timeout_s, timeout_s)
        appeared = False
        while time.time() < appear_deadline:
            if stop_check().startswith("STOP"):
                return False
            try:
                if self._answers().count() > self._count_before:
                    appeared = True
                    break
            except Exception:
                pass
            time.sleep(1.0)
        if not appeared:
            return False
        # 2) wait for the last answer's REAL text to stabilize AND generation to stop.
        # While the block still shows a processing placeholder ("処理中です" etc.) OR the
        # agent is still generating (Stop button present), keep waiting -- otherwise we
        # would lock onto a placeholder or a mid-stream partial as the final answer.
        last, stable_since = None, None
        while time.time() < deadline:
            if stop_check().startswith("STOP"):
                return False
            # PRIMARY gate: never read while the turn is still generating. _is_generating
            # never raises (-> False), so a driver/page without a Stop control degrades to
            # the pure text-stability behavior (back-compat).
            try:
                generating = self._is_generating()
            except Exception:
                generating = False
            if generating:
                last, stable_since = None, None
                time.sleep(1.0)
                continue
            t = self.read_last_response()
            if _is_processing(t):
                last, stable_since = None, None
            elif t == last:
                # require a longer settle when the tail carries no protocol marker, in
                # case the Stop button flickered off between two streamed chunks.
                need = dwell_s if has_end_marker(t) else dwell_s * 2.0
                if stable_since and (time.time() - stable_since) >= need:
                    if not has_end_marker(t):
                        print("[relay] accepting marker-less but idle+stable response "
                              "(%.0fs) -- no DONE/CONTINUE/STUCK tail" % need)
                    return True
            else:
                last, stable_since = t, time.time()
            time.sleep(1.0)
        return False

    def read_last_response(self) -> str:
        loc = self._answers()
        if loc.count() == 0:
            loc = self.page.locator(COPILOT_SELECTORS["assistant_msg_fallback"])
        if loc.count() == 0:
            return ""
        try:
            txt = loc.last.inner_text() or ""
        except Exception:
            return ""
        # strip the "<agent> said:" prefix Copilot prepends, then a duplicated
        # agent-name line if present.
        if " said:" in txt:
            txt = txt.split(" said:", 1)[1]
        lines = txt.splitlines()
        while lines and not lines[0].strip():
            lines = lines[1:]
        if len(lines) >= 2 and lines[0].strip() and lines[0].strip() == lines[1].strip():
            lines = lines[1:]
        return "\n".join(lines).strip()

    def read_last_reply_clean(self) -> str:
        """Agent reply TEXT only -- for callers (the OpenAI-compat adapter) where ANY stray prefix
        corrupts a downstream harness's action parsing.

        The DOM of one agent turn is:
            <h6 accessibleHeading>"<NAME> said:"</h6>  <img avatar alt="<NAME>">  <reply text...>
        so inner_text reads back  "<NAME> said:\n<NAME>\n<actual reply>"  -- the accessible heading
        AND the avatar's alt-text (the agent name a second time) BEFORE the real answer (here <NAME>
        was e.g. "desktopfile<emoji>"). read_last_response() only splits off the heading and leaves
        the avatar name line. We extract <NAME> from the heading, then drop the heading and every
        leading line that merely repeats <NAME>. Generic over any agent name and multi-line replies;
        Playwright already returns proper UTF-8."""
        loc = self._answers()
        # Prefer the FIRST block produced AFTER this send (index == _count_before)
        # over .last: on a freshly-recycled conversation a leftover greeting/
        # placeholder bubble can be the trailing block, and reading .last would
        # return it instead of this turn's real answer. Only do this when a genuinely
        # new block exists (count grew past _count_before); otherwise fall back to
        # .last exactly as before so the non-recycle path is unchanged.
        target = None
        try:
            cnt = loc.count()
        except Exception:
            cnt = 0
        if cnt == 0:
            loc = self.page.locator(COPILOT_SELECTORS["assistant_msg_fallback"])
            try:
                cnt = loc.count()
            except Exception:
                cnt = 0
        if cnt == 0:
            return ""
        if 0 <= self._count_before < cnt:
            target = loc.nth(self._count_before)
        else:
            target = loc.last
        try:
            txt = target.inner_text() or ""
        except Exception:
            return self.read_last_response()
        name = ""
        if " said:" in txt:
            name = txt.split(" said:", 1)[0].strip()
            txt = txt.split(" said:", 1)[1]
        lines = txt.splitlines()
        while lines and (not lines[0].strip() or (name and lines[0].strip() == name)):
            lines = lines[1:]
        return "\n".join(lines).strip()

    def conversation_title(self) -> str:
        """Best-effort scrape of the Copilot-generated conversation title.

        M365 Copilot auto-names a chat a beat after the first turn (e.g. "FizzBuzz
        スクリプトの作成"). The cockpit/chat use this as the card/conversation headline
        when present (else they fall back to the goal text), so capturing it is a pure
        readability win. This is COMPLETELY best-effort: any failure -> "" and the caller
        keeps its existing goal-derived title. Never raises.

        Order: a header/title DOM node (conv_title selector) first, then document.title
        with the trailing app-name chrome stripped. We reject placeholders ("新しいチャット"
        / "New chat" / "Copilot") and over-long blobs so we never overwrite a good goal
        title with junk; the caller treats "" as 'no title captured'.
        """
        def _clean(s):
            try:
                s = " ".join((s or "").split())
            except Exception:
                return ""
            if not s:
                return ""
            low = s.lower()
            # placeholders / app chrome that are NOT a real conversation name
            bad = ("新しいチャット", "新しい チャット", "new chat", "microsoft 365 copilot",
                   "m365 copilot", "copilot")
            if low in bad or s in ("Copilot", "Microsoft 365 Copilot"):
                return ""
            if len(s) > 120:                 # a runaway scrape (whole page text etc.)
                return ""
            return s

        # 1) a dedicated title node in the conversation header
        try:
            loc = self.page.locator(COPILOT_SELECTORS["conv_title"])
            n = min(loc.count(), 5)
            for i in range(n):
                try:
                    t = _clean(loc.nth(i).inner_text())
                except Exception:
                    t = ""
                if t:
                    return t
        except Exception:
            pass

        # 2) the browser tab title, minus the " - Microsoft 365 Copilot" style suffix
        try:
            dt = self.page.title() or ""
            for sep in (" | ", " - ", " — ", " · "):
                if sep in dt:
                    dt = dt.split(sep, 1)[0]
                    break
            t = _clean(dt)
            if t:
                return t
        except Exception:
            pass

        return ""


def run_relay(
    driver,
    goal: str,
    run_id: str = "relay",
    max_turns: int = 20,
    per_turn_timeout_s: int = 1800,
    max_no_progress: int = 3,
    max_timeouts: int = 2,
    notify=default_notify,
    sleep_s: float = 1.0,
    browser_context=None,
    research_model: str = "Claude",
    max_research: int = 3,
    throttle: bool = True,
    backoff_step_s: float = 20.0,
    backoff_max_s: float = 300.0,
    slow_factor: float = 2.5,
    checks=None,
    cwd: str | None = None,
    max_verify_attempts: int = 3,
    refuter: bool = False,
    max_refute: int = 2,
    review_lenses=None,
    forge: bool = False,
    max_forge: int = 3,
    max_transient: int = 10,
    agent_url: str | None = None,
    max_recycles: int = 8,
) -> str:
    """Run the autonomous loop unattended. Returns one of:
    DONE | STUCK | MAXTURNS | ABORTED. Notifies on every terminal outcome.

    Reliability guards (so a hands-off run never spins forever or dies silently):
      * per-turn completion timeout (max_timeouts consecutive -> STUCK)
      * no-progress detection: identical answer for max_no_progress turns -> STUCK
      * agent self-reported STUCK: -> STUCK
      * hard max_turns cap -> MAXTURNS
      * kill-switch (stop_check) every turn -> ABORTED
      * send/read exceptions -> STUCK (never crash unattended)
    Every turn is written to the run-log (operator D) and cross-session memory.

    Deep-dive delegation (spec §5): if `browser_context` is given and the agent
    writes a `RESEARCH: <query>` line, the relay opens the M365 Researcher agent in
    a side page, runs a Claude/Anthropic deep research, and feeds the report back
    into the implementation agent's next turn. Capped at `max_research` per run.
    """
    prior = memory_load(f"relay.{run_id}.context", scope="relay")
    context = "" if prior.startswith("[memory_load") else f"\n(前回までの文脈: {prior})\n"

    job = PROTOCOL + goal + context + (FORGE_HINT if forge else "")
    turn = 0
    no_progress = 0
    timeouts = 0
    research_count = 0
    analyze_count = 0
    forge_count = 0
    verify_attempts = 0
    transient = 0          # transient-failure retries (send/timeout/likely-transient STUCK)
    gen_waits = 0          # consecutive "previous turn still generating" reschedules.
    # This is NOT the transient budget: waiting out a slow (django/sympy) turn is not a
    # failure, so it must not count toward STUCK. A separate, generous cap only guards
    # against a turn that LITERALLY never stops generating (a wedged page) -- each wait is
    # already GEN_WAIT_S (~4min), so this many waits is a very long ceiling, after which we
    # treat it as a genuine stall. Reset to 0 once a send actually goes through.
    max_gen_waits = 30
    refute_count = 0
    checks_norm = normalize_checks(checks)      # spec 3-3 acceptance gate (empty -> trust DONE)
    backoff_s = 0.0          # adaptive throttle: extra cool-down added between turns
    base_elapsed = None      # fastest healthy turn so far -> the "not throttled" baseline
    last_norm = None
    recycles = 0           # fresh-conversation recycles after a token-limit exhaustion
    outcome: str | None = None
    reason = ""

    # max_turns=0 (or falsy) means unlimited -- progress-based guards still apply.
    while not max_turns or turn < max_turns:
        if stop_check().startswith("STOP"):
            outcome, reason = "ABORTED", "kill-switch"
            break
        turn += 1
        t_send = time.time()
        try:
            driver.send(job)
        except ConversationClosed as e:
            # The target tab/composer is gone (conversation ended). Retrying a dead
            # target can NEVER succeed, so this is terminal -- skip the transient-retry
            # budget entirely and stop now (prevents the 10x retry waste seen against
            # TargetClosed pages in send_failures.jsonl).
            runlog_append(run_id, {"turn": turn, "event": "conversation_closed",
                                   "detail": str(e)[:200]})
            outcome, reason = "STUCK", f"conversation closed: {e}"
            break
        except GenerationInProgress as e:
            # The PREVIOUS turn was still generating after send()'s generous wait. This is
            # NOT a failure -- the agent is just slow (django/sympy). Reschedule the SAME
            # job WITHOUT consuming a turn or the transient budget, so a slow turn can never
            # be counted into STUCK. A separate, very large cap only catches a page that
            # literally never stops generating.
            gen_waits += 1
            runlog_append(run_id, {"turn": turn, "event": "waiting_processing",
                                   "n": gen_waits, "detail": str(e)[:160]})
            print(f"[relay turn {turn}] previous turn still generating -> wait "
                  f"{gen_waits}/{max_gen_waits} (no budget consumed)")
            turn -= 1                                  # a deferred send didn't consume a turn
            if gen_waits >= max_gen_waits:
                outcome, reason = "STUCK", f"previous turn never stopped generating ({gen_waits} waits)"
                break
            time.sleep(sleep_s + backoff_s)
            continue
        except Exception as e:
            # a send failure is a transient (CDP/Edge/network) hiccup -- retry with backoff
            # rather than giving up (the relay analog of Claude Code retrying a request).
            if transient < max_transient:
                transient += 1
                runlog_append(run_id, {"turn": turn, "event": "transient_retry",
                                       "kind": "send", "n": transient})
                print(f"[relay turn {turn}] send failed -> transient retry "
                      f"{transient}/{max_transient}")
                turn -= 1                              # a failed send didn't consume a turn
                time.sleep(sleep_s + transient_backoff(transient))
                continue
            outcome, reason = "STUCK", f"send failed after {transient} retries: {type(e).__name__}: {e}"
            break
        gen_waits = 0          # a send actually went through -> reset the generation-wait count

        try:
            ok = driver.wait_for_idle(timeout_s=per_turn_timeout_s)
        except Exception as e:
            outcome, reason = "STUCK", f"wait failed: {type(e).__name__}: {e}"
            break
        turn_elapsed = time.time() - t_send

        # Adaptive throttle (spec §6): the laptop cannot see Microsoft's fair-use
        # ceiling directly, so infer "being throttled" from the agent's own
        # responsiveness -- a turn that times out, or runs much slower than the
        # fastest healthy turn, raises a cool-down added between turns; healthy
        # turns decay it. Every change is logged so "when did it start being
        # throttled" is visible in the run-log (operator D).
        if throttle:
            prev = backoff_s
            backoff_s, base_elapsed, t_reason = _adjust_backoff(
                ok, turn_elapsed, backoff_s, base_elapsed,
                backoff_step_s, backoff_max_s, slow_factor)
            if abs(backoff_s - prev) > 0.1:
                runlog_append(run_id, {"turn": turn, "event": "throttle", "reason": t_reason,
                                       "turn_elapsed_s": round(turn_elapsed, 1),
                                       "base_s": round(base_elapsed or 0, 1),
                                       "backoff_s": round(backoff_s, 1)})

        if not ok:
            timeouts += 1
            runlog_append(run_id, {"turn": turn, "event": "turn_timeout", "count": timeouts})
            if timeouts >= max_timeouts:
                outcome, reason = "STUCK", "turn did not finish (repeated timeout)"
                break
            job = NUDGE_JOB
            time.sleep(sleep_s + backoff_s)
            continue
        timeouts = 0

        try:
            resp = driver.read_last_response()
        except Exception as e:
            outcome, reason = "STUCK", f"read failed: {type(e).__name__}: {e}"
            break

        runlog_append(run_id, {"turn": turn, "job_excerpt": job[:160],
                               "response_excerpt": resp[:500]})
        memory_save(f"relay.{run_id}.turn{turn}", resp[:4000], scope="relay",
                    tags=["relay", run_id])
        print(f"[relay turn {turn}] {resp[:160].replace(chr(10), ' ')}")

        # ---- conversation-token-limit recovery (recycle to a fresh chat) ----
        # A hands-off run pumps ONE conversation; a long task eventually exhausts the
        # model's token budget (OpenAIModelTokenLimit) and then EVERY later turn returns
        # the same error. Detect it and start a NEW conversation on the SAME agent,
        # re-anchoring the goal so the agent re-derives progress from disk (the Excel /
        # output files). This does not consume a turn and is capped by max_recycles.
        if conversation_exhausted(resp):
            recycles += 1
            runlog_append(run_id, {"turn": turn, "event": "token_limit_recycle",
                                   "n": recycles, "detail": resp[:160]})
            if recycles > max_recycles:
                outcome, reason = "STUCK", (
                    f"conversation token limit hit; exceeded max_recycles={max_recycles}")
                break
            target = (agent_url or "").strip() or bare_agent_url(
                getattr(getattr(driver, "page", None), "url", "") or "")
            print(f"[relay turn {turn}] token limit -> recycling to fresh conversation "
                  f"({recycles}/{max_recycles}) at {target[:70]}")
            try:
                driver.page.goto(target, wait_until="domcontentloaded")
                for _ in range(30):
                    driver.page.wait_for_timeout(1000)
                    if driver.page.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                        break
            except Exception as e:  # noqa: BLE001
                outcome, reason = "STUCK", f"recycle navigation failed: {type(e).__name__}: {e}"
                break
            notify("♻ Relay 会話リサイクル",
                   f"トークン上限 → 新会話で続行 ({recycles}/{max_recycles})")
            # Re-anchor the goal as the first message of the fresh conversation.
            job = PROTOCOL + RECYCLE_PREFIX + goal + context + (FORGE_HINT if forge else "")
            no_progress = 0
            last_norm = None
            turn -= 1                       # the recycle itself is not a work turn
            time.sleep(sleep_s + backoff_s)
            continue

        norm = " ".join(resp.lower().split())[:300]
        no_progress = no_progress + 1 if norm and norm == last_norm else 0
        last_norm = norm

        # ---- deep-dive delegation (spec §5: researcher node) ----
        rq = extract_research(resp)
        if rq and browser_context is not None:
            research_count += 1
            if research_count > max_research:
                job = ("これ以上は調査を依頼できません（上限到達）。今ある情報で進めるか、"
                       "無理なら最後の行に STUCK: 理由 と書いてください。")
                time.sleep(sleep_s)
                continue
            notify("🔎 Relay 調査開始", rq[:80])
            runlog_append(run_id, {"turn": turn, "event": "research_start", "query": rq[:200]})
            print(f"[relay turn {turn}] -> RESEARCH delegated: {rq[:80]}")
            rres = {"ok": False, "result": "", "error": "not run"}
            rpage = None
            try:
                from .agent_profiles import RESEARCHER, ask_agent
                rpage = browser_context.new_page()
                rres = ask_agent(rpage, rq, RESEARCHER, model_name=research_model)
            except Exception as e:
                rres = {"ok": False, "result": "", "error": f"{type(e).__name__}: {e}"}
            finally:
                try:
                    if rpage is not None:
                        rpage.close()
                except Exception:
                    pass
                try:
                    driver.page.bring_to_front()
                except Exception:
                    pass
            report = (rres.get("result") or "")[:3500] if rres.get("ok") else ""
            runlog_append(run_id, {"turn": turn, "event": "research_done",
                                   "ok": bool(rres.get("ok")), "len": len(report),
                                   "elapsed_s": rres.get("elapsed_s"),
                                   "error": rres.get("error", "")})
            print(f"[relay turn {turn}] <- RESEARCH done ok={rres.get('ok')} "
                  f"len={len(report)} elapsed={rres.get('elapsed_s')}s")
            if report:
                job = ("依頼された調査が完了しました。以下が結果です。これを踏まえて作業を続けてください。\n"
                       f"--- 調査結果 ---\n{report}\n--- 調査結果ここまで ---\n" + CONTINUE_JOB)
            else:
                job = (f"調査を試みましたが結果を取得できませんでした（{rres.get('error', 'timeout/empty')}）。"
                       "調査結果なしで可能な範囲で進めるか、無理なら最後の行に STUCK: 理由 と書いてください。")
            time.sleep(sleep_s + backoff_s)
            continue
        # ---- end deep-dive delegation ----

        # ---- data-analysis delegation (spec §5: analyst node) ----
        az = extract_analyze(resp)
        if az and browser_context is not None:
            apath, ainstr = az
            analyze_count += 1
            if analyze_count > max_research:
                job = ("これ以上は分析を依頼できません（上限到達）。自前ツールで分析するか、"
                       "無理なら最後の行に STUCK: 理由 と書いてください。")
                time.sleep(sleep_s)
                continue
            notify("📊 Relay 分析開始", apath[:80])
            runlog_append(run_id, {"turn": turn, "event": "analyze_start", "file": apath[:200]})
            print(f"[relay turn {turn}] -> ANALYZE delegated: {apath[:80]}")
            ares = {"ok": False, "result": "", "error": "not run"}
            apage = None
            try:
                from .agent_profiles import ANALYST, analyze
                apage = browser_context.new_page()
                ares = analyze(apage, apath, ainstr, ANALYST)
            except Exception as e:
                ares = {"ok": False, "result": "", "error": f"{type(e).__name__}: {e}"}
            finally:
                try:
                    if apage is not None:
                        apage.close()
                except Exception:
                    pass
                try:
                    driver.page.bring_to_front()
                except Exception:
                    pass
            rep = (ares.get("result") or "")[:3000] if ares.get("ok") else ""
            runlog_append(run_id, {"turn": turn, "event": "analyze_done",
                                   "ok": bool(ares.get("ok")), "len": len(rep),
                                   "elapsed_s": ares.get("elapsed_s"),
                                   "error": ares.get("error", "")})
            print(f"[relay turn {turn}] <- ANALYZE done ok={ares.get('ok')} len={len(rep)}")
            if rep:
                job = ("依頼した分析が完了しました。以下が結果です。**数値は鵜呑みにせず、必ず "
                       "run_python / read_excel などの自前ツールで再計算して地上検証してから**使ってください。\n"
                       f"--- 分析結果 ---\n{rep}\n--- 分析結果ここまで ---\n" + CONTINUE_JOB)
            else:
                job = (f"分析を試みましたが結果を取得できませんでした（{ares.get('error', 'timeout/empty')}）。"
                       "自前ツールで分析するか、無理なら最後の行に STUCK: 理由 と書いてください。")
            time.sleep(sleep_s + backoff_s)
            continue
        # ---- end data-analysis delegation ----

        # ---- tool foundry (operator A): the agent forges a reusable tool ----
        fg = extract_forge(resp) if forge else None
        if fg:
            fname, fcode = fg
            forge_count += 1
            if forge_count > max_forge:
                job = ("これ以上は新しいツールを作成できません（上限）。既存のツールで作業を続けてください。"
                       + CONTINUE_JOB)
                time.sleep(sleep_s + backoff_s)
                continue
            try:
                from tools.foundry import forge_core
                fres = forge_core(fname, fcode)
            except Exception as e:
                fres = f"[forge error: {type(e).__name__}: {e}]"
            runlog_append(run_id, {"turn": turn, "event": "forge", "name": fname,
                                   "result": fres[:200]})
            print(f"[relay turn {turn}] FORGE {fname}: {fres[:100]}")
            job = ("ツール作成結果: " + fres + "\n（このツールは枠側では利用可、Copilot からは"
                   "サーバ再起動後に利用可能。）今は既存のツールで作業を続けてください。" + CONTINUE_JOB)
            time.sleep(sleep_s + backoff_s)
            continue
        # ---- end tool foundry ----

        up = resp.upper()
        last_line = (resp.strip().splitlines() or [""])[-1].upper()

        if reported_stuck(resp):
            # under load an agent STUCK is usually a downstream symptom of a transient
            # tool/network failure -- retry the turn before giving up, with backoff.
            if transient < max_transient:
                transient += 1
                runlog_append(run_id, {"turn": turn, "event": "transient_retry",
                                       "kind": "stuck", "n": transient})
                job = RETRY_JOB
                time.sleep(sleep_s + transient_backoff(transient))
                continue
            outcome, reason = "STUCK", f"agent reported STUCK (after {transient} retries)"
            break
        transient = 0          # a real (non-stuck) response -> the transient issue cleared
        if "DONE" in up and "FAIL" not in last_line:
            # spec 3-3 verification GATE: never trust a self-reported DONE when the goal
            # carries acceptance checks. Re-derive ground truth locally; on fail, hand the
            # agent the REAL output and keep working (bounded retries).
            if checks_norm:
                passed, detail = run_all_blocking(checks_norm, cwd=cwd)
                runlog_append(run_id, {"turn": turn, "event": "verify", "passed": passed,
                                       "attempt": verify_attempts + 1, "detail": detail[:400]})
                print(f"[relay turn {turn}] verify {'PASS' if passed else 'FAIL'}: {detail[:120]}")
                if not passed:
                    verify_attempts += 1
                    if verify_attempts >= max_verify_attempts:
                        outcome = "STUCK"
                        reason = f"acceptance check failed {verify_attempts}x: {detail[:200]}"
                        break
                    job = VERIFY_FIX_JOB % (detail or "(no detail)")
                    time.sleep(sleep_s + backoff_s)
                    continue
            # spec 4B refuter (operator B): machine checks passed (or none) -> a CANDIDATE
            # DONE. Optionally have an independent reviewer try to refute it before we
            # accept. A real refutation is fed back; otherwise the DONE stands. OFF by
            # default + budget-capped (it doubles oracle cost).
            if refuter and browser_context is not None and refute_count < max_refute:
                refute_count += 1
                from .refuter import run_refuter, aggregate_panel
                conv_url = ""
                try:
                    conv_url = driver.page.url
                except Exception:
                    pass
                if review_lenses:
                    # perspective-diverse panel: N independent reviewers, majority vote
                    panel = []
                    for lens in review_lenses:
                        k, r = run_refuter(browser_context, conv_url, goal, resp,
                                           notify=notify, runlog=runlog_append,
                                           run_id=run_id, turn=turn, lens=lens)
                        panel.append((lens, k, r))
                    kind, rreason = aggregate_panel(panel)
                    runlog_append(run_id, {"turn": turn, "event": "panel",
                                           "verdict": kind, "votes": [p[1] for p in panel]})
                else:
                    kind, rreason = run_refuter(browser_context, conv_url, goal, resp,
                                                notify=notify, runlog=runlog_append,
                                                run_id=run_id, turn=turn)
                try:
                    driver.page.bring_to_front()
                except Exception:
                    pass
                print(f"[relay turn {turn}] refuter: {kind} {rreason[:100]}")
                if kind == "REFUTED":
                    notify("🧐 Relay 反証あり", rreason[:80])
                    job = REFUTE_FIX_JOB % rreason
                    time.sleep(sleep_s + backoff_s)
                    continue
            outcome = "DONE"
            break
        if no_progress >= max_no_progress:
            outcome, reason = "STUCK", f"no progress for {no_progress + 1} turns"
            break
        if "FAIL" in last_line:
            job = FIX_JOB
        else:
            job = CONTINUE_JOB
        time.sleep(sleep_s + backoff_s)

    if outcome is None:
        # Only reachable when max_turns > 0 (unlimited runs exit via DONE/STUCK/ABORTED).
        outcome, reason = "MAXTURNS", f"reached max_turns={max_turns} without DONE"

    memory_save(f"relay.{run_id}.context",
                f"last_status={outcome} turns={turn} reason={reason}",
                scope="relay", tags=["relay", run_id])

    titles = {
        "DONE":     ("✅ Relay 完了", f"ゴール達成 ({turn} ターン): {goal[:120]}"),
        "STUCK":    ("⚠ Relay 停止 (要確認)", f"{reason} / {turn} ターンで停止"),
        "MAXTURNS": ("⏹ Relay 上限到達", f"{turn} ターンで DONE に至らず"),
        "ABORTED":  ("⏹ Relay 中止", "kill-switch により停止"),
    }
    title, body = titles.get(outcome, ("Relay", outcome))
    notify(title, body)

    print("\n--- run-log (operator D) ---")
    print(runlog_summarize(run_id))
    print(f"\nrelay finished: {outcome} ({reason}) in {turn} turn(s). "
          f"History in memory scope 'relay'. Notification sent.")
    return outcome


def find_conversation_page(context, conversation_url: str):
    """Always load the target URL fresh (a bare agent URL starts a NEW chat) and
    wait for the composer to render before returning."""
    pg = context.pages[0] if context.pages else context.new_page()
    pg.goto(conversation_url, wait_until="domcontentloaded")
    for _ in range(30):
        pg.wait_for_timeout(1000)
        if pg.locator(COPILOT_SELECTORS["composer"]).count() > 0:
            break
    return pg


def main():
    ap = argparse.ArgumentParser(description="Autonomous M365 Copilot relay (hands-off, non-interfering).")
    ap.add_argument("--cdp-url", default="http://localhost:9222",
                    help="CDP endpoint of an Edge/Chrome started with --remote-debugging-port")
    ap.add_argument("--conversation-url", required=True,
                    help="URL of the (new) Copilot agent conversation to drive")
    ap.add_argument("--goal", required=True, help="The goal to pursue autonomously")
    ap.add_argument("--run-id", default="relay", help="Identifier for run-log + memory keys")
    ap.add_argument("--max-turns", type=int, default=12)
    ap.add_argument("--per-turn-timeout", type=int, default=1800,
                    help="Max seconds to wait for a single agent turn to finish")
    ap.add_argument("--no-research", action="store_true",
                    help="Disable RESEARCH: delegation to the Researcher agent")
    ap.add_argument("--check", default=None,
                    help="acceptance check(s) as JSON -- a single check object or a list. "
                         'e.g. \'{"type":"shell","cmd":"python -m pytest -q"}\'. When set, a '
                         "self-reported DONE is verified locally before the run is accepted "
                         "(spec 3-3 gate); on failure the real output is fed back to the agent.")
    ap.add_argument("--check-cwd", default=None,
                    help="working directory the acceptance check(s) run in (default: repo)")
    ap.add_argument("--refuter", action="store_true",
                    help="operator B: after a candidate DONE, have an INDEPENDENT Copilot "
                         "review try to refute it; a real refutation is fed back. Doubles "
                         "oracle cost, so off by default and capped (--max-refute).")
    ap.add_argument("--max-refute", type=int, default=2,
                    help="max refuter rounds per run (default 2)")
    ap.add_argument("--panel", action="store_true",
                    help="review with a perspective-diverse PANEL (correctness / edge "
                         "cases / security), majority vote, instead of one reviewer. "
                         "Implies --refuter. More thorough, ~3x the review cost.")
    ap.add_argument("--forge", action="store_true",
                    help="operator A: let the agent forge reusable tools mid-task with a "
                         "FORGE: <name> + ```python``` block. Staged + compile-checked under "
                         "tools/auto/ (never executed); Copilot sees them after a restart.")
    args = ap.parse_args()

    checks = None
    if args.check:
        import json
        try:
            checks = json.loads(args.check)
        except Exception as e:
            ap.error(f"--check is not valid JSON: {e}")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(args.cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = find_conversation_page(context, args.conversation_url)
        page.bring_to_front()
        driver = CopilotWebDriver(page)
        from relay.refuter import PANEL_LENSES
        run_relay(driver, args.goal, args.run_id, args.max_turns,
                  per_turn_timeout_s=args.per_turn_timeout,
                  browser_context=None if args.no_research else context,
                  checks=checks, cwd=args.check_cwd,
                  refuter=args.refuter or args.panel, max_refute=args.max_refute,
                  review_lenses=list(PANEL_LENSES) if args.panel else None,
                  forge=args.forge)


if __name__ == "__main__":
    main()

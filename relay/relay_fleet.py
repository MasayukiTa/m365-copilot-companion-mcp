"""relay_fleet.py -- run N AUTONOMOUS relays in parallel (spec §1 fleet x §3/§4 loop).

Where the official Cowork is one autonomous track per user, this drives MANY goals
at once: N Copilot conversations, each pursued to DONE by its own deterministic
relay loop, advanced from a single thread in a non-blocking round-robin. While the
client does one cheap poll, all N agents are thinking server-side in parallel, so
their (slow) turns overlap -- that's the throughput edge.

MEMORY DISCIPLINE (why this is not just "open N tabs"):
  Each M365 Copilot tab is a heavy SPA (~0.3-0.6 GB). On a 16 GB laptop already
  running other work, opening many at once exhausts RAM -- Edge then crashes, and
  when it auto-restarts WITHOUT --remote-debugging-port the CDP endpoint is gone and
  the whole run dies (observed). So this fleet:
    * never opens all N tabs up front -- it keeps at most `max_concurrent` open,
    * sizes `max_concurrent` to *available* physical memory (GlobalMemoryStatusEx),
    * CLOSES each conversation's tab the instant it reaches a terminal state, which
      frees that RAM and lets the next queued goal open. Resuming = just run again;
      a fresh tab is opened for each goal.

Each worker reuses the same loop policy as run_relay (PROTOCOL framing; decide
DONE / STUCK / no-progress / FAIL->fix / CONTINUE per turn) but as a non-blocking
state machine so the open ones interleave. No threads, no async.

  results = run_relay_fleet(context, [goalA, goalB, goalC], agent_url)
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import time

from .acceptance import Check, normalize_checks, run_all_blocking
from .copilot_autopilot_relay import (
    CONTINUE_JOB, COPILOT_SELECTORS, ConversationClosed, CopilotWebDriver, FIX_JOB,
    GenerationInProgress, PROTOCOL, REFUTE_FIX_JOB, RETRY_JOB, VERIFY_FIX_JOB,
    _is_processing, default_notify, extract_analyze, extract_research, goal_not_seen,
    has_end_marker,
    reported_stuck, transient_backoff, conversation_exhausted, RECYCLE_PREFIX,
    conversation_start_label,
)
from relay import settle as _settle
from .planner import PLAN_PROMPT, extract_plan, opening_turn, plan_ready
from .review_resilience import (
    freeze_goal_dict, looks_like_policy_refusal, same_task_envelope,
    task_envelope_from_goal,
)

TERMINAL = (
    "done", "stuck", "maxturns", "error", "cancelled",
    "content_refused", "unresolved_refusal",
)
# non-terminal but not yet occupying a tab; counts as "still running" for the loop.
PENDING = "pending"

# Replies that mean the Copilot AGENT/PATH is down, NOT that the task failed. The error number /
# session-id / timestamp vary, but the prose is stable in both EN and JP. Stored lowercase; JP is
# unaffected by .lower(), so a single `marker in resp.lower()` covers both languages.
#
# SPLIT (2026-07 fix #2): these used to be ONE list (AGENT_DEAD_MARKERS) whose past-window branch
# both rode out AND, once past the window, declared "agent stopped/disabled" for either kind of
# match. That conflated two very different situations:
#   * TRANSIENT_ERROR_MARKERS -- GENERIC "something broke, try again" boilerplate that Copilot
#     also emits for an ordinary network/tunnel blip or a drifted tab. It says nothing about
#     WHICH agent or whether it's disabled -- it is the same string a healthy agent shows during
#     a passing outage. Declaring "stopped/disabled" off this alone was the FALSE POSITIVE (the
#     desktop toast a previous change wrongly deleted instead of fixing).
#   * ADMIN_BLOCK_MARKERS -- the narrower "an administrator needs to look at THIS" family. This is
#     the actual signal for a per-agent admin block (stopped/disabled in Copilot Studio) -- but
#     only trustworthy when our own path to the tools is confirmed healthy (see _infra_healthy),
#     since a network outage can make even this text appear as a generic-looking error page.
# AGENT_DEAD_MARKERS is kept as the union so the existing branch-entry condition
# (`any(m in _low for m in AGENT_DEAD_MARKERS)`) is unchanged; the branch body below now looks at
# WHICH sub-list actually matched to decide whether the desktop "disabled" toast may fire.
TRANSIENT_ERROR_MARKERS = (
    "予期しないエラー", "システムエラー", "systemerror", "unexpected error",
    "something went wrong", "ページをもう一度読み込", "reload the page", "try reloading",
    "問題が解決しない場合", "if the problem persists",
)
ADMIN_BLOCK_MARKERS = (
    "管理者に問い合わせ", "contact your administrator", "contact the administrator",
)
AGENT_DEAD_MARKERS = TRANSIENT_ERROR_MARKERS + ADMIN_BLOCK_MARKERS

# NETWORK-OUTAGE RESILIENCE (2026-06-17). A flaky corporate network / devtunnel can drop the path
# to the MCP backend for seconds-to-minutes. The retry budgets must be WALL-CLOCK windows, not tiny
# counts: a 10-count transient retry exhausted in ~55s and a 3-strike dead-agent detector STUCK in
# seconds, so a brief blip "ended everything". These windows let a worker RIDE OUT an outage (keep
# retrying with backoff) and give up only if the failure PERSISTS past the window -- at which point
# it really is a down/banned agent. Env-tunable.
NET_RETRY_WINDOW_S = float(os.environ.get("MCP_NET_RETRY_S", "1800"))      # send/CDP/network: 30 min
AGENT_ERR_WINDOW_S = float(os.environ.get("MCP_AGENT_ERR_S", "1200"))     # agent SystemError: 20 min

# DEAD-ENDPOINT EARLY-EXIT (2026-07 fix). NET_RETRY_WINDOW_S rides out a REAL outage, which by
# definition produces CHANGING symptoms over time (different errors, eventual recovery). If a
# STUCK reply instead repeats byte-identical turn after turn, the endpoint isn't flapping -- it is
# dead for this goal, and burning the full 30-min window against it just delays the inevitable
# terminal STUCK. Reuses the EXISTING no-progress/normalization signal (self.no_progress, computed
# in _decide from `norm == self.last_norm`) rather than a new response-comparison. Small K so a
# couple of coincidental identical replies from a slow-but-alive agent don't false-trip, but a
# genuinely wedged endpoint stops burning wall-clock quickly.
NET_RETRY_NOPROGRESS_MAX = int(os.environ.get("MCP_NET_RETRY_NOPROGRESS_MAX", "3"))

# TOOL-BACKEND-UNREACHABLE detector. When the MCP tool path (devtunnel) drops for even a moment, the
# agent's tool calls fail and it WRONGLY concludes its tools don't exist / aren't assigned and self-
# locks ("再試行では解消しません / won't respond without new input"). That is INFRA-FALSE (the tools
# DO exist; the network blipped), NOT a genuine STUCK -- but the agent's own STUCK was being accepted
# as a terminal miss. Detect it, RE-SEND THE GOAL (the "new input" it demands) to ride out the blip,
# and only give up (as a re-queueable infra stuck, NOT a miss) after the wall-clock window.
TOOL_UNREACHABLE_MARKERS = (
    "ツールが存在しない", "ツールが割り当てられ", "ツールがこのセッションに割り当て",
    "ツールが存在しないため", "再試行では解消", "再試行では解決しません",
    "恒常的制約", "構造的制約のため", "当環境へのツール有効化", "ツール有効化、または",
    "no tools are assigned", "tools are not available", "tool is not available to this session",
)

# CONNECTION-CONSENT detector. The FIRST time the agent calls an MCP tool, Copilot can show a
# connection-consent card ("この資格情報を 接続マネージャーを開く で検証してください ... 再試行")
# instead of executing -- the MCP connector's per-user connection is not authorized yet. This is
# NOT a dead agent and NOT a task failure. Regulation: consent must be resolved FULLY
# AUTOMATICALLY (Allow-button tier0 -> re-nav -> popup click-through); surfacing the dedicated
# Edge is the LAST RESORT, firing ONLY once every automatic tier has genuinely failed, and then
# it must land on the correct agent conversation (not the top page) and the notify must be
# truthful (gated on surface()'s real return value).
CONSENT_MARKERS = (
    "接続マネージャー", "この資格情報を", "接続の準備が整ったら", "接続して続行する",
    "open connection manager", "connection manager", "verify this credential",
    "verify your credential", "authorize the connection", "set up this connection",
)
# After the last-resort surface() succeeds, how many extra consent-card sightings we tolerate
# (retrying each time) before concluding the user hasn't approved yet and giving up for good --
# bounded so a successful surface can never turn into an infinite retry loop.
CONSENT_SURFACE_RETRY_MAX = int(os.environ.get("MCP_CONSENT_SURFACE_RETRY_MAX", "3"))

# BUG 4b fix: bounded safety net so a surface()'d dedicated Edge can NEVER stay foreground
# forever, even when the normal rehide()-on-resolution pairing is missed. threading.Timer is
# one-shot (not a persistent daemon loop) -- started right after every surface() call and
# cancelled if a real rehide() fires first. Env-tunable like this file's other windows.
CONSENT_SURFACE_FORCE_REHIDE_SEC = float(os.environ.get("MCP_FORCE_REHIDE_SEC", "90"))


# Work IQ surfaces its connection consent as a CHAIN of cards, not one: seven as measured
# on 2026-08-10 (User, Copilot, Teams, SharePoint, OneDrive, Mail, Calendar), each
# appearing only after the previous one is approved. The cap is the chain length plus
# headroom, so a card that re-renders instead of resolving cannot spin.
CONSENT_CHAIN_MAX = int(os.environ.get("MCP_CONSENT_CHAIN_MAX", "12"))


def _schedule_force_rehide(timeout=None):
    """Start a one-shot background timer that force-rehides the dedicated Edge after `timeout`
    seconds (default CONSENT_SURFACE_FORCE_REHIDE_SEC). Safety net for BUG 4a/4b: covers every
    surface() call site in this file (the consent-exhaustion last resort AND the sign-in
    surfaces, as defense-in-depth) so the window can never stay foreground indefinitely even if
    a caller's own rehide() is skipped. Exception-guarded; the Timer thread is daemon=True so it
    never blocks process exit. Returns the Timer so the caller can _cancel_force_rehide() it
    once a normal rehide() has already happened. ASCII-only print (this module has no logger)."""
    t = CONSENT_SURFACE_FORCE_REHIDE_SEC if timeout is None else timeout

    def _safe_rehide():
        try:
            from .edge_recover import rehide
            rehide()
            print("[relay_fleet] force-rehide safety net fired after %.0fs" % t)
        except Exception:
            print("[relay_fleet] force-rehide safety net: rehide() raised")

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

# CANNED-NONANSWER detector (headless->default-Copilot fallback, 2026-07-03). When the companion
# Edge runs --headless and its window-size/state wedges, the M365 SPA fails to resolve the
# ?titleId= custom agent and SILENTLY falls back to DEFAULT Copilot (which has NO MCP connector),
# so every tool call fails and the agent replies with a fixed non-answer -- "申し訳ございません。
# それに応答できませんでした" / "I couldn't respond to that". This matches NEITHER a consent card
# NOR a tool-unreachable message, so it fell through every recovery and looped forever. Detected
# here so we can (a) surface for sign-in if it is a login wall, (b) re-nav off the default-Copilot
# fallback, or (c) as a last resort force a HEADED relaunch of the companion Edge.
# Substring / locale-tolerant match.
CANNED_NONANSWER_MARKERS = (
    "それに応答できませんでした",
    "I couldn't respond to that",
    "I can't respond to that",
)
# How long (wall clock) to keep riding out a login-wall canned-non-answer streak before giving up
# as INFRA_STUCK (sign-in required). Mirrors the AGENT_ERR_WINDOW_S style of bounded-but-generous
# infra windows. Env-tunable.
CANNED_LOGIN_WINDOW_S = float(os.environ.get("MCP_CANNED_LOGIN_S", "600"))
# Consecutive canned-non-answers (login-wall case) tolerated before INFRA_STUCK, as a secondary
# guard against a pathological tight loop with little elapsed time.
CANNED_LOGIN_MAX = int(os.environ.get("MCP_CANNED_LOGIN_MAX", "6"))


def _companion_cdp_port():
    """CDP port of the dedicated companion Edge, derived from the SAME config the fleet's
    attach path uses (MCP_CDP_URL, default http://localhost:9222) -- never a new hardcoded
    literal. Used only for the last-resort headed relaunch (edge_recover.surface(port=...)).
    Never raises -> falls back to 9222 on any parse error."""
    try:
        url = os.environ.get("MCP_CDP_URL", "http://localhost:9222")
        return int(url.rsplit(":", 1)[-1].split("/")[0])
    except Exception:
        return 9222


# Thin, fully-guarded wrappers around edge_recover.{looks_like_login,surface}. Imported LAZILY
# (the rest of this file imports edge_recover inside functions, and edge_recover only pulls
# playwright inside its own tab-closing helpers -- so these are cheap) and never raise, so the
# canned-non-answer handler in _decide stays exception-safe.
def edge_recover_looks_like_login(url):
    try:
        from .edge_recover import looks_like_login
        return looks_like_login(url)
    except Exception:
        return False


def edge_recover_surface(port=None, open_url=""):
    """Surface (foreground / headed-relaunch) the companion Edge. Passes `port` through only
    when given, so surface()'s own default (9222) still applies when we don't need a specific
    port. `open_url` (optional) is forwarded to surface()'s open_url -- on a genuine sign-in
    surface, callers should pass the agent URL they were driving so a headed relaunch lands on
    that conversation instead of the launcher's default top page. "" preserves old behavior.
    Returns True/False; never raises."""
    try:
        from .edge_recover import surface
        kwargs = {"open_url": open_url} if open_url else {}
        return bool(surface(port, **kwargs) if port is not None else surface(**kwargs))
    except Exception:
        return False


# UNLOCK-REQUIRED detector. Write/exec MCP tools require unlock(password) per client IP
# (tools/security.py::require_unlocked). When the agent calls a write/exec tool before the
# (rotating M365 backend) IP is unlocked, the server returns ONE of its two literal error
# strings (tools/security.py require_unlocked(), ~line 129 and ~line 138):
#   "[locked: no HTTP request context] Denied: this call ran in-process (test, CLI, or an internal hook), not through the MCP HTTP server. unlock() cannot help here -- it needs the same HTTP context and will fail the same way; do not retry it. Either route the call through the HTTP server, or use an internal *_local path that does not pass this gate (memory_save_local / runlog_append_local)."
#   "[locked client IP: 'x.x.x.x'] Mutating and execution tools require an unlock. Call
#    unlock(password='<password>') first. The unlock is stored per client IP for
#    MCP_UNLOCK_TTL_DAYS days."
# which the agent echoes. We AUTO-INJECT the unlock: re-anchor the turn to first call the
# 'unlock' tool with MCP_UNLOCK_PASSWORD read LOCALLY from .env -- deliberately NOT baked into
# the agent's Copilot Studio instructions (that would expose the password permanently). The
# password appears only in this transient turn. Bounded: the backend IP can rotate and re-lock,
# so a few auto-unlocks are normal; past the cap we STUCK with an actionable reason.
#
# FALSE-POSITIVE FIX (2026-07-13): the original markers included the bare "call unlock(password"
# / "unlock(password=" substrings as SUFFICIENT triggers on their own. Those phrases are the tail
# of the real server error, but they ALSO appear whenever a worker's own PROSE discusses or
# reviews the unlock() API -- PROVEN live: a fleet security-review worker examining
# tools/security.py wrote a response describing the code ("unlock(password='<password>')の
# プレースホルダとテスト値"), which false-matched and made the relay auto-unlock 4x, then go
# STUCK with a misleading "M365 backend IP rotates" reason. A worker's analytical prose is long;
# the server's real lock error is the ENTIRE (short) tool-call return value. So detection now
# requires BOTH:
#   1. a DISTINCTIVE marker -- the "[locked ...]" bracket prefix the server actually emits.
#      This is the phrase least likely to be reproduced verbatim by prose ABOUT the API.
#   2. DOMINANCE -- the response is short (below LOCKED_DOMINANCE_MAX_CHARS), i.e. it looks like
#      the raw tool error rather than a long analysis that merely quotes/mentions it.
# The loose "unlock(password=" phrasing is kept only as documentation of what NOT to use alone;
# it is deliberately NOT part of LOCKED_MARKERS below.
LOCKED_MARKERS = ("locked client ip", "[locked:")
# A real lock error (see the two literal strings above) is ~90-230 chars. A security-review /
# analytical response that merely mentions unlock() runs to many hundreds/thousands of chars.
# Chosen well above the longest real error and well below a genuine multi-sentence review.
LOCKED_DOMINANCE_MAX_CHARS = 400
MAX_UNLOCK_ATTEMPTS = int(os.environ.get("MCP_FLEET_MAX_UNLOCK", "4"))
UNLOCK_PREFIX = (
    "【要解錠】書込/実行ツールは接続のIP単位ロック解除が必要です。まず最初に call_tool で "
    "'unlock' ツールを引数 {\"password\": \"%s\"} で1回だけ実行し、解錠に成功したら（以後その"
    "接続で書込/実行ツールが使えるので）当初のゴールをそのまま続行してください。解錠後は "
    "password を二度と出力しないこと。\n--- 元のゴール ---\n"
)


#: The exact prefix tools/security.py writes when it denies a caller that arrived with no HTTP
#: request context. Pinned here because that module is frozen and cannot import from this one,
#: and because a filter keyed on it is only as good as the literal staying identical -- a test
#: asserts the two match rather than trusting the copy.
NO_CONTEXT_REFUSAL = "[locked: no HTTP request context]"


def _looks_locked(resp: str, since: float = 0.0) -> bool:
    """True iff `resp` looks like the SERVER's require_unlocked() lock error, not a worker's
    prose that merely discusses/quotes the unlock() API (see the FALSE-POSITIVE FIX comment
    above LOCKED_MARKERS for the incident this guards against: a security-review worker
    describing tools/security.py false-tripped the old loose markers).

    Deterministic two-part rule (both required):
      1. DISTINCTIVE marker present -- one of LOCKED_MARKERS, the "[locked ...]" bracket prefix
         the server actually emits. Prose about the API rarely reproduces this exact bracket.
      2. DOMINANCE -- len(resp) < LOCKED_DOMINANCE_MAX_CHARS. The genuine server error IS the
         entire (short) tool-call return value; a long analytical response merely mentioning
         unlock(password=...) is not.
    """
    low = (resp or "").lower()
    if any(m in low for m in LOCKED_MARKERS):
        hit = len(resp or "") < LOCKED_DOMINANCE_MAX_CHARS
        if hit:
            _note_locked("marker", resp, since, None)
        return hit

    # The marker rule only fires while the agent pastes the tool error back
    # verbatim. It often does not: the operator discipline injected into every
    # turn tells it to write "淡々と事実とタスク結果のみ", so a real lock comes
    # back paraphrased -- "unlock パスワード欠如で確定。STUCK: unlock パスワード
    # 未提供。" -- carrying no marker at all. Detection missed, the generic retry
    # nudge ran instead of the unlock injection, and the run STUCKed asking a
    # human for a password already sitting in .env.
    #
    # So fall back to the server's own record. Whether a call was refused for
    # lock is a server fact, known exactly at the point of refusal; it does not
    # need to be recovered from prose. Freshness is what keeps this honest -- an
    # old refusal must not colour an unrelated later turn.
    # `since` is the moment this turn was sent. Without it a refusal from an unrelated
    # earlier call would mark the next few minutes of replies as locked -- CI caught
    # exactly that: one test triggered require_unlocked(), and a later test's ordinary
    # refusal reply ("I cannot assist with that request") was then read as a lock.
    if since <= 0.0:
        return False
    # THE SAME DOMINANCE RULE THE MARKER BRANCH USES. Without it this branch judged replies of
    # any length: a 533-character summary of a meeting was classified as a lock error because
    # some OTHER concurrent worker had been refused within the freshness window. The refusal
    # record is a single global slot with no client identity, so under concurrency one caller's
    # refusal colours everyone's reply -- and a long, ordinary answer is exactly what the
    # dominance rule exists to exclude. Identity is the real fix and it is not available here;
    # this removes the case that fired.
    if len(resp or "") >= LOCKED_DOMINANCE_MAX_CHARS:
        return False
    try:
        from tools import lock_state
        records = lock_state.matching_records(since)
        if not records:
            return False
        # A CONTEXT-LESS REFUSAL IS NOT EVIDENCE ABOUT THIS WORKER, and the test for one is the
        # server's own prefix rather than a blank client_ip.
        #
        # THE FIRST VERSION KEYED ON THE BLANK IP AND WAS A HOLE. derive_identity returns an
        # empty identity for a REAL request whose X-Forwarded-For holds only separators -- and
        # that header is set by the caller. So a remote caller could mint blank-ip refusals at
        # will and switch this branch off, blinding lock detection for every worker. Worse
        # than the bug it was fixing: a visible STUCK became an answer produced under a lock
        # nobody noticed.
        #
        # The prefix below is written by security.py itself and no caller influences it. Only
        # the no-context branch emits it; a genuine remote refusal says "[locked client IP: ..."
        # even when that IP is blank, and is treated as possibly this worker's -- which fails
        # towards the bounded, visible unlock/STUCK path rather than towards a silent one.
        #
        # ASKED OF EVERY REFUSAL IN THE WINDOW, not of one. The slot this used to read holds a
        # single record, so a context-less refusal landing after a genuine one hid the genuine
        # one and this branch answered "not locked" while a real lock stood.
        mine = [r for r in records
                if not str(r.get("detail") or "").startswith(NO_CONTEXT_REFUSAL)]
        if not mine:
            return False
        # Name the record actually decided on, not merely the last one to arrive. The note is
        # the only way to check afterwards whether a classification had evidence behind it.
        _note_locked("fallback", resp, since, mine[-1])
        return True
    except Exception:
        return False


def _note_locked(branch, resp, since, consumed):
    """Say which branch classified a reply as locked, and on what evidence. Never raises.

    Written because the last incident could not be reconstructed: five workers reported an
    unlock that would not hold, and nothing recorded WHICH test had fired or which refusal it
    had read. The fallback in particular consumes a record written by some other caller, and
    until now that record vanished behind a boolean.
    """
    try:
        from tools import lock_state
        lock_state.record_classification(branch, resp_len=len(resp or ""), since=since,
                                         consumed=consumed)
    except Exception:
        pass


def _unlock_password():
    """The unlock password, read LOCALLY (process env or .env) -- never stored in the agent
    config. Returns '' if unset."""
    # Single implementation, shared with the bridge: this fallback used to live only
    # here, which is why auto-unlock worked for fleet runs and never for the main chat.
    from tools.secret_store import unlock_password_local
    return unlock_password_local()


def _initial_job_with_unlock(goal: str, plan_mode: bool = False):
    """Build the first worker turn with a proactive unlock when local credentials exist.

    Waiting for a write/exec tool to fail is too late: the agent may give up before it has
    discovered the usable tool set.  The unlock must still be called by the M365-side agent
    because the gate is keyed to that remote client IP, so the password is injected only into
    this transient first turn and never into persistent agent configuration.
    """
    # plan_mode (operator-set, plan-then-WAIT) is unchanged. When it is off, which version
    # of the planner component opens the turn is the evolvable choice -- see
    # planner.PLANNER_VERSIONS for why an unattended plan-first arm is the comparable one.
    if plan_mode:
        original = PLAN_PROMPT + goal
        opening = PLAN_PROMPT + goal
    else:
        original = goal
        opening = opening_turn(goal, PROTOCOL)
    pw = _unlock_password()
    if not pw:
        return opening, False
    return PROTOCOL + (UNLOCK_PREFIX % pw) + original, True


def _redact_unlock_password(text: str) -> str:
    """Keep local secrets out of the fleet transcript files.

    Applied only where text is WRITTEN. The turn actually sent to the agent keeps the
    real password -- redacting that would stop the unlock from working at all. Nothing
    reads a transcript back and resends it, so redacting the stored copy costs nothing.

    Not just the unlock password: an agent that read .env once echoed it back, and the
    API key and HF token landed in a transcript in clear text. Selection is by NAME in
    one shared place, so a newly added key is not missed the same way.
    """
    try:
        from tools.secret_store import redact_secrets
        return redact_secrets(text)
    except Exception:
        return text or ""


def _mcp_tunnel_url():
    """MCP_TUNNEL_URL, read LOCALLY (process env or .env) the same way _unlock_password reads
    MCP_UNLOCK_PASSWORD. '' if unset (no tunnel configured / everything is local)."""
    url = (os.environ.get("MCP_TUNNEL_URL") or "").strip()
    if not url:
        try:
            from dotenv import load_dotenv
            repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            load_dotenv(os.path.join(repo, ".env"))
            url = (os.environ.get("MCP_TUNNEL_URL") or "").strip()
        except Exception:
            pass
    return url


def _url_ok(url, timeout_s=3.0):
    """GET `url`, True iff the response status is 200. Bypasses any corporate/system HTTP(S)
    proxy for this one request -- a proxy can swallow or mis-route 127.0.0.1/loopback traffic
    (and may also not have a route to a devtunnel host), so we build a ProxyHandler(proxies={})
    opener rather than relying on urllib's environment-derived default. Never raises -> False on
    ANY error (timeout, connection refused, DNS, TLS, etc.)."""
    try:
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout_s) as resp:
            return int(getattr(resp, "status", getattr(resp, "code", 0)) or 0) == 200
    except Exception:
        return False


def _infra_healthy(timeout_s=3.0) -> bool:
    """True iff OUR path to the MCP tools looks healthy right now: the LOCAL server answers
    http://127.0.0.1:8000/health AND (no tunnel is configured OR the tunnel's /health also
    answers). Used to gate the AGENT_DEAD branch's "agent is stopped/disabled" conclusion: an
    ADMIN_BLOCK-worded reply is only trustworthy as a real per-agent block when we can prove the
    problem is NOT on our own network/tunnel side.

    Deliberately a plain module-level function (not a method) so tests can monkeypatch
    `relay.relay_fleet._infra_healthy` directly, and deliberately conservative: any failure to
    confirm health (local server down, tunnel down, DNS hiccup, whatever) returns False, i.e.
    "infra looks NOT healthy" -- which, in the caller, SUPPRESSES the disabled-agent claim rather
    than asserting it. That is the safe direction for a false-positive-sensitive detector: we'd
    rather under-claim "agent disabled" (fall back to a re-queueable infra/network stuck) than
    wrongly tell the user to go disable-hunt in Copilot Studio for what was actually our own
    network blip. Never raises."""
    try:
        if not _url_ok("http://127.0.0.1:8000/health", timeout_s=timeout_s):
            return False
        tunnel = _mcp_tunnel_url()
        if tunnel:
            base = tunnel.rstrip("/")
            if not _url_ok(base + "/health", timeout_s=timeout_s):
                return False
        return True
    except Exception:
        return False


# STUCK-ON-REDIRECT detector (2026-06-18, W4 xarray-3364). A worker tab can land on the M365
# SSO-redirect / landing page (e.g. https://m365.cloud.microsoft/chat/?redirfrom=CsrToSSR&auth=2)
# instead of its agent conversation. That page has NO composer, so EVERY send fails with an empty
# composer (text_len:1, is_processing:False, phase:waiting_processing). The existing login-wall
# detector (edge_recover.looks_like_login) does NOT catch this -- it is a same-origin redirect, not
# a login.microsoftonline sign-in form -- so the send-retry loop kept hammering the wrong page for
# ~1h (~29/30 consecutive failures, 15:05-16:04) until the turn timed out -> STUCK. The fix: when a
# worker's send keeps failing AND its tab is on such a redirect/landing page (not the agent surface),
# RE-NAVIGATE the tab to the agent URL it was launched to drive (mirrors _open_fresh's about:blank
# re-nav) instead of retrying send forever. Bounded per turn so a persistently-wrong page still
# falls through to the existing terminal handling.
REDIRECT_URL_MARKERS = ("redirfrom=", "csrtossr", "auth=")

# A real conversation URL carries a UUID after /conversation/ OR /chat/ (the new agent uses the
# /chat/<guid> form). Used to capture conv_url regardless of which path the agent uses, without
# mistaking the agent BASE url (/chat/agent/T_xxx) for a conversation.
_CONV_GUID_RE = re.compile(
    r"/(?:conversation|chat)/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


def looks_like_redirect_landing(url):
    """True if `url` looks like an M365 SSO-redirect / landing page rather than an agent
    conversation. Heuristic, deliberately conservative: it keys off the redirect query markers
    that appear on the CsrToSSR landing URL (W4) and the absence of a real conversation path.
    An agent conversation URL carries a `/conversation/<guid>` (or `/chat/<guid>`) segment; a
    bare landing/redirect page does not. Never raises -> False on odd input (safe: 'not a
    redirect', so the new re-nav branch simply does not fire and behaviour is unchanged)."""
    try:
        u = (url or "").lower()
    except Exception:
        return False
    if not u:
        return False
    # explicit redirect/SSO query markers (the W4 CsrToSSR landing URL)
    if any(m in u for m in REDIRECT_URL_MARKERS):
        return True
    return False


def on_agent_surface(url):
    """True if `url` looks like a real agent conversation surface (a non-empty
    /conversation/<id> or /chat/<id> path segment), i.e. NOT a bare redirect/landing page
    like '.../chat/?redirfrom=...'. Used as a CHEAP sanity check; the authoritative
    confirmation after a re-navigation is composer-present (a DOM probe), since the URL
    alone can lag. Strips the query string first so '/chat/?redirfrom=...' (empty id) does
    NOT count as a surface. Never raises -> False on odd input (conservative)."""
    try:
        path = (url or "").lower().split("?", 1)[0].split("#", 1)[0]
    except Exception:
        return False
    for marker in ("/conversation/", "/chat/"):
        if marker in path:
            tail = path.split(marker, 1)[1].strip("/")
            if tail:                       # there is an actual id after /conversation//chat/
                return True
    return False


def _reap_orphan_redirect_tabs(context, workers):
    """Close stray SSO-redirect / landing tabs that are NOT owned by any worker (a failed goto or
    an auth bounce leaves one behind). Never touches a worker's live page or a real conversation
    surface. Mirrors the bridge-side reaper -- keeps the fleet Edge from piling up dead tabs within
    a long chunk (the per-chunk hard-reset only clears them between chunks). Never raises."""
    try:
        owned = set(id(w.page) for w in workers if getattr(w, "page", None) is not None)
        for pg in list(getattr(context, "pages", []) or []):
            if id(pg) in owned:
                continue
            try:
                u = pg.url or ""
            except Exception:
                continue
            if looks_like_redirect_landing(u) and not on_agent_surface(u):
                try:
                    pg.close()
                except Exception:
                    pass
    except Exception:
        pass


# Statuses where the main thread is legitimately busy with a BOUNDED acceptance check
# (eval/verification), NOT a wedged Edge. The watchdog must not hard-reset while a worker
# is in one of these -- doing so throws away in-progress eval and resumes every goal at
# attempt 1 (the sphinx-8595 t7->t1 regression). See fleet_runner._watchdog.
VERIFY_STATUSES = ("verifying",)

# Upper bound (seconds) the watchdog will tolerate a frozen status.json while a worker
# claims to be in a blocking acceptance eval. The SWE-bench docker eval is capped at
# ~1300s (swe_check timeout) inside swe_check.py; we add generous margin so a legitimately
# slow eval is never killed, but a TRULY wedged Edge that merely happens to be mid-verify
# is still eventually recovered. Beyond this, a non-advancing status is treated as wedged.
EVAL_STALL_CEILING_S = 1500


class FleetContextLost(Exception):
    """Raised when the underlying Edge/CDP context died mid-run (wedged or hard-reset).
    Carries the goals that had not finished, so the runner can reconnect and resume."""
    def __init__(self, unfinished):
        super().__init__("fleet CDP context lost")
        self.unfinished = unfinished


class _Transcript:
    """Append-only full-text log of one worker's conversation, one JSON object per line.

    Each line is {"turn": n, "role": "user"|"assistant", "text": <full, untruncated>,
    "ts": epoch}. A first "meta" line records the worker name / goal / conv guid so a
    reader can match the file to a card even before any turn lands.

    The unique key is the KEY of the file, not the worker name: worker names (w0/w1) are
    reused across rounds/runs, so keying on the name alone would interleave two unrelated
    conversations into one file. The key is `<run_id>_<name>` where run_id is unique per
    run_relay_fleet() invocation (its start time, base36) -- so two runs that both have a
    'w0' write to different files. When the conversation's guid (conv_url tail) becomes
    known it is recorded in-line; we do NOT rename the open file (that races with appends).

    Completely exception-safe: any I/O failure is swallowed so the fleet never stalls on a
    logging hiccup. Each append is flushed so a crash leaves whole lines, not partial ones."""

    def __init__(self, directory, key, name, goal):
        self.dir = directory
        self.key = key
        self.path = os.path.join(directory, key + ".jsonl") if directory else None
        self._guid_logged = False
        if not self.path:
            return
        try:
            os.makedirs(directory, exist_ok=True)
            # fresh file for this run+worker; truncate any stale leftover with the same key
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"meta": True, "key": key, "name": name,
                                    "goal": goal, "ts": time.time()},
                                   ensure_ascii=False) + "\n")
                f.flush()
        except Exception:
            self.path = None

    def _append(self, obj):
        if not self.path:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                f.flush()
        except Exception:
            pass

    def user(self, turn, text):
        self._append({"turn": turn, "role": "user", "text": _redact_unlock_password(text), "ts": time.time()})

    def assistant(self, turn, text):
        # 返ってきた側にも掛ける。こちらが送った文だけ伏せても、相手が復唱すれば
        # 同じ記録に平文で残る。プロンプトでは「二度と出力するな」と頼んでいるが、
        # 頼みごとであって保証ではない。
        self._append({"turn": turn, "role": "assistant",
                      "text": _redact_unlock_password(text), "ts": time.time()})

    def metric(self, turn, name, value, **extra):
        """One measured number for this turn, beside the text it belongs to.

        Kept in the same transcript rather than a separate file so a number can always be
        read against the turn that produced it -- a memory reading with no idea what was
        being sent at the time cannot tell a fat turn from a leak.
        """
        row = {"turn": turn, "role": "metric", "name": name, "value": value,
               "ts": time.time()}
        row.update(extra)
        self._append(row)

    def note_guid(self, guid):
        """Record the conversation guid once it's known (idempotent)."""
        if self._guid_logged or not guid:
            return
        self._guid_logged = True
        self._append({"guid": guid, "ts": time.time()})


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def avail_phys_mb() -> float:
    """Available physical memory in MB (Windows). Best-effort; ~4 GB on failure."""
    try:
        m = _MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullAvailPhys / (1024.0 * 1024.0)
    except Exception:
        return 4096.0


#: HOW MUCH PHYSICAL RAM MUST STAY FREE FOR THE OPERATOR, IN MB. ONE NUMBER, READ BY EVERY GATE.
#:
#: THE THREE GATES BELOW USED TO CARRY THREE DIFFERENT LITERALS -- 2048 when sizing concurrency,
#: 1400 when autoscaling, 2000 before opening a side page -- and none of them read the floor the
#: operator had configured, which is 512 (recorded as an operator instruction and encoded in both
#: evaluators' MIN_FREE_MB). The effect was silent: on a box with 2454 MB free, the 2048 literal
#: made `auto_concurrency` return 1 where the configured floor gives 2, so a fleet left on its
#: automatic setting ran everything one goal at a time and nothing said why.
#:
#: RAISING THIS IS THE SAFE DIRECTION AND IT IS ONE EDIT. The literals were high because RAM
#: exhaustion once wedged the Edge badly enough that the watchdog hard-reset it; 512 is more
#: permissive than what the fleet has been doing, so a box that starts thrashing should have this
#: raised rather than the gates re-forked.
FLEET_RAM_FLOOR_MB = float(os.environ.get("MCP_FLEET_RAM_FLOOR_MB", "512"))

#: What ONE Copilot tab is budgeted to cost. Separate from the floor because they answer
#: different questions: the floor is what must remain, this is what the next tab will take.
FLEET_PER_TAB_MB = float(os.environ.get("MCP_FLEET_PER_TAB_MB", "700"))


def ram_room_for_tab(floor_mb=None) -> bool:
    """True iff there is enough free physical RAM to open ANOTHER browser tab without crowding
    the machine. Used to RAM-gate the SUB-AGENT side-pages (research / refuter) -- the fleet's
    worker-tab autoscale doesn't count those, so on a low-RAM box even a single task's ultra
    pipeline (main + research + refuter tabs) could overload the Edge until the sweep wedged and
    the watchdog hard-reset it. Each side-page opens lazily once this returns True, so the live
    tab count tracks free RAM at ALL granularities, not just at worker admission.

    The default is DERIVED, not a fourth literal: room for the floor that must remain AND for
    the tab about to be opened. The 2000 that used to sit here was neither of those.
    """
    if floor_mb is None:
        floor_mb = FLEET_RAM_FLOOR_MB + FLEET_PER_TAB_MB
    return avail_phys_mb() >= floor_mb


#: Recycle a worker's conversation once its renderer heap passes this, in MB. Measured on this
#: machine 2026-08-20: a FRESH Copilot tab already holds 137-161 MB of JS heap with 926 DOM
#: nodes and 2 KB of visible text, so roughly 150 MB is the web app itself and nothing to do
#: with the conversation. Everything above that is what the turns put there.
#:
#: PROVISIONAL. The right threshold comes from MB-per-turn on real work, which is why every
#: turn now logs the heap. A worker's turns carry OCR text and spreadsheet rows, so they are
#: far fatter than the bridge probe's fixed round trips, and copying the bridge's 120-turn
#: number would have been a guess wearing a measurement's clothes.
FLEET_HEAP_RECYCLE_MB = float(os.environ.get("MCP_FLEET_HEAP_RECYCLE_MB", "500"))

#: Never recycle for memory before this many turns, whatever the heap says. A conversation that
#: is replaced every few turns re-anchors constantly, and re-anchoring costs a turn -- the cure
#: would cost more turns than the disease.
FLEET_HEAP_MIN_TURNS = int(os.environ.get("MCP_FLEET_HEAP_MIN_TURNS", "12"))


def _holds_slot(w):
    """Whether this worker is admitted and consuming the fleet's budget right now.

    THE DISK UNIT, NOT THE TAB UNIT. A socket worker holds no tab and still checks out the
    repository and runs the eval, so it counts here while contributing nothing to tab_load /
    tab_weight. Conflating the two lets sockets over-admit against a disk floor that has
    nothing to do with browsers -- which is silent until the disk is full.
    """
    return ((getattr(w, "page", None) is not None or getattr(w, "socket", False))
            and w.status not in TERMINAL)


_SOCKET_ROUTE = None
_SOCKET_ROUTE_LOCK = threading.Lock()


def _socket_route():
    """The fleet's socket route, built once. Off unless MCP_FLEET_SOCKET says otherwise.

    A worker asks this for a driver; if it gets None it opens a tab, which is what every
    worker did before this existed. Nothing here can fail a goal -- see relay/socket_route.py.
    """
    global _SOCKET_ROUTE
    if _SOCKET_ROUTE is None:
        with _SOCKET_ROUTE_LOCK:
            if _SOCKET_ROUTE is None:
                from relay.socket_route import (SocketRoute, capture_via_tab,
                                                websocket_connect)
                _SOCKET_ROUTE = SocketRoute(capture_fn=capture_via_tab,
                                            connect_fn=websocket_connect,
                                            log=lambda m: print(m, flush=True))
    return _SOCKET_ROUTE


def auto_concurrency(n_goals, per_tab_mb=None, headroom_mb=None, hard_cap=100):
    """How many heavy M365 tabs we can afford open at once, given free RAM right now.
    Keep `headroom_mb` for the user's other work; budget `per_tab_mb` per Copilot tab.
    `hard_cap` is a high upper RAIL (100), NOT a hardware bound: the RAM term (free//per_tab)
    is the real limiter, so a 16 GB box self-limits to ~3 while a big-RAM machine scales up.
    A modest run is still wise for M365 Copilot per-user fair-use, but that's a policy choice
    the operator makes via settings, not a hardcoded ceiling baked in here."""
    per_tab_mb = FLEET_PER_TAB_MB if per_tab_mb is None else per_tab_mb
    headroom_mb = FLEET_RAM_FLOOR_MB if headroom_mb is None else headroom_mb
    fit = int((avail_phys_mb() - headroom_mb) / per_tab_mb)
    return max(1, min(n_goals, fit, hard_cap))


# ── Disk-floor admission (capacity-aware continuous admission, 2026-06-14) ────────────────
# The fleet now admits jobs as fast as BOTH RAM and DISK allow, draining and re-admitting
# continuously (no batch barrier). The disk constraint matters because each SWE-bench eval
# pulls/builds Docker images and, on a timeout, can leave a detached container inflating the
# C: vhdx. So before opening a new tab we make sure C: free will stay above a reserved floor
# even after the new job's eval consumes its disk budget. The floor is USER-CONFIGURABLE
# (env SWE_DISK_FLOOR_GB, default 6; the cockpit can write it later) because "always keep N GB
# free" is a safety/usability win for normal use too, not just the bench.
DEFAULT_DISK_FLOOR_GB = float(os.environ.get("SWE_DISK_FLOOR_GB", "6"))
# Disk a single not-yet-started eval is assumed it MIGHT consume before its own per-instance
# cleanup reclaims it (image layers etc.). Used to look ahead so we never open a tab that would
# itself push C: under the floor. Env-tunable; conservative default. 0 disables look-ahead.
DEFAULT_EVAL_DISK_GB = float(os.environ.get("SWE_EVAL_DISK_GB", "0"))


def free_disk_gb(path=None):
    """Free space (GB) on the drive holding `path` (default: this repo's drive, i.e. C:).
    Best-effort; returns a large number on failure so a read error never WRONGLY blocks
    admission (RAM gate + per-instance disk guard in swe_check still protect the floor)."""
    try:
        import shutil
        if not path:
            path = os.path.splitdrive(os.path.abspath(__file__))[0] + os.sep
        return shutil.disk_usage(path).free / (1024.0 ** 3)
    except Exception:
        return 1e6


# Per-repo cold-build disk estimate (GB) -- the test's PER-UNIT WEIGHT in the disk gate. A 7GB
# matplotlib/sklearn build must reserve far more than a 2GB requests one, so the gate can safely
# PAIR light evals while keeping heavy ones solo, instead of one flat number that's wrong for both.
# Heavy values are tuned to ~(typical C: free - floor) so exactly ONE admits and a 2nd is blocked.
# Calibrated to MEASURED build footprints (2026-06-14), not guesses: matplotlib's cold build dips
# C: ~10GB (12->1.6GB observed); scikit-learn ~4GB (C: 13.7->7.3 with sklearn+requests, minus the
# 2.34GB requests image); requests image is 2.34GB. The earlier flat 7GB for sklearn was too high
# and wrongly blocked TWO sklearn from pairing -- at ~4-5GB each, two fit (2*5=10 <= C:free - min).
_REPO_EVAL_GB = {
    "matplotlib__matplotlib": 9.0, "astropy__astropy": 9.0,   # ~10GB build -> stays solo
    "scikit-learn__scikit-learn": 5.0,                          # ~4GB measured -> two can pair
    "django__django": 3.0, "sympy__sympy": 3.0, "sphinx-doc__sphinx": 3.0,
    "pydata__xarray": 3.0, "pytest-dev__pytest": 2.5, "pylint-dev__pylint": 2.5,
    "psf__requests": 2.5, "pallets__flask": 2.5,
}
DEFAULT_REPO_EVAL_GB = 5.0
EVAL_DISK_PERREPO = os.environ.get("SWE_EVAL_DISK_PERREPO") == "1"
# Crash-avoidance hard minimum for the per-repo gate (GB). A single heavy build (matplotlib ~7GB)
# legitimately dips C: BELOW the 6GB soft floor and recovers -- conc1 relies on that. So the
# per-repo gate reserves the SUM of all concurrent builds (in-flight + new) against this lower
# hard minimum, not the soft floor: heavy admits solo (12-7=5 >= 3), heavy+heavy or heavy+medium
# is blocked (would dip under 3GB, the level near which concurrent builds corrupted WSL), light
# evals still pair. Env-tunable.
PERREPO_HARD_MIN_GB = float(os.environ.get("SWE_EVAL_HARD_MIN_GB", "3"))


def repo_eval_gb(inst):
    """Per-instance cold-build disk estimate by repo (see _REPO_EVAL_GB). inst is '<owner>__<name>
    -<n>' or a worktree path embedding it."""
    inst = (inst or "").split("wt_")[-1]
    repo = inst.rsplit("-", 1)[0]
    return _REPO_EVAL_GB.get(repo, DEFAULT_REPO_EVAL_GB)



#: When the disk-defer notice was last printed, so a long drain says so once a minute rather
#: than once a sweep.
_DISK_DEFER_LAST = [0.0]
DISK_DEFER_NOTICE_S = 60.0


def _note_disk_defer(floor_gb, waiting):
    """Print why nothing is being admitted. Never raises; never becomes the log itself."""
    try:
        now = time.time()
        if now - _DISK_DEFER_LAST[0] < DISK_DEFER_NOTICE_S:
            return
        _DISK_DEFER_LAST[0] = now
        free = free_disk_gb()
        print("[fleet] admitting nothing: %.2f GB free on C:, floor %.1f GB -- %d goal(s) "
              "waiting. Free disk; lowering the floor turns this refusal into a crash."
              % (free, float(floor_gb or DEFAULT_DISK_FLOOR_GB), waiting), flush=True)
    except Exception:
        pass


def disk_admission_ok(floor_gb=None, eval_gb=None, free_gb=None, building=0, reserve_gb=None):
    """Pure predicate: may we open ANOTHER eval-bearing tab without risking the disk floor?

    OK iff (current C: free) - (disk this new eval AND every already-admitted eval still in
    flight might use) >= floor. The `building` term is the count of eval-bearing tabs already
    open whose Docker builds have NOT yet been reclaimed: each will consume up to eval_gb, so we
    must reserve eval_gb*(building+1), not just eval_gb for the one we're about to open. Without
    this, admission-time free-space looks fine for tab #1, #2, #3... (no build has consumed disk
    YET), all get admitted, then their cold builds run CONCURRENTLY and blow past the floor --
    which is exactly how 5 concurrent heavy builds crashed C: and corrupted WSL (2026-06-14).

    Splitting the free-reading out (`free_gb`) keeps this unit-testable with a mocked disk. A
    non-positive floor disables the gate (always OK) -- normal (non-bench) use may not want a
    reserve. eval_gb=0 keeps legacy floor-only behavior (no per-build look-ahead)."""
    floor = DEFAULT_DISK_FLOOR_GB if floor_gb is None else float(floor_gb)
    if floor <= 0:
        return True
    free = free_disk_gb() if free_gb is None else float(free_gb)
    if reserve_gb is not None:
        # caller computed an exact reserve (e.g. per-repo sum of in-flight + new build sizes)
        return (free - float(reserve_gb)) >= floor
    eval_gb = DEFAULT_EVAL_DISK_GB if eval_gb is None else float(eval_gb)
    reserve = eval_gb * (1 + max(0, int(building)))
    return (free - reserve) >= floor


def ram_target_cap(open_now, current_cap, ceiling,
                   per_tab_mb=None, headroom_mb=None, floor=1, up_margin_mb=0):
    """RAM-aware live concurrency target (autoscale). Recomputed each loop: given how many
    tabs are open right now (their RAM is already reflected in the free-RAM reading) and how
    much headroom we want to keep for the user, how many tabs can we SUSTAIN?

    Asymmetric on purpose, to never re-trigger the RAM-exhaustion crash:
      * scale UP by at most ONE tab per call (gentle ramp -- re-evaluated every loop), and
      * allow scale DOWN to the raw target immediately. A lower cap is SOFT: running tabs are
        not killed, we just stop opening new ones until some finish (natural drain).

    ANTI-THRASH HYSTERESIS (`up_margin_mb`): with up_margin_mb=0 (default, back-compat) the up-
    and down-thresholds coincide, so a fleet can oscillate 1<->3 every loop: open a tab -> RAM
    tightens just under the line -> drain target -> tab closes -> RAM frees just over the line
    -> ramp up -> repeat. A positive `up_margin_mb` makes the UP step require that much EXTRA
    free RAM beyond what merely holding the new tab needs, so once the fleet settles at a water
    level a small RAM jitter no longer pushes it back up -- it HOLDS. The DOWN side is unchanged
    (drains immediately on a real deficit), so the dead-band only damps needless growth.
    Clamped to [floor, ceiling] (ceiling = the user's configured maximum)."""
    per_tab_mb = FLEET_PER_TAB_MB if per_tab_mb is None else per_tab_mb
    headroom_mb = FLEET_RAM_FLOOR_MB if headroom_mb is None else headroom_mb
    avail = avail_phys_mb()
    # FLOOR division (not int(): truncates toward zero) so a RAM *deficit* yields a negative
    # term and the target actually drops below open_now -> drains. e.g. (-400)//700 == -1.
    raw = open_now + int((avail - headroom_mb) // per_tab_mb)
    target = max(floor, min(raw, ceiling))
    if target > current_cap:
        # hysteresis: only ramp UP if there is up_margin_mb of headroom ON TOP of the per-tab
        # budget the new tab needs (a dead-band so jitter around the line doesn't re-grow us).
        if (avail - headroom_mb - up_margin_mb) >= per_tab_mb:
            target = min(current_cap + 1, ceiling)   # ramp up one tab at a time
        else:
            target = current_cap                     # in the dead-band -> HOLD, don't grow
    return target


def _open_fresh(context, url):
    """Open a NEW tab on a fresh chat of the agent. Tolerant of slow navigation
    (a busy Edge can miss the 30s domcontentloaded) -- we proceed and wait for the
    composer to render either way. If a sign-in page appears, the background Edge is
    surfaced once so the user can authenticate."""
    from .edge_recover import surface, looks_like_login, touch_pause, rehide
    pg = context.new_page()
    surfaced = False
    force_timer = None
    # Up to 3 navigation attempts: a failed goto leaves the tab on about:blank, and
    # waiting 45s for a composer that will never come just leaves about:blank on screen.
    # Detect about:blank early (~4s) and RE-navigate instead of staring at it.
    #
    # When a sign-in page appears we surface() the hidden Edge ONCE. From then on the
    # user may be mid-MFA, so: (a) touch_pause() every ~1s while the login page is up so
    # the keeper's 180s backoff never expires and re-minimizes the window under them, and
    # (b) allow a much longer total wait (up to ~300s) for the composer -- the default
    # 3x25s aborts a slow login. As soon as the composer renders after we surfaced,
    # rehide() drops the window back to the background immediately.
    attempt = 0
    while True:
        attempt += 1
        navigation_failed = False
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            navigation_failed = True
        for k in range(25):
            pg.wait_for_timeout(1000)
            if pg.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                if surfaced:
                    _cancel_force_rehide(force_timer)  # real rehide now; drop the safety net
                    rehide()               # auth done -> back to background at once
                return pg
            try:
                u = pg.url or ""
                if looks_like_login(u):
                    if not surfaced:
                        # thread the target agent URL through so a headed relaunch (if the
                        # companion Edge is headless) lands on this conversation, not the
                        # launcher's default generic top page.
                        surface(open_url=url); surfaced = True
                        # BUG 4b safety net, defense-in-depth: if we give up below (or exit
                        # some other way) without ever calling rehide(), this bounded timer
                        # still forces the window back down on its own.
                        force_timer = _schedule_force_rehide()
                    touch_pause()          # keep the keeper backed off through a long login
                elif u == "about:blank" and k >= 3:
                    break                  # stuck on about:blank -> re-navigate
                elif navigation_failed and k >= 3:
                    # A timed-out navigation can report the requested URL while the
                    # target still has an about:blank document. Do not spend another
                    # 25s waiting for a composer that cannot exist; retry the navigation.
                    break
            except Exception:
                pass
        # Non-surfaced path unchanged: give up after 3 navigation attempts (~75s).
        # Surfaced path: keep polling for the composer up to ~300s total before giving
        # up, so a slow interactive/MFA sign-in is not aborted out from under the user.
        if surfaced:
            if attempt * 25 >= 300:
                break
        elif attempt >= 3:
            break
    if surfaced:
        # Giving up without ever seeing the composer -- rehide right now instead of relying
        # solely on the bounded timer (BUG 4a: a failed/abandoned sign-in must not leave the
        # window stuck in the foreground either).
        _cancel_force_rehide(force_timer)
        try:
            rehide()
        except Exception:
            pass
    return pg


def goal_fields(goal):
    """Normalize a goal into (text, checks, cwd). A goal is either a plain string
    (no acceptance check -- legacy/back-compat) or a dict
    {"text"|"goal": str, "check"|"checks": dict|list, "cwd": str}. This is how a
    goal carries machine-checkable acceptance criteria into the verification gate."""
    if isinstance(goal, dict):
        text = goal.get("text") or goal.get("goal") or ""
        checks = normalize_checks(goal.get("checks") or goal.get("check"))
        cwd = goal.get("cwd") or None
        return text, checks, cwd
    return str(goal), [], None


# Escalating/varying CONTINUE nudge (fixes the "identical nudge re-injected every turn"
# degradation: a worker that never emits DONE used to get the byte-for-byte-identical
# CONTINUE_JOB constant every single turn, up to max_turns -- observed to degrade the
# M365 Copilot model into refusing to answer after ~5 repeats in one live conversation).
# Pure and hermetically testable: given only the consecutive-continue COUNT, return the
# nudge TEXT (pre-anchor; the caller still wraps it with _task_anchor). Kept as a plain
# function (not a method) so tests can assert on it without constructing a RelayWorker.
_CONTINUE_ESCALATION_PHRASES = (
    "ここまでの作業を踏まえ、残りは簡潔に進めてください。",
    "同じ作業の繰り返しは不要です。残タスクを絞って手短に進めてください。",
    "ここまでの内容を土台に、まだ終わっていない部分だけ手早く仕上げてください。",
)


def _continue_nudge(count):
    """Return the CONTINUE-branch nudge text for the count-th consecutive continue (1-based).
    counts 1-2: the original gentle CONTINUE_JOB, unchanged (back-compat for the common
    quick case where the agent finishes in a turn or two).
    counts 3+: a STRONGER, wrap-up-leaning nudge that also embeds the count, so no two
    turns -- however close together -- ever see byte-identical text."""
    if count <= 2:
        return CONTINUE_JOB
    phrase = _CONTINUE_ESCALATION_PHRASES[(count - 3) % len(_CONTINUE_ESCALATION_PHRASES)]
    return ("%s（継続%d回目）完了したら最後の行に必ず DONE、まだなら FAIL と理由を"
            "書いてください（同じ作業の繰り返しは不要です）。" % (phrase, count))


_PHASE_LABELS = {
    "pending":     "Queued",
    "ready":       "Starting",
    "waiting":     "Running",
    "researching": "Researching",
    "refuting":    "Reviewing",
    "verifying":   "Verifying",
    "awaiting":    "Needs input",
    "done":        "Done",
    "stuck":       "Needs attention",
    "maxturns":    "Needs attention",
    "error":       "Stopped (error)",
    "cancelled":   "Stopped",
    "fresh_replay": "Fresh replay",
    "content_refused": "Content refused",
    "unresolved_refusal": "Unresolved refusal",
}


class RelayWorker:
    """One conversation running one goal to completion, as a non-blocking machine.
    Starts WITHOUT a tab (status 'pending'); attach() opens one, close() frees it.

    Acceptance gate (spec 3-3): if the goal carries `checks`, a Copilot "DONE" does
    NOT end the worker -- it moves to the 'verifying' state, where the frame runs the
    checks LOCALLY (acceptance.Check). Pass -> real DONE; fail -> the actual failure is
    re-injected and the agent keeps working, up to max_verify_attempts (then STUCK with
    outcome VERIFY_FAILED). No checks -> DONE is accepted as before (back-compat).

    Phase spine (Bucket B): every status TRANSITION is recorded in `phase_events` as a
    structured dict {"ts": <epoch float>, "event": "<status-key>", "label": "<English>"},
    appended ONLY on change so the list never duplicates. The UI can render a real (non-
    fabricated) phase timeline from these events. Use the `status` property to set the
    status field -- it intercepts assignments and auto-records the event."""

    # ── Phase-spine property (Bucket B) ──────────────────────────────────────────
    # `status` is exposed as a property so every assignment (self.status = "ready")
    # is intercepted and a phase event is appended ONLY on an actual state CHANGE.
    # The backing field is `_status`; existing code needs no changes.
    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        prev = getattr(self, "_status", None)
        self._status = value
        if value != prev:
            # phase_events may not exist yet during the very first assignment in __init__
            # (before self.phase_events is set); guard with getattr so it's safe.
            evts = getattr(self, "phase_events", None)
            if evts is not None:
                evts.append({
                    "ts": time.time(),
                    "event": value,
                    "label": _PHASE_LABELS.get(value, value),
                })

    def __init__(self, goal, name, max_turns=1000, dwell_s=4.0,
                 per_turn_timeout_s=240, max_no_progress=3, max_verify_attempts=3,
                 refuter=False, max_refute=2, plan_mode=False, review_lenses=None,
                 max_transient=10, transcript_dir=None, run_id="", busy_writer=None,
                 max_research=3, contract_budget=None, max_continue=6,
                 resilience_profile="off", max_fresh_replays=0):
        self.page = None
        self.drv = None
        #: True while this worker is talking over a socket instead of holding a tab. It still
        #: occupies a DISK slot (it runs the same eval) but no RAM slot, which is the whole
        #: difference and the reason the two accountings are separated below.
        self.socket = False
        #: How many socket turns of this worker the route has already been told about. Without
        #: it nothing reports SUCCESS, the breaker's consecutive counter never resets, and a
        #: long healthy run closes the route on three failures scattered across hours.
        self._socket_turns_seen = 0
        #: Whether this worker STARTED on a socket and had to open a tab. Distinct from
        #: `socket`, which is False afterwards and so cannot answer "which route did this
        #: goal actually need" -- the one question the classifier will be built to predict.
        self._socket_fell_back = False
        self.goal_record = freeze_goal_dict(goal) if isinstance(goal, dict) else {"text": str(goal)}
        text, checks, cwd = goal_fields(goal)
        self.goal = text
        self.checks = checks
        self.cwd = cwd
        self.task_envelope = task_envelope_from_goal(goal)
        self.resilience_profile = str(resilience_profile or "off").lower()
        # The resilience contract permits at most one identical replay per leaf task.
        self.max_fresh_replays = min(1, max(0, int(max_fresh_replays or 0)))
        self.fresh_replay_count = 0
        self.refusal_count = 0
        self.refusal_history = []
        self.original_goal_hash = self.task_envelope.goal_hash
        self.recovery_state = ""
        self.recovery_cause = ""
        self.recovery_result = ""
        self.attempt_transcripts = []
        # resume_conv: when a goal dict carries it, this worker RESUMES that existing Copilot
        # conversation URL instead of opening a fresh chat -- so a FINISHED fleet task can take a
        # CONTEXT-CARRYING follow-up (the prior turns stay in the conversation the agent reads).
        self.resume_conv = (goal.get("resume_conv") if isinstance(goal, dict) else None) or None
        self.max_verify_attempts = max_verify_attempts
        self.verify_attempts = 0
        # transient-failure retries (network/tool/send hiccups) -- the relay analog of
        # Claude Code retrying a failed request rather than giving up. Budget + backoff.
        self.max_transient = max_transient
        self.transient = 0
        self.first_transient_ts = 0.0   # wall-clock start of the current transient/outage streak
        # generation-wait reschedules: the PREVIOUS turn was still generating when we tried
        # to send (a slow django/sympy turn). This is NOT a failure -- send() waited and
        # then deferred -- so it does NOT consume the transient budget and does NOT count a
        # turn. A separate, very generous cap only catches a turn that LITERALLY never stops
        # generating (a wedged page). In the fleet path each send() only waits ~2s before
        # deferring (non-blocking). This was the W0 django__django-14730 STUCK:
        # send-into-generating burned the 10x transient budget into STUCK even though the turn
        # was merely slow.
        #
        # Patience is now WALL-CLOCK, not a fixed deferral COUNT: under load each defer cycle
        # can take well over the nominal ~4s (the round-robin sweep slows when many workers
        # are busy), so a count of 60 drifts to far LESS than the intended ~240s of realized
        # patience -- false-STUCKing a legitimately slow turn. We instead stamp the FIRST defer
        # (first_defer_ts) and cut off only when `now - first_defer_ts` exceeds
        # max_gen_wait_s, so the realized patience is the same wall-clock budget regardless of
        # load/sweep speed. (max_gen_waits is kept as a generous secondary guard against a
        # pathological tight-loop with no real elapsed time.) The single-relay path
        # (copilot_autopilot_relay.run_relay, GEN_WAIT_S=240) is synchronous and already
        # wall-clock equivalent, so it is unchanged.
        self.max_gen_wait_s = 360.0
        self.max_gen_waits = 90
        self.gen_waits = 0
        self.first_defer_ts = 0.0
        # progress-aware gate baseline: a legitimately long agent turn (folder/file/DB work)
        # keeps producing output, so we STUCK only on a turn FROZEN for the whole budget,
        # never on one that is merely slow-but-advancing. -1 = no baseline captured yet.
        self._defer_progress_sig = -1
        # goal-delivery recovery: when the agent reports it never received the task, RE-SEND
        # the goal verbatim instead of a generic retry nudge (bounded to avoid a resend loop).
        self.max_goal_resends = 3
        self._goal_resends = 0
        self._cooldown_until = 0.0
        self.verified = None          # None=not checked, True/False after a gate ran
        self.last_verify_detail = ""
        self._pending_checks = []     # acceptance.Check specs left to run this gate
        self._active_check = None     # the Check currently running (non-blocking)
        # When a BLOCKING acceptance eval (run_all_blocking) is about to run, this is set to
        # the time by which it must finish; surfaced into status.json so the watchdog can tell
        # "main thread legitimately busy with a bounded eval" from "Edge wedged". 0 = idle.
        # _busy_writer (if wired) flushes a status snapshot right BEFORE the blocking call so
        # the marker reaches disk even though on_tick can't fire during the blocked sweep.
        self.eval_busy_until = 0.0
        self._busy_writer = busy_writer
        # operator B refuter (spec 4B): an independent reviewer on a candidate DONE.
        self.refuter = refuter
        self.max_refute = max_refute
        self.refute_count = 0
        self._refuter_session = None
        # deep-research delegation (ported from the single-agent relay): a fleet worker can emit
        # `RESEARCH: <query>` and the relay spawns the Researcher sub-agent in a side page, feeds
        # its report back, and the worker continues -- the accuracy lever the single-agent relay
        # already has but the fleet previously lacked. ON by default, capped per worker.
        self.research_count = 0
        self.max_research = max_research
        self.research_model = "Claude"
        self._research_session = None   # non-blocking ResearchSession while status=='researching'
        self._copilot_err_streak = 0    # consecutive Copilot/tool 'SystemError' replies (path down)
        self._agent_err_ts = 0.0        # wall-clock start of the current agent-error (outage) streak
        self._toolerr_ts = 0.0          # wall-clock start of the tool-unreachable (devtunnel down) streak
        self._consent_streak = 0        # consecutive MCP connection-consent cards (auth needed)
        self._consent_auto_tried = False  # attempted the automatic click-through once
        self._consent_surfaced = False  # surfaced the Edge once (manual fallback)
        self._consent_surfaced_ok = False  # TRUTHFUL result of that surface() call (see edge_recover.surface)
        # CANNED-NONANSWER recovery (headless->default-Copilot fallback). Consecutive canned
        # non-answers, plus one-shot flags for the two escalations (surface-for-signin, and the
        # fleet-wide headed relaunch). See _decide's canned-non-answer handler.
        self._canned_streak = 0         # consecutive canned "couldn't respond" replies
        self._canned_ts = 0.0           # wall-clock start of the current canned-non-answer streak
        self._signin_surfaced = False   # surfaced the Edge once for interactive sign-in
        self._signin_surfaced_ok = False  # TRUTHFUL result of that surface() call (see edge_recover.surface)
        self._headed_recovery_done = False  # forced a HEADED companion relaunch once (last resort)
        self._unlock_attempts = 0       # auto-injected unlock(password) turns (write/exec gate)
        self._recycles = 0              # fresh-conversation recycles after a token-limit exhaustion
        try:
            self._max_recycles = int(os.environ.get("MCP_MAX_RECYCLES", "8"))
        except ValueError:
            self._max_recycles = 8
        # review panel (operator B, perspective-diverse): a list of lenses runs one
        # independent reviewer each, aggregated by majority. Empty = single reviewer.
        self.review_lenses = list(review_lenses) if review_lenses else []
        self._panel_queue = []
        self._panel_results = []
        # OPT-IN adaptive refuter (MCP_ADAPTIVE_REFUTER=1): set only when the adaptive hook
        # fires; None/unset means the fixed-panel path runs unchanged (back-compat).
        self._adaptive_features = None
        self._adaptive_mem = None
        self._context = None          # stored at attach() so we can open the side page
        self._agent_url = ""          # bare agent URL -> a fresh independent chat
        # STUCK-ON-REDIRECT recovery (W4 xarray-3364): count consecutive send failures, and once
        # they pile up WHILE the tab is on an SSO-redirect/landing page (not the agent surface),
        # RE-NAVIGATE to _agent_url instead of retrying send into a page that has no composer.
        # Reset whenever a send goes through. Bounded re-navs per turn so a persistently-wrong
        # page still falls through to the existing transient/terminal handling.
        self._send_fail_streak = 0    # consecutive send() exceptions since the last good send
        self.redirect_renav_threshold = 3   # send failures before we suspect a stuck redirect page
        self.max_redirect_renavs = 3        # cap re-navs PER TURN so we never loop forever
        self._redirect_renavs = 0           # re-navs spent on the CURRENT turn
        self.name = name
        self.conv_url = ""         # filled once the conversation gets its /conversation/<id>
        self.conv_title = ""       # Copilot's auto-generated chat title (best-effort scrape)
        self.steer_msgs = []       # user steering messages to inject on the next turn(s)
        self._last_was_steer = False   # so the FOLLOWING continue bridges off the steer
        self.max_turns = max_turns
        # autonomy-contract turn budget (None = no contract budget, inert). When set to an
        # int > 0, this was the effective cap applied from the active_contract.json at launch
        # (budget_turns). Stored so _begin_send can emit a budget-specific stop reason.
        self._contract_budget = contract_budget
        self.dwell_s = dwell_s
        self.per_turn_timeout_s = per_turn_timeout_s
        self.max_no_progress = max_no_progress
        # CONTINUE-nudge escalation/cap (fixes the identical-nudge-forever degradation --
        # see _continue_nudge above). Counts CONSECUTIVE turns that fell into the plain
        # CONTINUE branch (no DONE, no FAIL, no steer); reset whenever real progress/other
        # branch is taken. Independent of no_progress (which only fires on a VERBATIM-
        # identical agent reply -- a slowly-drifting non-converging reply never trips it).
        self.max_continue = max_continue
        self._continue_count = 0
        # plan-first: turn 1 proposes a plan and pauses for approval (a steer) before
        # executing. plan_steps is surfaced so the cockpit can show / let the user pick.
        self.plan_mode = plan_mode
        self.plan_steps = []
        self._plan_approved = False
        # Prime what we already know about this goal's THEME into the body that gets sent,
        # never into self.goal. self.goal is this worker's IDENTITY -- the transcript key,
        # the replay envelope, and what record_task derives the theme from. An earlier
        # version prepended the memory to the goal text itself and broke all three at once
        # (and hung a fleet sweep whose workers are keyed by goal text).
        initial_body, preflight_unlock = _initial_job_with_unlock(
            _with_theme_memory(self.goal), plan_mode)
        if preflight_unlock:
            # Count the proactive attempt against the same bounded budget used by reactive
            # re-unlocks when the M365 backend later rotates to a different source IP.
            self._unlock_attempts = 1
        self.job = (initial_body if self.resume_conv
                    else conversation_start_label(self.name) + initial_body)
        self.turn = 0
        self._turn_sent_at = 0.0
        self.no_progress = 0
        self.last_norm = None
        # phase_events MUST be initialized before `self.status = PENDING` so the setter
        # can append the initial "Queued" event immediately on construction.
        self.phase_events = []
        self.status = PENDING      # pending | ready | waiting | done | stuck | maxturns | error
        self.outcome = None
        self.reason = ""
        self.last_response = ""
        self.next_step = ""        # last NEXT: marker (informational, not gated)
        self.self_confidence = ""  # last CONFIDENCE: marker ("low"|"medium"|"high"|"")
        self.closed = False        # True once its tab has been released
        self._count_before = 0
        self._last_text = None
        self._stable_since = None
        self._t_send = 0.0
        # full-text transcript (each turn's send + Copilot reply, untruncated). The KEY
        # is run-unique (run_id includes the fleet start time) so reused worker names
        # (w0/w1) across rounds never share a file. Path is exposed via .transcript so
        # the snapshot can hand it to the UI. None when no dir was passed (back-compat).
        self._tx_base_key = ((run_id + "_") if run_id else "") + name
        self._tx_key = (self._tx_base_key + "_a0"
                        if self.resilience_profile != "off" else self._tx_base_key)
        self._tx = _Transcript(transcript_dir, self._tx_key, name, self.goal)
        self.transcript = self._tx.path or ""
        if self.transcript:
            self.attempt_transcripts.append(self.transcript)

    def _start_fresh_replay(self):
        """Move this worker to a brand-new conversation and resend the identical envelope."""
        replay_envelope = task_envelope_from_goal(self.goal_record)
        if not same_task_envelope(self.task_envelope, replay_envelope):
            self.status, self.outcome = "error", "ERROR"
            self.reason = "fresh replay envelope hash mismatch"
            return False

        old_url = self.conv_url or (getattr(self.page, "url", "") if self.page is not None else "")
        self.refusal_history.append({
            "attempt": self.fresh_replay_count,
            "conversation_url": old_url,
            "response": self.last_response,
            "goal_hash": self.original_goal_hash,
        })
        try:
            if self.page is not None:
                self.page.close()
        except Exception:
            pass
        self.page = None
        self.drv = None
        self.conv_url = ""
        self.conv_title = ""

        self.fresh_replay_count += 1
        self.status = "fresh_replay"
        self.recovery_state = "fresh_replay"
        self.reason = "policy refusal -> identical task in a fresh conversation"

        # Conversation-local state must not leak into the replay.
        self.turn = 0
        self._turn_sent_at = 0.0
        self.no_progress = 0
        self.last_norm = None
        self.last_response = ""
        self._continue_count = 0
        self.transient = 0
        self.first_transient_ts = 0.0
        self._toolerr_ts = 0.0
        self._last_text = None
        self._stable_since = None
        self._count_before = 0
        self._t_send = 0.0
        self._cooldown_until = 0.0
        self.closed = False

        attempt_key = "%s_a%d" % (self._tx_base_key, self.fresh_replay_count)
        self._tx = _Transcript(getattr(self._tx, "dir", None), attempt_key, self.name, self.goal)
        self.transcript = self._tx.path or ""
        if self.transcript:
            self.attempt_transcripts.append(self.transcript)

        try:
            self.page = _open_fresh(self._context, self._agent_url)
            self.drv = CopilotWebDriver(self.page)
        except Exception as e:
            self.status, self.outcome = "error", "ERROR"
            self.reason = "fresh replay open failed: %s: %s" % (type(e).__name__, e)
            return False

        # This is the same initial payload as the original non-plan review task.
        self.job = conversation_start_label(self.name + "-replay%d" % self.fresh_replay_count) + PROTOCOL + self.goal
        self.status = "ready"
        return True

    def _reset_agent_error_window(self):
        """Forget stale agent-disabled evidence after a different recoverable state wins."""
        self._copilot_err_streak = 0
        self._agent_err_ts = 0.0

    def attach(self, context, agent_url):
        """Open this worker's tab and make it ready to send. On failure -> error.
        A resume_conv worker opens its EXISTING conversation URL (carrying prior context)
        instead of a fresh agent chat; re-navs then return to that conversation too."""
        self._context = context
        open_url = self.resume_conv or agent_url
        self._agent_url = open_url
        # A SOCKET IF ONE IS ON OFFER, A TAB OTHERWISE. Nothing downstream branches on this:
        # the socket driver answers to the same names, so the turn loop cannot tell. A worker
        # RESUMING a named conversation is never offered one -- resume means "reopen that URL",
        # and a socket has no URL to reopen.
        # WHICH TRANSPORT THIS GOAL SHOULD USE, asked before one is requested. Until now the
        # answer was "a socket whenever the route offers one", which sends Work IQ goals over
        # a transport that cannot reach Work IQ and relies on the fallback to notice -- and
        # the fallback does not always notice, because an answer formed without that context
        # can still look like an answer. transport_policy holds the fixed predicate.
        want_socket = True
        try:
            from relay.transport_policy import SOCKET, choose
            want_socket = choose(self.goal, kind=getattr(self, "task_kind", "") or "") == SOCKET
        except Exception:
            pass                      # a policy that cannot answer must not cost a goal
        if not self.resume_conv and want_socket:
            drv = _socket_route().driver_for(self.name)
            if drv is not None:
                self.page, self.drv, self.socket = None, drv, True
                self.status = "ready"
                return True
        try:
            self.page = _open_fresh(context, open_url)
            self.drv = CopilotWebDriver(self.page)
            self.status = "ready"
            # BUG 4d fix: proactively run the EXISTING auto-consent click-through once, right
            # after the composer has rendered (_open_fresh only returns once it has), instead
            # of ONLY reactively from _decide after a real reply already contained consent
            # markers. This makes a connection-manager card that appears immediately after
            # first open get auto-clicked instead of being left for the (much more disruptive)
            # last-resort surface() in _decide. Reuses self._auto_consent's existing three
            # tiers unchanged -- no click logic reimplemented here; each tier no-ops safely
            # when there is nothing to click, so this is safe to call unconditionally.
            # Best-effort: never let a consent-probe failure fail the attach.
            try:
                if self._auto_consent():
                    print("[relay_fleet] %s: startup proactive auto-consent succeeded" % self.name)
            except Exception:
                pass
            return True
        except Exception as e:
            self.status, self.outcome = "error", "ERROR"
            self.reason = "open failed: " + type(e).__name__ + ": " + str(e)
            return False

    def close(self):
        """Release the tab (frees ~0.3-0.6 GB). Idempotent; never raises."""
        if self.closed:
            return
        self.closed = True
        try:
            # THE POSITIVE EXAMPLES TOO. A record of only the failures teaches a classifier
            # that everything fails; the goals that went the whole way over a socket are half
            # the training set and they are only knowable here, at the end.
            _socket_route().record(
                "worker_done", worker=self.name, goal=(self.goal or "")[:600],
                route=("socket" if getattr(self, "socket", False) else "tab"),
                fell_back=bool(getattr(self, "_socket_fell_back", False)),
                turns=self.turn, outcome=self.outcome, status=self.status,
                reason=(self.reason or "")[:200])
        except Exception:
            pass
        try:
            if getattr(self, "socket", False) and self.drv is not None:
                self.drv.close()          # a socket is cheap, but it is not free
        except Exception:
            pass
        try:
            if self.page is not None:
                if not self.conv_url:
                    self._capture_url()      # last chance: a guid that landed late, before we close
                self.page.close()
        except Exception:
            pass
        try:
            if self._refuter_session is not None:
                self._refuter_session.close()     # don't leak the side-page tab
        except Exception:
            pass
        try:
            if self._research_session is not None:
                self._research_session.close()    # don't leak the research side-page tab
        except Exception:
            pass
        self.page = None
        self.drv = None

    def cancel(self):
        """User asked to stop+release this one from the cockpit. Mark terminal so the
        loop won't reopen it, then free its tab."""
        if self.status in TERMINAL:
            self.close()
            return
        self.status, self.outcome = "cancelled", "CANCELLED"
        self.reason = "手動で停止・タブ解放しました"
        self.close()

    def _capture_url(self):
        try:
            if self.page is not None:
                u = self.page.url
                # Capture a real conversation guid (UUID) after EITHER /conversation/<guid> (the
                # old agent) OR /chat/<guid> (the new agent T_02140b8c, which never had a
                # /conversation/ segment -> conv_url stayed empty for every worker). The UUID gate
                # means the agent BASE url (/chat/agent/T_xxx, not a UUID) is never mistaken for a
                # conversation. Called every poll, so a guid that appears a beat late is still caught.
                m = _CONV_GUID_RE.search(u.split("?", 1)[0])
                if m:
                    self.conv_url = u
                    self._tx.note_guid(m.group(1))
        except Exception:
            pass
        # Best-effort: scrape Copilot's auto-generated chat title once it exists. M365
        # names a chat a beat after the first turn, so we keep trying (cheaply) until we
        # have one, then stop. The cockpit/chat use conv_title as the card headline when
        # present (else the goal text), so a miss is harmless. Fully isolated in try/except
        # -- a scrape failure must never affect the relay loop.
        try:
            if not self.conv_title and self.drv is not None:
                t = self.drv.conversation_title()
                if t:
                    self.conv_title = t
        except Exception:
            pass

    def steer(self, text):
        """Queue a user steering message; injected as the worker's next turn (Codex-
        style mid-task redirection). Takes priority over CONTINUE/FIX."""
        if text:
            self.steer_msgs.append(text)

    def _task_anchor(self, nudge):
        """Prepend the worker's task identity to a GENERIC retry/continue/fix nudge so a
        long or retrying conversation can't drift into a different role. A bare
        'もう一度' / '次のステップを' carries no task identity; after many turns the agent can
        forget WHICH task it is on. We re-state cwd + a one-line goal summary every time.
        Uses only fields already on the worker (self.cwd, self.goal); never raises."""
        try:
            anchor = ""
            one = ""
            for ln in (self.goal or "").splitlines():
                ln = ln.strip()
                if ln:
                    one = ln[:160]
                    break
            where = (self.cwd or "").strip()
            if where and one:
                anchor = "あなたは %s で「%s」を修正中です。その作業を続けてください。\n" % (where, one)
            elif one:
                anchor = "あなたは「%s」を修正中です。その作業を続けてください。\n" % one
            elif where:
                anchor = "あなたは %s での作業を続けてください。\n" % where
            return anchor + nudge if anchor else nudge
        except Exception:
            return nudge

    def _begin_send(self):
        # max_turns=0 (or falsy) means unlimited -- no turn-cap check at all.
        if self.max_turns and self.turn >= self.max_turns:
            # before reporting MAXTURNS, see if the workspace ALREADY satisfies the goal's
            # acceptance checks -- if so the result is proven-done and we finish DONE+verified
            # rather than labeling an already-correct artifact MAXTURNS.
            if self._salvage_via_checks():
                return
            if self._contract_budget and self.turn >= self._contract_budget:
                self.status, self.outcome = "maxturns", "MAXTURNS"
                self.reason = "autonomy contract: turn budget %d reached" % self._contract_budget
            else:
                self.status, self.outcome, self.reason = "maxturns", "MAXTURNS", "reached max_turns"
            return
        # a queued steering message preempts the normal CONTINUE/FIX job for this turn
        if self.steer_msgs:
            self.job = ("【ユーザーからの追加指示】" + self.steer_msgs.pop(0)
                        + "\n上記を最優先で踏まえて作業を続行してください。"
                        "完了なら DONE、無理なら FAIL と理由を書いてください。")
            self._last_was_steer = True
        else:
            self._last_was_steer = False
        try:
            self._count_before = self.drv._answers().count()
            self.drv._count_before = self._count_before
            # FLEET PATH MUST NOT BLOCK: the round-robin advances every worker from one
            # thread, so send() may not sit in _wait_generation_idle for the full ~4min
            # (it would freeze the sweep -> status.json goes stale "フリート停止?" and, at
            # concurrency>1, starves the OTHER workers). Pass a SHORT gen-wait so send()
            # just checks "is the turn still generating?", waits ~2s, and if so raises
            # GenerationInProgress immediately -> we defer and the sweep moves on. The
            # generous total patience is realized across deferrals as a WALL-CLOCK budget
            # (max_gen_wait_s), not one blocking call. (run_relay's single-conversation path
            # keeps the full 240s.)
            self.drv.send(self.job, gen_wait_s=2.0)
        except ConversationClosed as e:
            # The target tab/composer is gone (conversation ended). Retrying a dead
            # target can never succeed -- terminal, skip the transient budget entirely
            # (prevents the 10x retry waste against TargetClosed pages seen in
            # send_failures.jsonl). Still let an already-satisfied workspace salvage to
            # DONE before declaring STUCK.
            if self._salvage_via_checks():
                return
            self.status, self.outcome = "stuck", "STUCK"
            self.reason = "conversation closed: %s" % (str(e),)
            return
        except GenerationInProgress as e:
            # The PREVIOUS turn was still generating when send() tried to submit (a slow
            # django/sympy turn). send() did a SHORT (~2s) non-blocking check and deferred
            # -- this is NOT a failure. Reschedule the SAME job WITHOUT consuming
            # a turn OR the transient budget (the W0 django__django-14730 fix: a slow turn
            # must never be counted into STUCK). A separate, very generous cap only catches
            # a turn that LITERALLY never stops generating.
            if self._defer_generation():
                elapsed = max(0.0, time.time() - self.first_defer_ts)
                self.reason = "previous turn still generating -> wait %ds/%ds (no budget)" % (
                    int(elapsed), int(self.max_gen_wait_s))
                return
            if self._salvage_via_checks():
                return
            self.status, self.outcome = "stuck", "STUCK"
            elapsed = max(0.0, time.time() - self.first_defer_ts) if self.first_defer_ts else 0.0
            self.reason = "previous turn never stopped generating (%ds, %d waits): %s" % (
                int(elapsed), self.gen_waits, str(e)[:120])
            return
        except Exception as e:
            # STUCK-ON-REDIRECT recovery (W4 xarray-3364): a tab parked on the M365 SSO-redirect
            # / landing page has no composer, so EVERY send fails identically and retrying send
            # there can never succeed. Detect "sends keep failing AND the tab is NOT on the agent
            # surface" and RE-NAVIGATE to the agent URL (mirrors _open_fresh's about:blank re-nav)
            # before spending the transient budget. Bounded per turn so a persistently-wrong page
            # still falls through to the normal transient/terminal handling below.
            self._send_fail_streak += 1
            if self._maybe_renav_off_redirect():
                self.reason = "stuck on redirect page -> re-navigated to agent (renav %d/%d)" % (
                    self._redirect_renavs, self.max_redirect_renavs)
                return
            # a send failure is a transient (CDP/Edge/network) hiccup -- retry the turn
            # rather than giving up, up to the budget. Don't consume a turn for a failed
            # send (turn is only counted once the send actually goes through).
            if self._retry_transient():
                self.reason = "send retry %d/%d (%s)" % (self.transient, self.max_transient,
                                                         type(e).__name__)
                return
            if self._salvage_via_checks():
                return
            self.status, self.outcome = "stuck", "STUCK"
            self.reason = "send failed after %d retries: %s: %s" % (
                self.transient, type(e).__name__, str(e))
            return
        self.turn += 1
        # When this turn went out. The lock fallback below compares the server's refusal
        # record against it, so only a refusal caused BY THIS TURN counts.
        self._turn_sent_at = time.time()
        # a send actually went through -> reset BOTH the generation-wait count and the
        # wall-clock streak stamp so the next slow turn gets a fresh full patience budget.
        self.gen_waits = 0
        self.first_defer_ts = 0.0
        # a successful send proves the tab is on a live agent surface -> clear the
        # stuck-on-redirect state (streak + per-turn re-nav budget) for the next turn.
        self._send_fail_streak = 0
        self._redirect_renavs = 0
        self._tx.user(self.turn, self.job)     # persist the full sent prompt for this turn
        self._last_text, self._stable_since, self._t_send = None, None, time.time()
        self._settle_state = _settle.SettleState()
        self.status = "waiting"

    def _on_redirect_page(self):
        """True if the tab is currently parked on a non-agent / SSO-redirect / landing page
        (the W4 CsrToSSR symptom) rather than the agent conversation surface. Conservative and
        fully guarded: reads the live URL and probes for the composer. Returns False on any
        error or when there is no real page/agent URL to re-navigate to (so the new re-nav
        branch simply never fires in tests / before attach -- behaviour unchanged there).

        A page is judged 'on a redirect' when its URL carries the redirect markers, OR it is
        NOT on the agent surface AND the composer is absent (a landing page that lost the agent
        chat). The composer probe is the decisive signal -- the happy path (composer present)
        can never satisfy this, so a working agent tab is never re-navigated."""
        if self.page is None or not self._agent_url:
            return False
        try:
            url = self.page.url or ""
        except Exception:
            return False
        if looks_like_redirect_landing(url):
            return True
        # Not an explicit redirect URL: only treat as 'wrong page' if we are NOT on an agent
        # surface AND the composer is missing (so a transient send glitch on a real agent tab,
        # which still HAS a composer, is left to the normal transient retry).
        if on_agent_surface(url):
            return False
        try:
            has_composer = self.page.locator(COPILOT_SELECTORS["composer"]).count() > 0
        except Exception:
            has_composer = True            # unknown -> assume present (don't re-nav on a guess)
        return not has_composer

    def _renav_to_agent_surface(self):
        """Low-level mechanics shared by every re-nav-first recovery path: goto self._agent_url on
        the CURRENT tab and wait briefly for the composer to render (mirrors _open_fresh's
        about:blank re-nav). Spends one unit of the per-turn re-nav budget and fully guards the
        goto/wait so a failure just leaves the tab where it was. Returns True iff the composer
        was observed after the goto (the agent surface is back), else False.

        This does NOT touch status/job/cooldown/streaks -- callers decide what a successful or
        failed re-nav means for them (retry-same-job vs. fall through)."""
        if self.page is None:
            # NO TAB, NOTHING TO RE-NAVIGATE. A worker can exist without a page -- the socket
            # route leaves it None -- and "did not land" is the honest answer, not a crash.
            return False
        self._redirect_renavs += 1
        landed = False
        try:
            try:
                self.page.goto(self._agent_url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
            # wait up to ~10s for the composer to appear (the agent surface is back)
            for _ in range(20):
                try:
                    if self.page.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                        landed = True
                        break
                except Exception:
                    pass
                self.page.wait_for_timeout(500)
        except Exception:
            landed = False
        return landed

    def _renav_budget_ok(self):
        """True iff we still have re-nav budget left this turn AND a real page/agent_url to
        re-nav to. Guards every re-nav-first call site so a permanently-broken agent still
        exhausts the budget and falls through to its existing (genuine) STUCK path rather than
        re-navving forever. Never raises."""
        try:
            if self.page is None or not self._agent_url:
                return False
            return self._redirect_renavs < self.max_redirect_renavs
        except Exception:
            return False

    def _maybe_renav_off_redirect(self):
        """If sends keep failing because the tab is stuck on a redirect/landing page, RE-NAVIGATE
        to the agent URL the worker was launched to drive (the same URL attach() opened) and reset
        the send-failure streak -- instead of retrying send into a page that has no composer (W4
        xarray-3364: ~29/30 consecutive empty-composer failures over ~1h until the turn timed out).

        Fires only when ALL of: (a) the consecutive send-failure streak has reached the threshold,
        (b) re-navs spent this turn are under the per-turn cap, and (c) the tab really is on a
        redirect/non-agent page (see _on_redirect_page -- the happy path with a live composer can
        never satisfy this). Re-arms the worker to 'ready' with a short cooldown so the next sweep
        re-sends the SAME job on the freshly-navigated agent surface. Returns True if it re-navigated
        (caller should return and let the loop continue), else False (caller falls through to the
        normal transient/terminal handling, so a persistently-wrong page still ends up STUCK)."""
        if self._send_fail_streak < self.redirect_renav_threshold:
            return False
        if self._redirect_renavs >= self.max_redirect_renavs:
            return False
        if not self._on_redirect_page():
            return False
        # Re-navigate this tab to the agent conversation URL (mirrors _open_fresh's about:blank
        # re-nav: goto + wait briefly for the composer to render). Fully guarded -- a failed goto
        # leaves the tab where it was and we just fall through to transient handling next time.
        self._renav_to_agent_surface()
        # Reset the send-failure streak: we have moved the tab, so the previous failures no
        # longer reflect the current page. Re-arm to 'ready' to re-send the SAME job. If the
        # composer never appeared, the streak will simply re-accumulate and, once the per-turn
        # re-nav cap is hit, fall through to the existing transient/terminal handling.
        self._send_fail_streak = 0
        self._cooldown_until = time.time() + 2.0
        self.status = "ready"
        return True

    def _maybe_renav_before_signal(self):
        """RE-NAV-FIRST recovery for the CONSENT and AGENT-DEAD signals (2026-07 fix): both symptoms
        are usually NOT a genuine consent-needed / dead-agent state but a DRIFTED tab -- the SPA
        normalizes the URL to the CsrToSSR/auth=2 landing while silently dropping the loaded custom
        agent (default Copilot / bare /chat/conversation/<id>, no MCP connector). Every tool call
        there fails identically and returns either a connection-consent card or a canned error, which
        used to go straight into the fragile popup click-through or straight toward STUCK. A standalone
        `python -m relay.edge_reconnect` reliably recovers from exactly this by RE-NAVIGATING a fresh
        page to the agent titleId URL -- so try that FIRST, in-loop, before spending the consent-popup
        or dead-agent wall-clock budget.

        Fires only when the tab really looks drifted (_on_redirect_page) AND we still have per-turn
        re-nav budget (_renav_budget_ok -- shared with _maybe_renav_off_redirect, so the two paths
        can never together exceed max_redirect_renavs re-navs on one turn). On a successful re-nav
        (composer back), re-arms the worker to RETRY the same job and returns True. On budget
        exhaustion, no page, or a failed re-nav, returns False so the caller falls through to its
        existing (genuine) consent/dead-agent handling -- a truly consent-gated or truly dead agent
        still ends up STUCK within the normal window, never loops forever. Never raises."""
        try:
            if not self._renav_budget_ok():
                return False
            if not self._on_redirect_page():
                return False
            if not self._renav_to_agent_surface():
                return False
            self.job = self._task_anchor(RETRY_JOB)
            self._cooldown_until = time.time() + 2.0
            self.status = "ready"
            self.reason = "drifted off agent surface -> re-navigated to agent (renav %d/%d), retry" % (
                self._redirect_renavs, self.max_redirect_renavs)
            return True
        except Exception:
            return False

    def _defer_generation(self):
        """Schedule a non-failure RESCHEDULE because the previous turn is still generating.
        Unlike _retry_transient this does NOT touch self.transient (the transient/STUCK
        budget) -- waiting out a slow turn is not a failure. It re-arms the worker to 'ready'
        with a short cooldown and re-sends the SAME job. Bounded primarily by a WALL-CLOCK
        budget (max_gen_wait_s) so realized patience is load-independent, with max_gen_waits
        as a secondary guard. Returns True if rescheduled, else False (the budget was hit ->
        the caller should go terminal)."""
        now = time.time()
        # stamp the FIRST defer of this wait-streak; the streak's clock resets to 0 on a
        # successful send (see _begin_send, where gen_waits is also reset).
        if self.first_defer_ts <= 0.0:
            self.first_defer_ts = now
            self._defer_progress_sig = self._gen_progress_sig()   # baseline output at wait start
        # A SOCKET TURN CARRIES ITS OWN DEADLINE, so this one does not apply to it. Measured
        # 2026-08-21 on twelve real past goals: the heaviest (a DB lookup with skill_match)
        # was still working at 206s, this tab-era budget expired first, and the worker went
        # STUCK while the turn was healthy -- 1 of 12, and the only failure in the run.
        #
        # The budget exists because a wedged TAB gives no signal that it is wedged. A socket
        # does: Conversation bounds the turn with turn_timeout_s, raises when it passes, and
        # the driver's `failed` then sends this worker to a tab. Two deadlines for one turn is
        # one deadline too many, and the shorter one here knows nothing about the turn.
        #
        # The progress test below cannot cover this: a socket answer can arrive as a single
        # snapshot at the end, so `_gen_progress_sig` is flat for a turn that is streaming
        # perfectly well underneath.
        if getattr(self, "socket", False) and getattr(self.drv, "_is_generating", None):
            try:
                if self.drv._is_generating():
                    self.gen_waits += 1          # still counted, so the wait is observable
                    self._cooldown_until = now + 2.0
                    self.status = "ready"
                    return True
            except Exception:
                pass
        budget_hit = (now - self.first_defer_ts) > self.max_gen_wait_s
        count_hit = self.gen_waits >= self.max_gen_waits
        # PROGRESS-AWARE cutoff. Hitting the budget/count is a real STUCK ONLY if the turn made
        # no new output the whole time. A legitimately long agent turn (it kept streaming, or
        # added a reply) is alive, not hung -> restart the budget from the latest progress and
        # keep waiting. This is exactly the >max_gen_wait_s real turn (folder/file/DB work) that
        # must NOT be a false STUCK. Only a turn FROZEN for the whole budget goes terminal.
        if budget_hit or count_hit:
            cur = self._gen_progress_sig()
            if cur >= 0 and cur > self._defer_progress_sig:
                self._defer_progress_sig = cur
                self.first_defer_ts = now
                self.gen_waits = 0
            else:
                return False
        self.gen_waits += 1
        # short, fixed cooldown before re-checking (send() itself does the long minutes-wait
        # for generation to finish; this is just a brief breather between deferrals).
        self._cooldown_until = now + 2.0
        self.status = "ready"
        return True

    def _gen_progress_sig(self) -> int:
        """Cheap signature of conversation OUTPUT progress: (#agent replies, last-reply length).
        Rises while the agent is actively producing output (streaming text or a new reply), so a
        legitimately long agent turn keeps advancing it; a wedged/frozen page leaves it flat.
        _defer_generation uses it to tell 'slow but alive' from 'genuinely hung'. Never raises."""
        try:
            ans = self.drv._answers()
            n = ans.count()
            last_len = len((ans.last.inner_text() or "")) if n else 0
            return n * 10_000_000 + last_len
        except Exception:
            return -1

    def _retry_transient(self):
        """Schedule a retry for a TRANSIENT failure (send/timeout/likely-transient STUCK) with
        SDK-style exponential backoff (0.5->1->2->4->8s capped, -25% jitter). The budget is a
        WALL-CLOCK WINDOW (NET_RETRY_WINDOW_S), not a tiny count: a flaky network/devtunnel can be
        down for minutes, and a 10-count budget exhausted in ~55s -> a brief blip 'ended everything'.
        Now the worker keeps retrying (every ~8s) for up to the window, riding out a real outage, and
        gives up only if it PERSISTS past the window. Returns True if a retry was scheduled, else
        False (window exceeded -> caller goes terminal). first_transient_ts resets on a real reply."""
        now = time.time()
        if self.first_transient_ts <= 0.0:
            self.first_transient_ts = now
        if (now - self.first_transient_ts) > NET_RETRY_WINDOW_S:
            return False
        self.transient += 1
        self._cooldown_until = now + transient_backoff(self.transient)   # backoff caps at ~8s
        self.status = "ready"
        return True

    def _eval_ceiling_s(self):
        """The longest a single blocking acceptance eval should take = the max per-check
        timeout (the SWE-bench shell check carries timeout=1300), bounded by the global
        EVAL_STALL_CEILING_S so a mis-set huge timeout can't disable the failsafe."""
        try:
            mx = max((float(c.get("timeout", 0) or 0) for c in (self.checks or [])),
                     default=0.0)
        except Exception:
            mx = 0.0
        # use the larger of the configured check timeout and the global ceiling, so the
        # watchdog never kills an eval that is still within its own declared budget.
        return max(EVAL_STALL_CEILING_S, mx)

    def _mark_eval_busy(self):
        """Enter a blocking acceptance eval: set status 'verifying' + a busy deadline and
        flush a status snapshot so the watchdog sees the marker before the sweep freezes."""
        self.eval_busy_until = time.time() + self._eval_ceiling_s()
        # show 'verifying' on the card too (and so a status-only watchdog read also defers).
        if self.status not in TERMINAL:
            self.status = "verifying"
        if self._busy_writer is not None:
            try:
                self._busy_writer()
            except Exception:
                pass

    def _clear_eval_busy(self):
        """Leave a blocking acceptance eval (always, even on failure/exception)."""
        self.eval_busy_until = 0.0

    def _poll_research(self):
        """Drive the NON-BLOCKING deep-research (status=='researching'). None -> still researching,
        so the round-robin keeps stepping every OTHER worker; a report string -> inject it and
        continue; '' (failure/timeout) -> continue without it. Mirrors _poll_refute, so a worker's
        minutes-long deep-dive never freezes the fleet."""
        report = self._research_session.poll()
        if report is None:
            return False                     # still researching; the sweep keeps moving
        self._research_session = None
        if report:
            self.job = ("依頼された調査が完了しました。以下が結果です。これを踏まえて作業を続けて"
                        "ください。\n--- 調査結果 ---\n" + report + "\n--- 調査結果ここまで ---\n"
                        + CONTINUE_JOB)
            self.reason = "research %d/%d 反映して続行" % (self.research_count, self.max_research)
        else:
            self.job = ("調査結果を取得できませんでした。調査なしで可能な範囲で進めるか、無理なら"
                        "最後の行に STUCK: 理由 と書いてください。")
            self.reason = "research %d/%d 結果なし" % (self.research_count, self.max_research)
        self.status = "ready"
        return False

    def _salvage_via_checks(self):
        """Last-chance acceptance salvage for the EXHAUSTION paths (spec 3-3 verify gate,
        applied where the worker would otherwise go terminal NON-done). Before burning a
        turn it doesn't have (at max_turns) or giving up on a timeout/stuck, run the SAME
        acceptance checks the DONE gate uses against the current workspace. If they already
        PASS, the artifact is proven-done regardless of whether Copilot ever emitted a clean
        DONE -- so finish DONE+verified instead of MAXTURNS/STUCK. (Observed on HumanEval_56:
        solution.py passed the canonical test but per-turn timeout-retries ate the 10-turn
        budget before a clean DONE landed.) No checks -> nothing to prove against -> can't
        salvage. Runs blocking like the single-relay DONE gate (run_all_blocking); this is a
        once-per-worker terminal moment, not the hot round-robin path. Returns True iff
        salvaged (status is now terminal DONE)."""
        if not self.checks:
            return False
        # run_all_blocking is SYNCHRONOUS and can take the full eval timeout (SWE-bench docker
        # eval ~1300s). It freezes the single-thread round-robin -> status.json stops advancing.
        # Mark this worker "verifying" with a deadline and flush a snapshot BEFORE blocking, so
        # the watchdog sees a legitimate bounded eval (not a wedged Edge) and waits instead of
        # hard-resetting. Cleared in finally so the marker never sticks past the eval.
        self._mark_eval_busy()
        try:
            passed, detail = run_all_blocking(self.checks, cwd=self.cwd)
        finally:
            self._clear_eval_busy()
        self.last_verify_detail = detail
        if not passed:
            return False
        self.verified = True
        self.status, self.outcome = "done", "DONE"
        self.reason = "checks already pass at exhaustion -> salvaged DONE (%s)" % (
            (detail or "")[:160])
        return True

    def tab_load(self):
        """RAM footprint of this worker in OPEN browser tabs right now: the main agent tab plus
        any sub-agent side-pages currently open (research / refuter). The fleet's RAM admission
        counts THIS (sum over workers), not just the worker count -- so an auto/ultra task that
        fans out to 3 tabs is treated as ~3 tabs of RAM pressure, automatically, without a human
        hand-capping concurrency. 0 while the worker holds no tab (pending / closed)."""
        # A SOCKET WORKER HOLDS NO MAIN TAB -- but it can still open side-pages, and counting
        # zero for it would under-report exactly the tabs this accounting exists to bound.
        n = 1 if self.page is not None else 0
        rs = self._research_session
        if rs is not None and getattr(rs, "page", None) is not None:
            n += 1
        fs = self._refuter_session
        if fs is not None and getattr(fs, "page", None) is not None:
            n += 1
        return n

    def tab_weight(self, assume_socket=None):
        """PEAK tabs this worker may hold: 1 main + 1 if it can delegate research + 1 if it runs a
        refuter. Admission RESERVES this many tab-slots, so N lean workers can't all be admitted at
        1 tab each and THEN fan out together to 3 tabs each (the balloon that crashed the Edge). For
        an auto/ultra task -- which nearly always researches AND refutes -- the peak IS the typical
        load, so this is accurate, not merely conservative. min-effort = 1 (no side-pages).

        SWE_SIDEPAGE_RESERVE=0 relaxes this to reserve ONLY the main tab, so auto/ultra tasks
        PARALLELIZE on a RAM-tight box (a proper benchmark must run at the production effort, not be
        downgraded to min for throughput). Safe because the side-page open is INDEPENDENTLY
        ram_room-gated: RefuterSession/ResearchSession.start() defer their new_page() until
        ram_room_for_tab() clears the ~2 GB floor (refuter.py / agent_profiles.py). So relaxing
        this risks at worst a transient STALL (a worker waits in 'refuting'/'researching' for RAM),
        never the balloon crash the peak-reservation was added to prevent."""
        # `assume_socket` EXISTS BECAUSE THIS IS ASKED BEFORE THE ANSWER IS KNOWN.
        #
        # `self.socket` is set inside attach(), and admission weighs a PENDING worker before
        # attach runs -- so a worker about to take a socket and hold no tab at all was billed as
        # a tab. With a budget of 2 that made the fleet strictly serial on BOTH routes: the
        # admitted worker drops to weight 1 once attached, the next one is still charged 2, and
        # 1 + 2 exceeds 2. Measured 2026-08-24: four goals started 43, 39 and 56 seconds apart
        # over sockets, each waiting for the previous one's reply, with the cap set to 2.
        main = 0 if (self.socket if assume_socket is None else assume_socket) else 1
        if os.environ.get("SWE_SIDEPAGE_RESERVE", "1") == "0":
            return main
        return main + (1 if self.max_research > 0 else 0) + (1 if self.refuter else 0)

    def _consent_tier0_allow(self):
        """Tier 0 of _auto_consent, factored out so _decide can try it BEFORE the re-nav-first
        recovery: if there is literally a 許可/Allow button on the CURRENT page, clicking it is
        the cheapest and most correct move (nothing to do with a drifted tab). Returns True iff a
        button was found and clicked. Never raises."""
        if self.page is None:
            # Consent is a card in a tab. Without one there is nothing to click, and nothing
            # to approve either: a route that does not reach an external connector never
            # raises the question.
            return False
        pg = self.page
        if pg is None:
            return False
        # CLICK THE WHOLE CHAIN. Work IQ is no longer one connection: a fresh conversation
        # surfaces seven cards in sequence (User, Copilot, Teams, SharePoint, OneDrive,
        # Mail, Calendar), each appearing only once its predecessor is approved -- observed
        # directly on 2026-08-10. A single click left six pending, which matters most HERE:
        # the fleet creates a conversation per worker, so every worker met the full chain
        # and every worker gave up after one card.
        #
        # LAST, not first: approved cards stay in the transcript and stack downward, so the
        # one still waiting is the bottom one. Bounded, so a card that re-renders instead
        # of resolving cannot spin.
        # Shared with edge_reconnect so there is ONE implementation of the stop condition.
        # It matters: "click until no Allow remains" never terminates, because an approved
        # card keeps its buttons in the transcript -- measured, that wasted 72s per attempt.
        # The chain GROWS by one card per approval, so growth is the real signal.
        try:
            from relay.edge_reconnect import _click_consent_chain
            return bool(_click_consent_chain(pg))
        except Exception:
            return False

    def _auto_consent(self, skip_tier0=False):
        """Re-establish the MCP connection AUTOMATICALLY. NOT a credential entry -- the Bearer key
        is already on the connector; this only re-selects + commits the connection. Tiered so the
        cheapest path that works wins; the manual-surface fallback in _decide fires only if ALL
        tiers fail. Returns True iff a commit happened (caller then sends RETRY_JOB).

        Tier 0 -- variant (a): a 許可/Allow button in the consent card on self.page -> one click.
                  Skipped when skip_tier0=True (the caller already tried it itself, e.g. _decide's
                  re-nav-first ordering tries Tier 0 first, then re-nav, and only then falls back
                  to here for Tier 1/2 -- so we don't re-probe the same button twice).
        Tier 1 -- DIRECT-HIT: a cached connection-manager URL -> open a new page in the same
                  context, go there, and fix ALL stale rows (skip if it redirects to a login page;
                  do NOT delete the cache on failure -- the popup flow refreshes it).
        Tier 2 -- popup flow: edge_reconnect.click_through_consent(self.page), which now caches the
                  URL and fixes ALL rows (handles both variant (a) and the 接続マネージャー popup)."""
        if self.page is None:
            return False
        pg = self.page
        if pg is None:
            return False
        from .edge_reconnect import (
            click_through_consent, fix_all_stale_connections, load_conn_url,
        )
        # Tier 0: variant (a) -- 許可/Allow directly on the card.
        if not skip_tier0 and self._consent_tier0_allow():
            return True
        # Tier 1: DIRECT-HIT a cached connection-manager URL.
        try:
            url = load_conn_url()
            if url:
                from .edge_recover import looks_like_login
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
                    # cached URL 404'd / redirected to login -> fall through to Tier 2 (which
                    # refreshes the cache). Deliberately do NOT clear the cache here.
                finally:
                    if np is not None:
                        try:
                            np.close()
                        except Exception:
                            pass
        except Exception:
            pass
        # Tier 2: popup flow (also (re)caches the URL and fixes ALL rows).
        try:
            return bool(click_through_consent(pg))
        except Exception:
            return False


    def _heap_mb(self):
        """This tab's JS heap in MB, or None where the browser will not say.

        `performance.memory` is Chromium-only and absent under some privacy settings, so this
        must degrade to None rather than raise -- a memory reading is a nice-to-have and a
        worker that dies for lack of one is a worse outcome than a tab that grows.
        """
        if self.page is None:
            return None                      # no tab, no renderer heap -- not an error
        try:
            v = self.page.evaluate(
                "() => performance.memory ? performance.memory.usedJSHeapSize : null")
            return None if v is None else float(v) / 1048576.0
        except Exception:
            return None

    def _memory_pressure(self):
        """Whether this conversation has grown heavy enough to be worth replacing.

        Checked at a TURN BOUNDARY, which is what makes this safe: the fleet's recycle already
        re-anchors the goal and the agent re-derives progress from the files on disk, so the
        thing being discarded is chat history the design already treats as expendable. Doing it
        while the conversation is healthy is gentler than the existing trigger, which fires
        only once the model has started returning the token-limit error on every turn.
        """
        if FLEET_HEAP_RECYCLE_MB <= 0:
            return False
        # SINCE THE LAST MEMORY RECYCLE, not since the start. `self.turn` is the worker's
        # global budget counter and is deliberately NOT reset when a conversation is replaced
        # (max_turns has to keep counting), so an absolute guard would pass immediately after
        # a recycle -- and if the fresh tab's heap has not fallen below the threshold yet, the
        # worker would recycle every turn until it burned through max_recycles and went stuck.
        if (self.turn - getattr(self, "_heap_recycle_turn", 0)) < FLEET_HEAP_MIN_TURNS:
            return False
        heap = self._heap_mb()
        if heap is None:
            return False
        self._last_heap_mb = heap
        return heap >= FLEET_HEAP_RECYCLE_MB

    def _decide(self, resp):
        self.last_response = resp
        self._tx.assistant(self.turn, resp)    # persist the full Copilot reply for this turn
        # HEAP PER TURN, RECORDED. The recycle threshold above is provisional and the only way
        # to replace it with a measured one is to know MB-per-turn on real work -- a worker's
        # turns carry OCR text and spreadsheet rows and are nothing like the bridge probe's
        # fixed round trips, so the bridge's turn count could not be copied. One number per
        # turn, beside the transcript that already exists.
        try:
            _h = self._heap_mb()
            if _h is not None:
                self._tx.metric(self.turn, "heap_mb", round(_h, 1),
                                recycles=self._recycles)
        except Exception:
            pass
        # Parse optional NEXT/CONFIDENCE turn markers (informational only, no gating).
        from relay.copilot_autopilot_relay import extract_next, extract_confidence
        self.next_step = extract_next(resp)
        self.self_confidence = extract_confidence(resp)

        # CONVERSATION TOKEN-LIMIT RECYCLE. A hands-off worker pumps ONE chat; a long task
        # (lots of OCR text / Excel rows) eventually exhausts the model token budget
        # (OpenAIModelTokenLimit) and then EVERY later turn returns the same error until
        # MAXTURNS. Detect it, open a FRESH conversation on the same agent surface, and
        # re-anchor the goal so the agent re-derives progress from disk (the target Excel /
        # output files) instead of from the lost conversation history.
        heavy = (not conversation_exhausted(resp)) and self._memory_pressure()
        if conversation_exhausted(resp) or heavy:
            self._recycles += 1
            self._heap_recycle_turn = self.turn
            if self._recycles > self._max_recycles:
                self.status, self.outcome = "stuck", "STUCK"
                self.reason = (f"conversation recycled too often; exceeded "
                               f"max_recycles={self._max_recycles}")
                return
            landed = False
            try:
                self.page.goto(self._agent_url, wait_until="domcontentloaded", timeout=45000)
                for _ in range(30):
                    self.page.wait_for_timeout(1000)
                    if self.page.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                        landed = True
                        break
            except Exception:
                landed = False
            if not landed:
                # could not get a fresh composer -> fall through to normal transient handling
                self.status, self.outcome = "stuck", "STUCK"
                self.reason = "token-limit recycle: fresh conversation did not render"
                return
            self.job = (conversation_start_label(self.name + "-recycle%d" % self._recycles)
                        + PROTOCOL + RECYCLE_PREFIX + self.goal)  # re-anchor in the fresh chat
            self.reason = (
                f"ヒープ {getattr(self, '_last_heap_mb', 0):.0f}MB → 新会話で続行 "
                f"({self._recycles}/{self._max_recycles})" if heavy else
                f"会話トークン上限 → 新会話で続行 ({self._recycles}/{self._max_recycles})")
            try:
                default_notify("♻ Fleet 会話リサイクル", self.reason)
            except Exception:
                pass
            return
        # DEAD-AGENT / DEAD-PATH detector. The Copilot agent surfaces a generic failure instead of
        # doing the task -- "予期しないエラー / SystemError" (e.g. the devtunnel to the MCP dropped on
        # a network switch) or "ページをもう一度読み込んで... / 管理者に問い合わせて" (the agent is
        # wedged, or has been ADMIN-BLOCKED -- observed when one agent was disabled while others kept
        # working). Each reply carries a fresh timestamp/session-id, so the exact-text no-progress
        # check never fires and the worker burns ALL its turns (50-turn MAXTURNS observed). Catch the
        # pattern, bail FAST after a few, and go STUCK so the goal can be re-submitted on a healthy
        # agent rather than wasting the whole turn budget on a dead endpoint.
        _low = resp.lower()
        # CONNECTION-CONSENT -- NOT a credential/sign-in event, but the regulation is NOT
        # "never surface" either: consent must be resolved FULLY AUTOMATICALLY, and surfacing
        # the Edge is the LAST RESORT that fires ONLY once every automatic tier has genuinely
        # failed. The agent's tool call returned the connector's connection-SELECT confirm card
        # instead of a result. AUTO-CONSENT: the relay clicks it through (接続マネージャー ->
        # レビュー -> 送信する / 許可) via the same three-tier _auto_consent() and re-invokes the
        # tool via RETRY_JOB -- this fully-automatic ladder is unchanged. Only once that ladder
        # is EXHAUSTED (streak >= 2) does this call surface() once (truthful: gated on its real
        # return value, never a blind "surfaced!" claim) so the user can approve in the actual
        # agent chat; if surface() cannot even bring a window up, THEN it goes STUCK with an
        # honest manual-recovery reason (see the exhaustion branch below).
        #
        # RE-NAV-FIRST (2026-07 fix): the consent card is frequently a SYMPTOM of a drifted tab
        # (the SPA normalized the URL but silently dropped the loaded custom agent), not a genuine
        # consent-needed state -- and the connection-manager popup flow is fragile precisely
        # because that cached URL is often not populated. So: try a real Allow button on the
        # CURRENT page first (cheapest, and correct if this really is consent), then RE-NAV to the
        # agent surface (reloading the agent tends to make tools work again with no consent dance
        # at all), and only once re-nav budget is exhausted do we fall to the existing (fragile)
        # connection-manager/popup tiers -- and, past those, to the last-resort surface() below.
        if any(m in resp for m in CONSENT_MARKERS) or any(m in _low for m in CONSENT_MARKERS):
            # Consent is a separate, recoverable state. Do not let an older SystemError/admin-block
            # window survive into the manual-approval path and immediately raise a misleading
            # "agent stopped/disabled" toast after the Edge is surfaced for consent.
            self._reset_agent_error_window()
            self._consent_streak += 1
            if not self._consent_auto_tried:
                self._consent_auto_tried = True
                if self._consent_tier0_allow():
                    self.job = self._task_anchor(RETRY_JOB)
                    self.reason = "auto-consent: Allowボタンをクリックし再呼出"
                    return
                if self._maybe_renav_before_signal():
                    return
                if self._auto_consent(skip_tier0=True):
                    self.job = self._task_anchor(RETRY_JOB)
                    self.reason = "auto-consent: 接続を確定し再呼出"
                    return
            if self._consent_streak >= 2:
                # All automatic tiers are exhausted. LAST RESORT (not a repeat of the old
                # always-STUCK behavior): surface the dedicated Edge, pointed at THIS worker's
                # agent conversation (not the top page), so the user can approve by hand. Fire
                # this at most once per worker (guarded by _consent_surfaced); the TRUTHFUL
                # result gates both the notify text and whether we retry or give up.
                if not self._consent_surfaced:
                    self._consent_surfaced = True
                    ok = False
                    try:
                        from .edge_recover import surface
                        ok = bool(surface(open_url=self._agent_url))
                        if ok:
                            # BUG 4a/4b fix: this surface() had no paired rehide() at all --
                            # fire-and-forget, so the window stayed foreground until the whole
                            # fleet process exited. Precisely detecting "consent resolved" here
                            # is hard (the next sweep of THIS worker is what would notice, and
                            # by then other workers may also be relying on the same shared
                            # Edge), so schedule the bounded safety net instead: force the
                            # window back down on its own after CONSENT_SURFACE_FORCE_REHIDE_SEC
                            # regardless of what the user does.
                            _schedule_force_rehide()
                    except Exception:
                        ok = False
                    self._consent_surfaced_ok = ok
                    if ok:
                        self.job = self._task_anchor(RETRY_JOB)
                        self.reason = ("⚠ MCP接続の自動承認に失敗 → 専用Edgeを前面に出しました。"
                                       "表示された画面で接続を許可してください。承認後に自動で再試行します "
                                       "(%s)" % self.name)
                        try:
                            default_notify("⚠ MCP接続の承認が必要",
                                           "専用Edgeを前面に出しました。表示された画面で接続を許可してください "
                                           "(%s)" % self.name)
                        except Exception:
                            pass
                        return
                    self.status, self.outcome = "stuck", "STUCK"
                    self.reason = ("⚠ 自動承認も自動フォアグラウンド化も失敗。手動で PowerShell から: "
                                   "powershell -NoProfile -ExecutionPolicy Bypass -File "
                                   "scripts\\start_companion_edge.ps1 -Foreground を実行し、専用Edgeで"
                                   "接続を許可してください。承認後、このゴールを再投入してください。")
                    try:
                        default_notify("⚠ MCP接続の自動承認・自動表示に失敗",
                                       "手動で PowerShell から scripts\\start_companion_edge.ps1 -Foreground "
                                       "を実行し、専用Edgeで接続を許可してください (%s)" % self.name)
                    except Exception:
                        pass
                    return
                # Already surfaced once this worker. If that surface() genuinely worked, give the
                # user a bounded number of extra retries to notice the foregrounded Edge and
                # approve before giving up for good (never loop forever on an unattended card).
                if self._consent_surfaced_ok and self._consent_streak < (2 + CONSENT_SURFACE_RETRY_MAX):
                    self.job = self._task_anchor(RETRY_JOB)
                    self.reason = ("専用Edge表示済み、承認待ち -> 自動再試行 (%d)" % self._consent_streak)
                    return
                self.status, self.outcome = "stuck", "STUCK"
                self.reason = ("⚠ MCP接続の自動承認に失敗。専用Edgeを前面に出しましたが、承認がまだ"
                               "完了していません。表示されたEdgeで接続を許可してから、このゴールを"
                               "再投入してください。（自動表示に失敗していた場合は手動で: powershell "
                               "-NoProfile -ExecutionPolicy Bypass -File scripts\\start_companion_edge.ps1 "
                               "-Foreground）")
                return
            self.job = self._task_anchor(RETRY_JOB)
            self.reason = "auto-consent 失敗 -> 再試行 (%d)" % self._consent_streak
            return
        # UNLOCK-REQUIRED: a write/exec tool hit a locked client IP. Auto-inject unlock(password)
        # with the LOCAL .env password (NOT the agent's persistent instructions), then resume the
        # goal. Bounded -- the M365 backend IP can rotate and re-lock, so a few auto-unlocks are
        # normal; past the cap STUCK with an actionable reason. Uses _looks_locked() (distinctive
        # marker + dominance) rather than a bare substring match so a long security-review
        # response that merely discusses unlock() is never mistaken for the real lock error.
        if _looks_locked(resp, getattr(self, "_turn_sent_at", 0.0)):
            pw = _unlock_password()
            if not pw:
                self.status, self.outcome = "stuck", "STUCK"
                self.reason = ("⚠ 書込/実行に unlock が必要だが MCP_UNLOCK_PASSWORD が未設定。"
                               ".env に設定して再投入してください。")
                return
            if self._unlock_attempts < MAX_UNLOCK_ATTEMPTS:
                self._unlock_attempts += 1
                self.job = PROTOCOL + (UNLOCK_PREFIX % pw) + self.goal
                self.reason = "コネクタ未解錠 → unlock 自動投入 (%d/%d)" % (
                    self._unlock_attempts, MAX_UNLOCK_ATTEMPTS)
                # WITHOUT THIS THE UNLOCK IS NEVER SENT. 'ready' is the state that sends
                # self.job; the branch composed the job and left the worker in 'waiting', so
                # the next sweep re-read the SAME reply, re-classified it as locked, and spent
                # another attempt -- four gone in about eight seconds, and the message blamed
                # a rotating IP and a wrong password for a turn that was never sent. Every
                # sibling branch that sets self.job sets this too; this one did not.
                self.status = "ready"
                return
            self.status, self.outcome = "stuck", "STUCK"
            self.reason = ("⚠ unlock を %d 回投入したが解錠が続かない。M365バックエンドの送信元IPが"
                           "毎回変わる(unlockはIP単位)か、MCP_UNLOCK_PASSWORD不一致の可能性。"
                           % self._unlock_attempts)
            return
        # TOOL-BACKEND-UNREACHABLE: the agent's tool calls failed (devtunnel/network blip) and it
        # self-locked claiming its tools don't exist. INFRA-FALSE, not a miss. Re-send the GOAL (the
        # "new input" it demands) to ride out the blip; give up only past the window, as a re-queueable
        # infra stuck (NOT counted as a coding miss).
        if any(m in resp for m in TOOL_UNREACHABLE_MARKERS) or any(m in _low for m in TOOL_UNREACHABLE_MARKERS):
            now = time.time()
            if self._toolerr_ts <= 0.0:
                self._toolerr_ts = now
            if (now - self._toolerr_ts) > AGENT_ERR_WINDOW_S:
                self.status, self.outcome = "stuck", "INFRA_STUCK"
                self.reason = ("⚠ ツール経路(devtunnel/網)が%d分以上不通でツール呼び出し不可。エージェントは"
                               "『ツールが存在しない』と誤判定し自己ロックしている。**タスク失敗でなくインフラ起因**"
                               "(網/トンネル復旧後に再投入＝reverify対象)。" % int((now - self._toolerr_ts) / 60))
                return
            self.job = PROTOCOL + self.goal          # re-send the goal as 'new input' to unlock it
            self._cooldown_until = now + transient_backoff(2)
            self.status = "ready"
            self.reason = "tool path down (infra) -> re-send goal, riding out outage"
            return
        # CANNED-NONANSWER: the headless->default-Copilot fallback. Placed AFTER the consent and
        # tool-unreachable handlers so a genuine consent card / explicit tool-missing message still
        # take priority. This fires when the ?titleId= custom agent failed to resolve (headless
        # window wedge) and the tab silently dropped to DEFAULT Copilot (no MCP connector) -- every
        # tool call fails and the agent returns a fixed non-answer that matches neither of those.
        # It is INFRA (not a coding miss): re-queueable, and mirrors TOOL_UNREACHABLE's
        # INFRA_STUCK classification, never a solved/failed task. Fully exception-guarded so this
        # new branch can never raise out of _decide and break the live sweep.
        if any(m in resp for m in CANNED_NONANSWER_MARKERS) or \
                any(m in _low for m in CANNED_NONANSWER_MARKERS):
            try:
                now = time.time()
                self._canned_streak += 1
                if self._canned_ts <= 0.0:
                    self._canned_ts = now
                # (a) LOGIN WALL: the session needs interactive sign-in. Surface the Edge ONCE so
                # the user can sign in, RE-QUEUE the same job (RETRY_JOB, not a miss), and give up
                # only after a bounded window/count as INFRA_STUCK (sign-in required).
                try:
                    on_login = edge_recover_looks_like_login(self.page.url if self.page else "")
                except Exception:
                    on_login = False
                if on_login:
                    if not self._signin_surfaced:
                        self._signin_surfaced = True
                        surfaced_ok = False
                        try:
                            # pass the agent URL this worker is driving so the headed
                            # relaunch (if one happens) lands on the real conversation,
                            # not the launcher's default generic top page.
                            surfaced_ok = edge_recover_surface(open_url=self._agent_url)
                            if surfaced_ok:
                                # BUG 4b defense-in-depth: this sign-in surface is never paired
                                # with a rehide() anywhere in this file (only _open_fresh's own
                                # surface() is) -- schedule the bounded safety net so the window
                                # comes back down on its own even if the user never signs in.
                                _schedule_force_rehide()
                        except Exception:
                            surfaced_ok = False
                        self._signin_surfaced_ok = surfaced_ok
                        try:
                            if surfaced_ok:
                                default_notify("⚠ サインインが必要",
                                               "専用Edgeを前面に出しました。サインインしてください (%s)" % self.name)
                            else:
                                default_notify("⚠ サインインが必要 (自動表示も失敗)",
                                               "専用Edgeを自動で前面に出すことに失敗しました。手動で次を実行して"
                                               "サインインしてください: powershell -NoProfile -ExecutionPolicy "
                                               "Bypass -File scripts\\start_companion_edge.ps1 -Foreground (%s)"
                                               % self.name)
                        except Exception:
                            pass
                    if (now - self._canned_ts) > CANNED_LOGIN_WINDOW_S \
                            or self._canned_streak >= CANNED_LOGIN_MAX:
                        self.status, self.outcome = "stuck", "INFRA_STUCK"
                        if getattr(self, "_signin_surfaced_ok", False):
                            self.reason = ("⚠ 定型の無回答が継続し、セッションはサインイン待ち。前面に出した"
                                           "**専用Edgeでサインイン**してから再投入してください。**タスク失敗でなく"
                                           "サインイン未完(INFRA)**=再投入対象。")
                        else:
                            self.reason = ("⚠ 定型の無回答が継続し、セッションはサインイン待ち。さらに専用Edgeを"
                                           "自動で前面に出すことにも失敗した。→ 手動で `powershell -NoProfile "
                                           "-ExecutionPolicy Bypass -File scripts\\start_companion_edge.ps1 "
                                           "-Foreground` を実行してサインインしてから再投入してください。"
                                           "**タスク失敗でなくサインイン未完(INFRA)**=再投入対象。")
                        return
                    self.job = self._task_anchor(RETRY_JOB)
                    self._cooldown_until = now + transient_backoff(2)
                    self.status = "ready"
                    if getattr(self, "_signin_surfaced_ok", False) or not self._signin_surfaced:
                        self.reason = ("サインイン待ち(定型無回答) -> 専用Edgeでサインイン後に自動再試行 "
                                       "(%d回)" % self._canned_streak)
                    else:
                        self.reason = ("サインイン待ち(定型無回答)、専用Edgeの自動表示は失敗 -> 手動でEdgeを"
                                       "前面に出してサインイン後に自動再試行 (%d回)" % self._canned_streak)
                    return
                # (b) NOT a login wall = the headless->default-Copilot fallback. PREFER the cheap,
                # per-tab redirect recovery: re-navigate the tab back to the agent URL. If it
                # re-navigated, the next sweep re-sends on the agent surface.
                # _maybe_renav_off_redirect fires only when its own preconditions hold; nudge the
                # send-fail streak so it is eligible on this infra signal.
                if self._send_fail_streak < self.redirect_renav_threshold:
                    self._send_fail_streak = self.redirect_renav_threshold
                if self._maybe_renav_off_redirect():
                    self.reason = ("定型無回答(既定Copilotフォールバック) -> エージェントURLへ再ナビ "
                                   "(renav %d/%d)" % (self._redirect_renavs, self.max_redirect_renavs))
                    return
                # (c) ESCALATION -- last resort, one-shot per worker, fleet-wide disruptive.
                # Only after re-nav has already been exhausted this worker AND the canned
                # non-answer persists AND we have not escalated yet. A headless companion Edge
                # cannot resolve the ?titleId= wedge by re-nav within the SAME process, so force
                # a HEADED relaunch via surface(port=<companion CDP port>). This KILLS the shared
                # Edge (disrupts ALL workers), hence the strong guards. Then RE-QUEUE (not a miss).
                if self._redirect_renavs >= self.max_redirect_renavs \
                        and not self._headed_recovery_done:
                    self._headed_recovery_done = True
                    surfaced_ok = False
                    try:
                        # pass the agent URL so the forced headed relaunch lands on the real
                        # conversation instead of the launcher's default generic top page.
                        surfaced_ok = edge_recover_surface(port=_companion_cdp_port(),
                                                           open_url=self._agent_url)
                        if surfaced_ok:
                            # BUG 4b defense-in-depth -- see the sign-in branch's identical
                            # comment above; this headed-relaunch surface is likewise never
                            # paired with a rehide() anywhere in this file.
                            _schedule_force_rehide()
                    except Exception:
                        surfaced_ok = False
                    try:
                        if surfaced_ok:
                            default_notify("🖥 ヘッドフル復旧",
                                           "定型無回答が解消せず、専用Edgeをヘッドフルで再起動しました (%s)" % self.name)
                        else:
                            default_notify("⚠ ヘッドフル復旧に失敗",
                                           "定型無回答が解消せず自動でヘッドフル再起動を試みましたが失敗しました。"
                                           "手動で次を実行してください: powershell -NoProfile -ExecutionPolicy "
                                           "Bypass -File scripts\\start_companion_edge.ps1 -Foreground -Port %d (%s)"
                                           % (_companion_cdp_port(), self.name))
                    except Exception:
                        pass
                    self.job = self._task_anchor(RETRY_JOB)
                    self._cooldown_until = now + transient_backoff(3)
                    self.status = "ready"
                    if surfaced_ok:
                        self.reason = ("定型無回答が再ナビでも解消せず -> **専用Edgeをヘッドフル再起動**して"
                                       "再投入(最終手段・1回のみ)")
                    else:
                        self.reason = ("定型無回答が再ナビでも解消せず、**専用Edgeのヘッドフル再起動も失敗**"
                                       "(ヘッドレス→ヘッドフル切替を確認できず)。→ 手動で `powershell -NoProfile "
                                       "-ExecutionPolicy Bypass -File scripts\\start_companion_edge.ps1 "
                                       "-Foreground` を実行してから再投入してください。")
                    return
                # nothing left to try: infra-classified STUCK (re-queueable, NOT a coding miss)
                self.status, self.outcome = "stuck", "INFRA_STUCK"
                self.reason = ("⚠ 定型の無回答が継続。headless の ?titleId= 解決失敗で既定Copilot"
                               "(MCPコネクタ無し)にフォールバックしている疑い。再ナビ/ヘッドフル復旧でも"
                               "解消せず。**タスク失敗でなくインフラ(接続/エージェント未確立)**=再投入対象。")
                return
            except Exception:
                # NEVER raise out of _decide: on any unexpected error, fall through to the normal
                # handling below (the reply may still hit AGENT_DEAD / no-progress paths) rather
                # than breaking the live sweep.
                pass
        if any(m in _low for m in AGENT_DEAD_MARKERS):
            now = time.time()
            if self._agent_err_ts <= 0.0:
                self._agent_err_ts = now
            self._copilot_err_streak += 1
            # RE-NAV-FIRST (2026-07 fix): "agent dead" is very often just "tab drifted off the
            # agent surface" -- the SystemError/admin-block text is a generic canned reply that a
            # drifted tab returns for EVERY tool call, indistinguishable from a genuinely dead/
            # banned agent by text alone. Before letting this count toward the wall-clock STUCK
            # window, check whether the tab is actually parked on a redirect/non-agent page and,
            # if so and re-nav budget remains, RE-NAVIGATE and RETRY once -- reloading the agent
            # tends to make tools work again, exactly like standalone edge_reconnect. This does
            # NOT weaken the genuine-outage handling below: a real outage/dead agent has no
            # composer-less redirect page to fix, so _maybe_renav_before_signal simply returns
            # False (or budget runs out) and the existing wall-clock STUCK still fires on schedule.
            if self._maybe_renav_before_signal():
                return
            # WALL-CLOCK window, not a 3-strike count: a devtunnel SSL flap / brief Copilot blip
            # surfaces as a SystemError too, and STUCKing after 3 quick errors (~seconds) meant a
            # momentary outage killed the worker. Keep retrying WITH BACKOFF for AGENT_ERR_WINDOW_S
            # so an outage is ridden out; only a failure that PERSISTS past the window is treated as
            # a genuinely down/banned agent (then the actionable Copilot-Studio message applies).
            if (now - self._agent_err_ts) > AGENT_ERR_WINDOW_S and self._copilot_err_streak >= 3:
                # SPLIT (2026-07 fix #2): a previous change just DELETED the desktop notify here
                # because it fired on every generic transient error too (a false positive) -- that
                # hid the symptom instead of fixing it. The real fix is to only claim "agent
                # stopped/disabled" when (a) an ADMIN_BLOCK-worded reply actually matched (not just
                # the generic transient boilerplate) AND (b) our own path to the tools is confirmed
                # healthy right now (_infra_healthy) -- so we can be sure the failure is on the
                # agent/Copilot-Studio side, not a network/tunnel outage wearing the same words.
                admin_block_matched = any(m in _low for m in ADMIN_BLOCK_MARKERS)
                infra_ok = False
                if admin_block_matched:
                    try:
                        infra_ok = _infra_healthy()
                    except Exception:
                        infra_ok = False
                if admin_block_matched and infra_ok:
                    # TRUE POSITIVE path: admin-block wording + our own infra is fine -> the
                    # failure really does look like this specific agent being stopped/disabled.
                    self.status, self.outcome = "stuck", "STUCK"
                    self.reason = ("⚠ エージェント応答エラーが%d分以上継続(%d回連続)。MCP接続は正常"
                                   "(自ホスト/トンネルとも応答あり)なので網の問題ではなく"
                                   "**エージェント自体が応答していない**。→ Copilot Studio でこのエージェントが"
                                   "**停止/無効化(管理者ブロック)されていないか確認**してください"
                                   "（他のエージェントが動くなら本エージェント固有の block の可能性大）。"
                                   "健全なエージェントに切り替えて再投入を。"
                                   % (int((now - self._agent_err_ts) / 60), self._copilot_err_streak))
                    try:
                        default_notify(
                            "⚠ エージェントが停止/無効化されている可能性",
                            "Copilot Studio でこのエージェントの停止/無効化を確認してください (%s)" % self.name)
                    except Exception:
                        pass
                    return
                # Otherwise: only GENERIC transient wording matched, OR infra looks unhealthy right
                # now -- either way this is a network/tunnel outage wearing agent-error clothing,
                # NOT proof this agent is disabled. Classify as INFRA_STUCK (re-queueable, mirrors
                # TOOL_UNREACHABLE's convention) and do NOT fire the disabled-agent toast: the user
                # cannot fix an agent-disable in Copilot Studio when the real problem is the network,
                # and a wrong toast just sends them chasing the wrong knob.
                self.status, self.outcome = "stuck", "INFRA_STUCK"
                self.reason = ("⚠ MCP接続/ネットワークが%d分以上不通(%d回連続)。**エージェント停止ではなく"
                               "接続断の可能性が高い**(管理者ブロックの文言でも自ホスト/トンネルの"
                               "healthチェックが失敗している場合を含む)。ネットワーク/トンネルを確認し、"
                               "復旧後に再投入してください(タスク失敗ではなくインフラ起因=reverify対象)。"
                               % (int((now - self._agent_err_ts) / 60), self._copilot_err_streak))
                return
            self.job = self._task_anchor(RETRY_JOB)
            self._cooldown_until = now + transient_backoff(self._copilot_err_streak)  # back off, don't hammer
            return
        self._reset_agent_error_window()
        # P2c policy-refusal recovery. Infrastructure/tool/login/canned/admin signals above
        # have priority, so their generic errors can never be misclassified as a policy refusal.
        if looks_like_policy_refusal(resp):
            self.refusal_count += 1
            if self.resilience_profile != "off" \
                    and self.fresh_replay_count < self.max_fresh_replays:
                self._start_fresh_replay()
                return
            if self.resilience_profile != "off" and self.fresh_replay_count > 0:
                self.refusal_history.append({
                    "attempt": self.fresh_replay_count,
                    "conversation_url": self.conv_url or getattr(self.page, "url", ""),
                    "response": resp,
                    "goal_hash": self.original_goal_hash,
                })
                self.status, self.outcome = "content_refused", "CONTENT_REFUSED"
                self.recovery_state = "content_refused"
                self.recovery_cause = "task_content"
                self.recovery_result = "needs_decomposition"
                self.reason = "identical task refused in two independent conversations"
                return
        norm = " ".join(resp.lower().split())[:300]
        self.no_progress = self.no_progress + 1 if norm and norm == self.last_norm else 0
        self.last_norm = norm
        up = resp.upper()
        last_line = (resp.strip().splitlines() or [""])[-1].upper()
        # GOAL-DELIVERY recovery (additive, exception-safe): if the agent says it never
        # received the task -- whether or not it dressed that up as STUCK -- the goal text
        # didn't land in the tab. Re-send the GOAL ITSELF (verbatim, via PROTOCOL) rather
        # than a generic retry nudge, which on round 5 spun 10 empty retries. Bounded by
        # max_goal_resends; only the plan-pending phase is exempted (it legitimately has no
        # task body yet for the executor).
        try:
            _gns = goal_not_seen(resp)
        except Exception:
            _gns = False
        if _gns and not (self.plan_mode and not self._plan_approved) \
                and self._goal_resends < self.max_goal_resends:
            self._goal_resends += 1
            self.job = (PROTOCOL + self.goal)
            self.status = "ready"
            self.reason = "goal not received by agent -> resend goal %d/%d" % (
                self._goal_resends, self.max_goal_resends)
            return
        if reported_stuck(resp):
            # DEAD-ENDPOINT EARLY-EXIT (2026-07 fix): self.no_progress (updated above, BEFORE this
            # branch, from `norm == self.last_norm`) already counts consecutive VERBATIM-identical
            # replies. A real transient outage produces CHANGING text turn to turn (different
            # errors, eventual recovery) -- only a genuinely dead endpoint echoes the exact same
            # STUCK text repeatedly. Don't wait out the full NET_RETRY_WINDOW_S (up to 30 min) on a
            # goal that has already proven it cannot change; go terminal now. This does NOT touch
            # the wall-clock cap itself (a slow-but-changing outage still rides the full window via
            # _retry_transient below) -- it only adds an earlier terminal condition.
            if self.no_progress >= NET_RETRY_NOPROGRESS_MAX:
                if self._salvage_via_checks():
                    return
                self.status, self.outcome, self.reason = "stuck", "STUCK", \
                    ("identical STUCK reply repeated %d times (no progress) -> dead endpoint, "
                     "not waiting out the full %ds retry window" % (self.no_progress, NET_RETRY_WINDOW_S))
                return
            # Under load, an agent STUCK is usually a downstream symptom of a transient
            # tool/network failure (the agent couldn't write a file etc.). Retry the turn
            # (re-prompt to try the tools again) before giving up, up to the budget.
            #
            # AND THE COUNT IS THE BUDGET HERE, which it had stopped being. `_retry_transient`
            # was changed from a count to a 30-minute wall-clock window -- correct for a
            # transport failure, where a short count exhausted during a brief outage and
            # "ended everything". It is not correct for a model saying it is stuck: with the
            # backoff capped near 8s, that window is ~225 re-prompts of an agent that has
            # already told us it cannot proceed. Meanwhile `max_transient` survived only
            # inside the message, which printed "retry 3/2" against a limit that limited
            # nothing.
            #
            # That mattered beyond this loop. `max_transient` is fed by the evolution
            # manifest's `max_retries`, described there as "transient-retry budget per
            # worker" -- so the self-improvement loop could tune a parameter with no effect
            # and measure the noise. Transport retries keep the window; this one keeps the
            # count, which is what both the name and the manifest already claimed.
            if self.transient < self.max_transient and self._retry_transient():
                self.job = self._task_anchor(RETRY_JOB)
                self.reason = "STUCK -> transient retry %d/%d" % (self.transient, self.max_transient)
                return
            if self._salvage_via_checks():
                return
            self.status, self.outcome, self.reason = "stuck", "STUCK", \
                "agent reported STUCK (after %d retries)" % self.transient
            return
        self.transient = 0   # a real (non-stuck) response -> the transient issue cleared
        self.first_transient_ts = 0.0   # reset the outage window on a healthy reply
        self._toolerr_ts = 0.0          # tool path is back -> clear the tool-unreachable window
        # deep-research delegation: the agent wrote `RESEARCH: <query>` asking for an external
        # deep-dive. Spawn the Researcher sub-agent (side page), feed its report back as the next
        # turn, and continue. Capped per worker (max_research); past the cap, tell it to proceed.
        rq = extract_research(resp)
        if rq and self._context is not None and self.max_research > 0:
            if self.research_count >= self.max_research:
                self.job = ("これ以上は調査を依頼できません（上限到達）。今ある情報で進めるか、"
                            "無理なら最後の行に STUCK: 理由 と書いてください。")
                self.status = "ready"
                return
            self.research_count += 1
            # NON-BLOCKING: kick off the Researcher in a side page and enter 'researching'. The
            # round-robin keeps stepping every OTHER worker while this one's deep-dive runs (see
            # _poll_research). A blocking wait here would freeze the whole fleet for minutes and
            # cause false turn-timeouts on the siblings -- the reason v1 was unusably slow.
            from .agent_profiles import ResearchSession
            self._research_session = ResearchSession(
                self._context, rq, model_name=self.research_model,
                tx_dir=getattr(self._tx, "dir", None), parent_key=self._tx_key,
                parent_turn=self.turn, sub_index=self.research_count).start()
            self.status = "researching"
            self.reason = "🔎 外部調査中 (%d/%d): %s" % (self.research_count, self.max_research, rq[:48])
            return
        # DATA-ANALYSIS DELEGATION -- the other half of a protocol that was only half wired.
        # `extract_analyze` has existed for a long time, the agent has always been told it may
        # write `ANALYZE: <path> | <instruction>`, an ANALYST profile is configured, and the
        # SINGLE-AGENT relay acts on it. The fleet did not: it read RESEARCH: and dropped
        # ANALYZE: on the floor. So every fleet worker that asked for analysis was answered
        # with silence and carried on without it, and nothing recorded that it had asked.
        #
        # Non-blocking for the same reason research is: the analyst takes minutes, and a
        # blocking wait here would freeze every other worker in the sweep.
        az = extract_analyze(resp)
        if az and self._context is not None and self.max_research > 0:
            apath, ainstr = az
            if self.research_count >= self.max_research:
                self.job = ("これ以上は分析を依頼できません（上限到達）。自前ツールで分析するか、"
                            "無理なら最後の行に STUCK: 理由 と書いてください。")
                self.status = "ready"
                return
            if not os.path.isfile(apath):
                # NAMED, NOT SILENT. A missing file used to be indistinguishable from the
                # feature not existing, which is exactly how this stayed unnoticed.
                self.job = ("指定されたファイルが見つかりません: %s。パスを確認するか、"
                            "自前ツールで分析してください。" % apath[:200])
                self.status = "ready"
                return
            self.research_count += 1
            from .agent_profiles import ANALYST, ResearchSession
            self._research_session = ResearchSession(
                self._context, ainstr, model_name="", profile=ANALYST, upload_path=apath,
                tx_dir=getattr(self._tx, "dir", None), parent_key=self._tx_key,
                parent_turn=self.turn, sub_index=self.research_count).start()
            self.status = "researching"
            self.reason = "データ分析中 (%d/%d): %s" % (self.research_count, self.max_research,
                                                       os.path.basename(apath)[:48])
            return

        # plan phase (plan_mode): capture the proposed plan and PAUSE for approval; a steer
        # (approve as-is, or an edit) resumes into execution. Don't run DONE/CONTINUE yet.
        if self.plan_mode and not self._plan_approved:
            if plan_ready(resp):
                self.plan_steps = extract_plan(resp)
                self.status = "awaiting"
                self.reason = "計画提示・承認待ち (%d ステップ)" % len(self.plan_steps)
                return
            self.job = ("実行計画を番号付きステップで完成させ、最後の行に PLAN_READY と"
                        "書いてください（まだ実装はしないこと）。")
            self.status = "ready"
            return
        if "DONE" in up and "FAIL" not in last_line:
            self._on_done_claimed()
            return
        if self.no_progress >= self.max_no_progress:
            if self._salvage_via_checks():
                return
            # CARD/UI STALL (unrecognized consent / file-op confirm variant). A worker that keeps
            # repeating a SHORT response and never produced real work is almost always blocked
            # behind a Copilot UI card whose wording the CONSENT_MARKERS list didn't catch (e.g.
            # "desktopfile操作 書き込む内容を教えてください" -- no 接続 markers at all). That is an
            # INFRA block, NOT a coding miss: the agent never got to run a tool. Try auto-consent
            # once more (some variants still click through), then mark INFRA_STUCK so the
            # orchestrator RE-ATTEMPTS it rather than scoring it a miss (which would silently
            # under-count pass@1). Domain-general: keyed on response shape, not instance text.
            short_loop = len((resp or "").strip()) < 160
            if short_loop and not self._consent_auto_tried:
                self._consent_auto_tried = True
                try:
                    if self._auto_consent():
                        self.job = self._task_anchor(RETRY_JOB)
                        self.reason = "card-stall: auto-consent 再試行"
                        return
                except Exception:
                    pass
            if short_loop:
                self.status, self.outcome = "stuck", "INFRA_STUCK"
                self.reason = ("⚠ 短い同一応答の反復=UIカード(接続consent/ファイル操作確認)でツール呼び出しが"
                               "阻まれている可能性大。タスク失敗でなく**接続/UI未確立(INFRA)**=再投入対象。")
                return
            self.status, self.outcome = "stuck", "STUCK"
            self.reason = "no progress for %d turns" % (self.no_progress + 1)
            return
        if "FAIL" in last_line:
            self.job = self._task_anchor(FIX_JOB)
            self._continue_count = 0   # real progress signal -> the continue streak resets
        elif self._last_was_steer:
            # bridge off the steer instead of a raw CONTINUE so the redirection sticks
            self.job = ("先ほどの追加指示を踏まえて作業を続行してください。"
                        "完了なら DONE、無理なら FAIL と理由を書いてください。")
            self._continue_count = 0   # a steer is real progress -> the continue streak resets
        else:
            # HARD CAP, independent of no_progress (which only trips on a VERBATIM-identical
            # reply): a task that keeps producing slightly-different prose but never DONE
            # would otherwise ride the plain CONTINUE branch all the way to max_turns while
            # WE re-send byte-identical nudge text every turn -- the confirmed degradation
            # mechanism. Cap consecutive continues and terminate gracefully instead.
            self._continue_count += 1
            if self._continue_count >= self.max_continue:
                if self._salvage_via_checks():
                    return
                self.status, self.outcome = "stuck", "STUCK"
                self.reason = ("no DONE after %d continue nudges (stopped to avoid degrading "
                               "the model)" % self._continue_count)
                return
            self.job = self._task_anchor(_continue_nudge(self._continue_count))
        self.status = "ready"

    def _on_done_claimed(self):
        """Copilot reported DONE. With no acceptance checks, go straight to the candidate-
        done step (back-compat trust, unless a refuter is on). With checks, run the
        verification gate first."""
        self._continue_count = 0   # a DONE claim is real progress -> the continue streak resets
        if not self.checks:
            self.verified = False
            self._candidate_done()
            return
        self._pending_checks = list(self.checks)
        self._active_check = None
        self.status = "verifying"
        self._advance_check()

    def _advance_check(self):
        """Start the next pending check, or go to the candidate-done step if all passed."""
        if not self._pending_checks:
            self.verified = True
            self.reason = "acceptance verified (%d check(s))" % len(self.checks)
            self._candidate_done()
            return
        self._active_check = Check(self._pending_checks[0], cwd=self.cwd).start()

    def _candidate_done(self):
        """Machine checks passed (or none) -> a CANDIDATE done. If the refuter is enabled
        and within budget, open an independent reviewer (non-blocking) before accepting;
        otherwise finish now."""
        if (self.refuter and self._context is not None
                and self.refute_count < self.max_refute):
            self.refute_count += 1
            if self.review_lenses:
                lenses = list(self.review_lenses)
                # OPT-IN adaptive lens selection (default OFF). When MCP_ADAPTIVE_REFUTER=1,
                # learn from past per-lens refutation rates and throw only the top-k most
                # likely-to-refute lenses for THIS candidate's features -- fewer oracle calls,
                # adaptive over time. Env unset => this branch is skipped and the full fixed
                # panel runs exactly as before (byte-for-byte the old behaviour).
                # SELECTING AND RECORDING WERE THE SAME SWITCH, AND THEY ARE DIFFERENT
                # QUESTIONS. The memory was written only when adaptive selection was ON, so
                # it could only ever learn about lenses the policy had already chosen -- the
                # exploration slot keeps the others alive (measured: 15 observations of each
                # unchosen lens per 30 candidates against 30 of the chosen one), but there
                # was no way to warm it from a FULL panel at all.
                #
                # That matters beyond the bias. Section 18 compares allocation policies over
                # a corpus where every lens ran against every candidate, and with an empty
                # memory the adaptive policy returns the panel's own order -- which is the
                # fixed policy, so the comparison would put one policy on the frontier twice.
                # `MCP_REFUTER_MEMORY_RECORD=1` records without changing what runs.
                self._adaptive_features = None
                adaptive = os.environ.get("MCP_ADAPTIVE_REFUTER") == "1"
                if adaptive or os.environ.get("MCP_REFUTER_MEMORY_RECORD") == "1":
                    from .refuter_memory import RefuterMemory, extract_features
                    self._adaptive_mem = RefuterMemory()
                    self._adaptive_features = extract_features(self.goal, self.last_response)
                if adaptive:
                    try:
                        k = int(os.environ.get("MCP_ADAPTIVE_REFUTER_K", "2"))
                    except ValueError:
                        k = 2
                    lenses = self._adaptive_mem.select_lenses(
                        self._adaptive_features, lenses, k)
                self._panel_queue = lenses
                self._panel_results = []
                self._start_next_lens()
            else:
                from .refuter import RefuterSession
                self._refuter_session = RefuterSession(
                    self._context, self._agent_url or "", self.goal,
                    self.last_response).start()
            self.status = "refuting"
            return
        if self.fresh_replay_count:
            self.recovery_cause = "session_state"
            self.recovery_result = "recovered"
            self.recovery_state = "recovered"
        self.status, self.outcome = "done", "DONE"

    def _start_next_lens(self):
        from .refuter import RefuterSession
        lens = self._panel_queue.pop(0)
        self._refuter_session = RefuterSession(
            self._context, self._agent_url or "", self.goal,
            self.last_response, lens=lens).start()

    def _poll_refute(self):
        """Drive the non-blocking refuter / review panel. REFUTED -> feed the reason back
        and keep working; UPHELD/UNCLEAR -> accept. A panel runs one independent reviewer
        per lens in turn, then aggregates by majority."""
        r = self._refuter_session.poll()
        if r is None:
            return False
        kind, reason = r
        if self.review_lenses:
            lens = self._refuter_session.lens
            self._panel_results.append((lens, kind, reason))
            self._refuter_session = None
            # OPT-IN adaptive memory: after each lens's verdict is known, record whether it
            # refuted, keyed by this candidate's features. No-op unless the adaptive hook in
            # _candidate_done was taken (MCP_ADAPTIVE_REFUTER=1 => _adaptive_features is set).
            if getattr(self, "_adaptive_features", None) is not None:
                try:
                    self._adaptive_mem.record(
                        self._adaptive_features, lens, refuted=(kind == "REFUTED"))
                except Exception:
                    pass
            if self._panel_queue:                  # consult the remaining lenses first
                self._start_next_lens()
                return False
            from .refuter import aggregate_panel    # all in -> majority vote
            kind, reason = aggregate_panel(self._panel_results)
        else:
            self._refuter_session = None
        # surface the verdict in status.json (reason) so the run is observable live
        self.reason = ("refuter#%d: %s%s"
                       % (self.refute_count, kind,
                          (": " + reason) if reason else ""))[:300]
        if kind == "REFUTED":
            self.job = REFUTE_FIX_JOB % (reason or "(no reason)")
            self.status = "ready"
            return False
        if self.fresh_replay_count:
            self.recovery_cause = "session_state"
            self.recovery_result = "recovered"
            self.recovery_state = "recovered"
        self.status, self.outcome = "done", "DONE"
        return True

    def _poll_verify(self):
        """Drive the running acceptance check non-blockingly. On pass, advance to the
        next check (all pass -> DONE). On fail, re-inject the GROUND TRUTH and let the
        agent keep working, up to max_verify_attempts (then STUCK/VERIFY_FAILED)."""
        if self._active_check is None:
            self._advance_check()
            return self.status in TERMINAL
        r = self._active_check.poll()
        if r is None:
            return False                 # still running -- the other workers keep moving
        passed, detail = r
        self.last_verify_detail = detail
        if passed:
            self._pending_checks.pop(0)
            self._active_check = None
            self._advance_check()
            return self.status in TERMINAL
        self.verify_attempts += 1
        self.verified = False
        if self.verify_attempts >= self.max_verify_attempts:
            self.status, self.outcome = "stuck", "VERIFY_FAILED"
            self.reason = ("acceptance check failed %d time(s): %s"
                           % (self.verify_attempts, (detail or "")[:200]))
            return True
        self.job = VERIFY_FIX_JOB % (detail or "(no detail)")
        self._pending_checks = []
        self._active_check = None
        self.status = "ready"
        return False

    def _report_socket_turns(self):
        """Tell the route about turns that WORKED, once each.

        The breaker only ever heard about failures, so `consecutive` could not fall back to
        zero and a run of thousands of good turns would still be closed by three bad ones
        spread across hours. Counted from the driver's own completed-answer count, so a turn
        is reported when it produced an answer -- not when it was merely started.

        THAT COUNT IS ALSO WHAT MAKES A FAILED TURN UNREPORTABLE: a turn that failed
        never increments it, so no ordering here can turn a failure into a success.
        Mutation testing says as much -- moving this call above the failure check
        changes nothing, which is a property of the design rather than a gap in a test.
        """
        try:
            done = self.drv._answers().count()
        except Exception:
            return
        seen = getattr(self, "_socket_turns_seen", 0)
        if done <= seen:
            return
        self._socket_turns_seen = done
        route = _socket_route()
        for _ in range(done - seen):
            route.note_success()

    def _fall_back_to_tab(self):
        """The socket stopped working for this worker. Open a tab and re-send the same turn.

        THE GOAL SURVIVES; WHETHER THE WORLD DOES DEPENDS ON THE REASON. The turn is re-sent
        verbatim, and the tab is the path every worker used before this route existed, so the
        usual cost of a socket failure is one turn's latency and one tab's RAM.

        That is the whole story only when the turn never reached the server. It did reach it
        whenever the failure was "the turn completed but carried no text" or a consent card --
        the model acted, and only the ANSWER was unusable -- and re-sending then asks for the
        act a second time. Invisible for a goal that writes a file; a second real event for one
        that sends a message or appends to a record.

        The re-send is unchanged, because trading a certain lost turn for a hazard nobody here
        has measured is not an improvement -- no fallback has fired in any recorded arm. What
        changed is that the record now names the delivery status, so a duplicate is something
        the log can show rather than something nobody thought to look for.

        Returns False only if the TAB could not be opened either, which is an ordinary open
        failure and is treated as one.
        """
        reason = getattr(self.drv, "failed", "") or "unknown"
        route = _socket_route()
        route.note_failure("%s: %s" % (self.name, reason))
        self._socket_fell_back = True
        # RECORDED IMMEDIATELY, not at the end: a run that dies mid-goal still leaves the
        # evidence behind, and this line is the only place the pairing of a goal with the
        # reason it needed a tab exists at all.
        try:
            from relay.transport_policy import classify_fallback, delivery_status
            cause, delivery = classify_fallback(reason), delivery_status(reason)
        except Exception:
            cause, delivery = "unknown", "unknown"
        route.record("fallback", worker=self.name, goal=(self.goal or "")[:600],
                     turn=self.turn, socket_turns=getattr(self, "_socket_turns_seen", 0),
                     # WHOSE FAULT, AND WHETHER THE TURN HAD ALREADY LANDED. Both were
                     # derivable from `reason` and neither was written down, so every question
                     # about them had to be answered by re-reading prose after the fact.
                     cause=cause, delivery=delivery,
                     duplicate_risk=delivery in ("delivered", "unknown"),
                     reason=reason[:300])
        try:
            self.drv.close()
        except Exception:
            pass
        self.socket, self.drv = False, None
        try:
            self.page = _open_fresh(self._context, self._agent_url)
            self.drv = CopilotWebDriver(self.page)
        except Exception as e:
            self.status, self.outcome = "error", "ERROR"
            self.reason = "socket fell back but the tab would not open: %s: %s" % (
                type(e).__name__, e)
            return False
        try:
            # The commonest reason a socket turn carries no text is a card only a tab can
            # show. Now there is a tab, so click it before re-sending into the same wall.
            self._auto_consent()
        except Exception:
            pass
        # Re-send the same job: 'ready' is the state that sends self.job, which still holds it.
        self.status = "ready"
        self._last_text, self._stable_since = None, None
        self._count_before = 0
        print("[relay_fleet] %s: socket -> tab (%s)" % (self.name, reason[:120]))
        return True

    def poll(self):
        """Advance one non-blocking step. Returns True when terminal."""
        if self.status in TERMINAL:
            return True
        # getattr, like the rest of this loop: the settle tests drive poll() on a stand-in
        # worker that never runs __init__, and a hard attribute read here would make a socket
        # feature break a test about text stability.
        if getattr(self, "socket", False):
            if getattr(self.drv, "failed", ""):
                if not self._fall_back_to_tab():
                    return True
            else:
                self._report_socket_turns()
        if self.status == PENDING:
            return False                 # not attached yet; the fleet attaches it
        if self.status == "awaiting":
            # plan proposed; paused for human approval. A steer (approval or an edit) is
            # the resume signal -- it becomes the next turn via _begin_send's steer path.
            if self.steer_msgs:
                self._plan_approved = True
                self.status = "ready"
            return False
        if self.status == "verifying":
            return self._poll_verify()
        if self.status == "researching":
            return self._poll_research()
        if self.status == "refuting":
            return self._poll_refute()
        if self.status == "ready":
            if time.time() < self._cooldown_until:
                return False             # waiting out a transient-retry backoff
            self._begin_send()
            self._capture_url()
            return self.status in TERMINAL
        if self.status == "waiting":
            self._capture_url()
            if time.time() - self._t_send > self.per_turn_timeout_s:
                # a turn that never finished is a transient stall -- retry before STUCK
                if self._retry_transient():
                    self.reason = "turn timeout -> retry %d/%d" % (self.transient, self.max_transient)
                    return False
                # retries exhausted: don't give up on an already-correct artifact -- if the
                # workspace already passes the acceptance checks, salvage it as DONE+verified.
                if self._salvage_via_checks():
                    return True
                self.status, self.outcome, self.reason = "stuck", "STUCK", \
                    "turn timeout (after %d retries)" % self.transient
                return True
            try:
                if self.drv._answers().count() <= self._count_before:
                    return False
            except Exception:
                return False
            # PRIMARY completion gate: never read/commit a turn while the agent is STILL
            # GENERATING (the live Stop/square button is showing). Reading mid-stream was
            # the root cause of partial capture (transcript turn5: 102 chars, mid-word
            # "...隠し", no end marker) -- a streaming pause longer than dwell_s made the
            # partial text look 'stable' and it was committed as the final answer, dropping
            # the rest of the reply AND its DONE/CONTINUE/STUCK tail marker. This reuses the
            # same Stop-button signal the SEND gate uses. Defensive getattr + guard so a
            # mock/stub driver without _is_generating degrades to pure text-stability
            # (back-compat with the test fakes). Reset the stability clock while generating
            # so a pre-pause partial never carries its stale stable_since across the resume.
            _isgen = getattr(self.drv, "_is_generating", None)
            if callable(_isgen):
                try:
                    if _isgen():
                        self._last_text, self._stable_since = None, None
                        self._settle_state = _settle.SettleState()
                        # LIVE PREVIEW ONLY: surface the in-flight partial so the cockpit shows
                        # mid-turn progress per worker (status.json "last" -> card body). This is
                        # display-only: we do NOT call _decide() and do NOT append to the transcript,
                        # so the authoritative answer is still taken from the stable-settle path below
                        # (preserves the partial-capture-corruption guard this branch was added for).
                        try:
                            partial = self.drv.read_last_response()
                            if partial and not _is_processing(partial):
                                self.last_response = partial
                        except Exception:
                            pass
                        return False
                except Exception:
                    pass
            t = self.drv.read_last_response()
            if _settle.unified():
                # THE ONE RULE, and for this site it is a real change rather than a move.
                # This loop has no sample requirement at all -- only a dwell -- so the guard
                # that 3,931 measured replies justified has never applied here, and a
                # streaming pause longer than dwell_s is the whole failure it was written
                # for. Gated because "stricter" is a claim, not a fact, until the A/B says
                # so; the old path below is untouched and is still the default.
                state = getattr(self, "_settle_state", None) or _settle.SettleState()
                state, outcome = _settle.settle_step(
                    state, t, now=time.time(), dwell_s=self.dwell_s, generating=False,
                    is_processing=_is_processing(t), has_marker=has_end_marker)
                self._settle_state = state
                # Keep the legacy fields in step: the cockpit and the resume path read them,
                # and leaving them frozen would make a unified run look permanently stalled.
                self._last_text, self._stable_since = state.last, state.stable_since
                if outcome != _settle.ACCEPT:
                    return False
                if getattr(self.drv, "_is_stale_repeat", lambda _t: False)(t):
                    return False
                accept = getattr(self.drv, "_accept_new_reply", None)
                if callable(accept):
                    accept(t)
                self._decide(t)
                return self.status in TERMINAL
            if _is_processing(t):
                self._last_text, self._stable_since = None, None
                return False
            if t == self._last_text:
                # A stable answer whose TAIL has no protocol marker may be a mid-stream
                # pause that briefly hid the Stop button; require an EXTENDED settle (2x
                # dwell) before committing it, vs the normal dwell for a marker-terminated
                # (DONE/CONTINUE/STUCK/...) tail. Bounded by per_turn_timeout_s, so this
                # cannot hang the round-robin -- a turn that genuinely never marks still
                # commits once it stays byte-identical for the extended window.
                need = self.dwell_s if has_end_marker(t) else self.dwell_s * 2.0
                # COMPARED TO None IN THE UNIFIED PATH, not for truthiness. Kept as-is here
                # because this is the legacy branch and it must not move, but the defect is
                # real: a clock reading of 0.0 reads as "never became stable".
                if self._stable_since and (time.time() - self._stable_since) >= need:
                    # This poll loop bypasses CopilotWebDriver.wait_for_idle -- apply the
                    # same cross-turn correspondence guard directly (see its docstring):
                    # a settled text byte-identical to the PREVIOUS turn's accepted
                    # answer on this driver is the stale-capture signature (the idle
                    # tool probe incident), not a fresh reply. Keep polling instead of
                    # re-deciding on stale text; still bounded by per_turn_timeout_s.
                    if getattr(self.drv, "_is_stale_repeat", lambda _t: False)(t):
                        return False
                    accept = getattr(self.drv, "_accept_new_reply", None)
                    if callable(accept):
                        accept(t)
                    self._decide(t)
                    return self.status in TERMINAL
                return False
            self._last_text, self._stable_since = t, time.time()
            return False
        return False


def _refresh_selfimprove_dashboard():
    """Regenerate .fleet/selfimprove_dashboard.json after a run.

    The feed used to be rebuilt only by ui/SelfImproveDashboard.cs when the operator
    opened it, so the numbers were as old as the last time somebody looked -- measured
    2026-08-10, five days stale while runs kept happening. Rebuilding here means the panel
    is already correct when it is opened.

    Best-effort and silent: it reads ledgers and writes one JSON file, so a failure is
    cosmetic and must not touch the run that just finished.
    """
    try:
        from relay.selfimprove.dashboard import write_json
        write_json()
    except Exception:
        pass


_MEMORY_HEADER = "--- このテーマでの過去の作業メモ ---"


def _with_theme_memory(goal_text):
    """Return the goal body with this theme's history prepended, or unchanged on any doubt.

    Applied to the BODY that is sent, never to the worker's goal. Recording without recall
    is pointless -- until now recall lived only in relay/code_task.py, so a fleet run could
    write a memory it would never read back -- but recall must not cost the runner its
    identity: workers are keyed by goal text (transcript key, replay envelope, and the
    theme record_task derives), and rewriting it breaks all of them.

    Never raises and never returns empty: on any failure the original body is used.
    """
    try:
        from relay.project_memory import load_notes, theme_from_goal
        text = str(goal_text or "")
        if not text or _MEMORY_HEADER in text:
            return goal_text
        notes = load_notes(theme_from_goal(text), goal=text)
        if not notes:
            return goal_text
        return "%s\n%s\n--- メモここまで ---\n\n%s" % (_MEMORY_HEADER, notes, text)
    except Exception:
        return goal_text


def _genome_default(name, fallback):
    """A default taken from the ACTIVE HARNESS rather than written in the signature.

    This is what makes max_retries and max_refute_passes evolvable in fact and not only on
    paper: an A/B over a parameter that no running code reads is two runs of the same
    program. Explicit arguments still win -- the caller who passed a value meant it.
    """
    try:
        from relay.selfimprove import runtime_config as _rc
        return {"max_transient": _rc.max_retries,
                "max_refute": _rc.max_refute_passes}[name]()
    except Exception:
        return fallback


def run_relay_fleet(context, goals, agent_url, max_turns=1000, poll_s=1.0,
                    notify=default_notify, on_tick=None, max_concurrent=None,
                    mc_box=None, add_box=None, refuter=False, max_refute=None,
                    plan_mode=False, review_lenses=None, max_transient=None, max_research=3,
                    autoscale=False, autoscale_max=None, asc_box=None,
                    autoscale_per_tab_mb=None, autoscale_headroom_mb=None,
                    autoscale_up_margin_mb=0,
                    disk_floor_gb=None, eval_disk_gb=None, disk_box=None,
                    ram_box=None,
                    transcript_dir=None, run_id="", busy_writer=None,
                     pause_box=None, stop_box=None, resilience_profile="off",
                     max_fresh_replays=0):
    """Drive len(goals) autonomous relays in parallel to completion, but never with
    more than `max_concurrent` tabs open at once (defaults to what free RAM allows).
    A goal's tab is opened only when a slot frees and CLOSED the moment it finishes.

    CONTINUOUS CAPACITY-AWARE ADMISSION (2026-06-14): this is a single continuous flow --
    pass ALL goals at once and they are admitted as fast as capacity allows, NOT in batches.
    A job that finishes frees its slot (tab RAM + the eval's disk via swe_check cleanup) and
    the NEXT queued goal is admitted on the very next sweep -- there is no batch barrier (the
    orchestrator no longer waits for a chunk of K to all finish before launching the next K).
    Admission is gated on BOTH resources:
      * RAM -- the live cap (mc_box / autoscale ram_target_cap), and
      * DISK -- C: free must stay above a reserved floor (disk_floor_gb, user-configurable via
        env SWE_DISK_FLOOR_GB or disk_box) even after the new job's eval consumes its budget.
    Both must be satisfied to open a tab, so a job is never admitted in a way that would either
    exhaust RAM (the Edge-crash failure mode) or push C: under the floor.

    `mc_box`, if given, is a 1-element list whose value is read EACH loop -- so the
    cockpit can raise/lower the live concurrency cap mid-run (set_maxtabs command).
    `disk_box`, if given, is a 1-element list with the live disk floor in GB (cockpit-settable).

    Returns a list of {name, goal, outcome, turns, reason} in goal order. `on_tick`
    (workers) is called after each round-robin sweep -- use it to log live progress."""
    if max_concurrent is None:
        max_concurrent = auto_concurrency(len(goals))
    if mc_box is None:
        mc_box = [max_concurrent]
    # autoscale ceiling: never open more than this many tabs even if RAM is plentiful
    # (the user's configured maximum / fair-use bound). Defaults to the launch cap.
    if autoscale_max is None:
        autoscale_max = max(1, max_concurrent)
    # run_id keys the transcript files for THIS invocation; if the caller didn't supply
    # one, derive a run-unique id from the start time so resumes/rounds don't collide.
    if not run_id:
        run_id = "r%x" % int(time.time())
    # busy_writer: flush a status snapshot on demand. A worker calls this right before a
    # BLOCKING acceptance eval freezes the sweep, so its 'verifying'/eval-busy marker reaches
    # status.json BEFORE on_tick stops firing -- the watchdog then waits instead of resetting.
    # Caller may inject one; otherwise default to on_tick (which writes the snapshot).
    if busy_writer is None and on_tick is not None:
        def busy_writer():
            try:
                on_tick(workers)
            except Exception:
                pass

    # live disk floor (GB): cockpit can change it mid-run via disk_box; otherwise the launch
    # value (env SWE_DISK_FLOOR_GB default) applies. <=0 disables the disk gate.
    if disk_box is None:
        disk_box = [DEFAULT_DISK_FLOOR_GB if disk_floor_gb is None else float(disk_floor_gb)]
    # live RAM floor (MB): the free-RAM reserve kept for the user's other work. Mirrors disk_box --
    # the cockpit can change it mid-run via ram_box; otherwise the launch headroom applies. The
    # autoscale (ram_target_cap) keeps this much RAM free, so a higher floor shrinks concurrency.
    if ram_box is None:
        ram_box = [FLEET_RAM_FLOOR_MB if autoscale_headroom_mb is None
                   else autoscale_headroom_mb]
    # pause/stop control (1-element lists read EACH loop, set by the cockpit via commands.json):
    # pause_box[0] True  -> freeze the fleet in place (no new turns / no new tabs / no liveness
    #                       probe) so a deliberate network switch doesn't trip FleetContextLost.
    # stop_box[0]  True  -> graceful abort: cancel every running worker and end the run.
    if pause_box is None:
        pause_box = [False]
    if stop_box is None:
        stop_box = [False]

    # ── Autonomy-contract turn budget (additive, inert without a contract) ──────────────────
    # Read the active contract once at fleet launch. If it is active and carries a budget_turns
    # > 0, tighten each worker's turn cap to min(max_turns, budget_turns). When there is no
    # active contract or budget_turns <= 0, effective_max_turns == max_turns (no change).
    # contract_budget tracks the budget value so workers can emit a clear stop reason.
    _contract_budget = None
    try:
        from tools.contract_gate import load_contract
        _c = load_contract()
        if _c is not None and _c.get("active") and isinstance(_c.get("budget_turns"), int) \
                and _c["budget_turns"] > 0:
            _contract_budget = _c["budget_turns"]
    except Exception:
        pass
    effective_max_turns = (min(max_turns, _contract_budget)
                           if _contract_budget is not None else max_turns)

    # None means "whatever the active harness says". Resolved here, once, so the value is
    # the same for every worker in the run and shows up in the fingerprint.
    if max_transient is None:
        max_transient = _genome_default("max_transient", 10)
    if max_refute is None:
        max_refute = _genome_default("max_refute", 2)

    workers = [RelayWorker(g, "w%d" % i, max_turns=effective_max_turns,
                           refuter=refuter, max_refute=max_refute, plan_mode=plan_mode,
                           review_lenses=review_lenses, max_transient=max_transient,
                           transcript_dir=transcript_dir, run_id=run_id,
                            busy_writer=busy_writer, max_research=max_research,
                            contract_budget=_contract_budget,
                            resilience_profile=resilience_profile,
                            max_fresh_replays=max_fresh_replays)
               for i, g in enumerate(goals)]
    pending = list(workers)            # FIFO queue of not-yet-attached workers

    def _active_open():
        # Worker (MAIN-tab) count -- the DISK accounting unit: only main agent tabs run the
        # Docker eval, so the disk gate reserves per main tab, not per sub-agent side-page.
        # Every worker that still HOLDS a tab counts -- including ones in 'verifying'/'refuting'
        # (a bounded eval / review still occupies its tab + the disk its eval used).
        return sum(1 for w in workers if _holds_slot(w))

    def _active_tabs():
        # ACTUAL open browser tabs across the fleet = main tabs + every open sub-agent side-page
        # (research / refuter). The RAM-pressure reading that drives the autoscale recompute and
        # the cockpit display -- "maxtabs" means TABS, so an auto worker fanned out to 3 shows as 3.
        return sum(w.tab_load() for w in workers)

    def _socket_open_now():
        """Whether a worker admitted right now would take a socket rather than a tab."""
        try:
            return bool(_socket_route().open())
        except Exception:
            return False

    def _projected_peak():
        # WORST-CASE tabs if every active worker fans out fully (sum of tab_weight). Admission
        # reserves against THIS so N lean workers can't be admitted at 1 tab each and then balloon
        # to 3 tabs each at once (the overload that wedged the Edge). _active_tabs reacts AFTER a
        # fan-out; _projected_peak prevents the over-admission that makes the fan-out unaffordable.
        return sum(w.tab_weight() for w in workers if _holds_slot(w))

    def _unfinished():
        # reconstruct the full goal (incl. acceptance checks/cwd) so a resume after a
        # wedged Edge keeps verifying -- returning bare text would drop the gate.
        #
        # FINISHED set (do NOT resurrect): DONE/CANCELLED were already excluded. STUCK is added
        # here (2026-07 overnight-stall fix) because a plain STUCK worker already exhausted its
        # full transient-retry budget (_retry_transient / NET_RETRY_WINDOW_S) or another terminal
        # check in _decide and is a genuinely broken/un-fixable goal -- resurrecting it after an
        # Edge-context-loss recovery just re-runs the SAME un-fixable goal through another full
        # 30-minute retry window. INFRA_STUCK is DELIBERATELY EXCLUDED from this set: it means our
        # own network/tunnel/tool path looked unhealthy (see the INFRA_STUCK branches in _decide),
        # NOT that the goal/agent is broken -- that's transient infra, re-queueable by design, so a
        # fresh Edge context still gets another shot at it.
        FINISHED_OUTCOMES = (
            "DONE", "CANCELLED", "STUCK", "CONTENT_REFUSED", "UNRESOLVED_REFUSAL",
        )
        return [freeze_goal_dict(getattr(w, "goal_record", None) or
                                 {"text": w.goal, "checks": w.checks, "cwd": w.cwd})
                for w in workers if w.outcome not in FINISHED_OUTCOMES]

    _reap_counter = 0
    while any(w.status not in TERMINAL for w in workers) or (add_box and len(add_box) > 0):
        # --- stop / pause control (cockpit -> commands.json -> *_box, read every loop) ---
        if stop_box[0]:
            # graceful abort: cancel every still-running worker, then fall through to the
            # cleanup below (which closes all tabs) and return the results normally.
            for w in workers:
                if w.status not in TERMINAL:
                    w.cancel()
            break
        if pause_box[0]:
            # freeze: take NO new turns, open NO tabs, and DON'T probe the context -- a
            # deliberate network switch must not trip FleetContextLost. Keep firing on_tick
            # so a later {"pause":false}/{"stop":true} is still drained and the cockpit shows
            # the paused state. Any cloud turn already in flight settles on its own; resume
            # picks up from the next poll. State is fully retained (nothing is lost).
            if on_tick:
                try:
                    on_tick(workers)
                except Exception:
                    pass
            time.sleep(poll_s)
            continue

        # auto-recovery: if the Edge/CDP context has died (wedged, or hard-reset by the
        # watchdog) a LIVE probe raises -> bail out with the unfinished goals so the
        # runner can reconnect to a fresh Edge and resume them. NB: context.pages is a
        # cached property and never raises -- cookies() actually round-trips to CDP.
        try:
            context.cookies()
        except Exception:
            raise FleetContextLost(_unfinished())

        # periodically reap orphan SSO-redirect tabs (every ~30 sweeps) so a long chunk does not
        # accumulate dead landing tabs; cheap and never touches a worker's own page.
        _reap_counter += 1
        if _reap_counter % 30 == 0:
            _reap_orphan_redirect_tabs(context, workers)

        # goals added mid-run (e.g. from the native chat while at capacity) join the
        # queue here -- priority items jump to the front, but still wait for a free slot
        # so the tab budget is never exceeded.
        if add_box:
            while add_box:
                item = add_box.pop(0)
                # item may carry checks/cwd too; goal_fields reads them (priority ignored)
                nw = RelayWorker(item, "w%d" % len(workers), max_turns=effective_max_turns,
                                 refuter=refuter, max_refute=max_refute,
                                 plan_mode=plan_mode, review_lenses=review_lenses,
                                 max_transient=max_transient, busy_writer=busy_writer,
                                  max_research=max_research,
                                  contract_budget=_contract_budget,
                                  resilience_profile=resilience_profile,
                                  max_fresh_replays=max_fresh_replays)
                workers.append(nw)
                if item.get("priority"):
                    pending.insert(0, nw)
                else:
                    pending.append(nw)

        # RAM-aware autoscale: recompute the live cap from free RAM each loop, ramping up
        # gently and draining down softly (see ram_target_cap). When on, this drives mc_box.
        # asc_box (if given) is the live [on, ceiling] control the cockpit can flip mid-run;
        # otherwise the launch-time `autoscale`/`autoscale_max` apply. The START cap is
        # whatever mc_box was initialized to (the user's DEFAULT) -- autoscale grows/shrinks
        # from there toward the ceiling.
        asc_on = autoscale
        ceiling = autoscale_max
        if asc_box:
            asc_on = bool(asc_box[0])
            ceiling = asc_box[1] or autoscale_max
        if asc_on:
            # Drive the cap off ACTUAL tabs (main + sub-agent side-pages), so the cap is a TABS
            # budget and an auto/ultra worker mid-fan-out (3 tabs) is felt as 3 tabs of pressure.
            mc_box[0] = ram_target_cap(_active_tabs(), mc_box[0], max(1, ceiling),
                                       per_tab_mb=autoscale_per_tab_mb,
                                       headroom_mb=ram_box[0],   # live, cockpit-settable RAM floor
                                       up_margin_mb=autoscale_up_margin_mb)

        # fill free tab slots from the pending queue. ADMISSION is gated on BOTH (a) the live
        # RAM cap (mc_box / autoscale) and (b) the DISK floor: open a new eval-bearing tab only
        # if C: free will stay above the reserved floor after this job's eval (disk_admission_ok
        # looks ahead by eval_disk_gb). If disk is tight we STOP admitting this sweep and let
        # running jobs finish + release their disk (swe_check cleanup), then re-admit -- the
        # continuous-flow drain. A non-positive floor disables the disk gate (normal use).
        # ADMISSION gates on the TAB budget (mc_box[0] is in tabs), RESERVING each worker's PEAK
        # fan-out (tab_weight) so the fleet's worst-case tab count never exceeds the budget -- "an
        # auto task == ~3 tabs" is accounted for at admission, automatically, with no human cap.
        # EXCEPTION: when the fleet is empty always admit ONE worker even if its peak exceeds the
        # budget (a lone auto task needs 3 tabs while maxtabs=1) -- it runs solo and the per-tab
        # ram_room gate defers its side-pages if RAM is genuinely tight, rather than deadlocking.
        # Sub-agent tabs don't run evals, so the DISK gate below still counts main tabs only
        # (_active_open). With no side-pages tab_weight==1, reducing EXACTLY to the old worker cap.
        # Weigh the pending worker as what it is ABOUT to become, not as what it is now: with the
        # route open it will take a socket and hold no tab. If the capture then fails it opens a
        # tab after all and the fleet is one tab over budget for the rest of this sweep -- the
        # next iteration reads the real weight, so it cannot compound.
        while pending and (_active_open() == 0
                           or _projected_peak()
                              + pending[0].tab_weight(assume_socket=_socket_open_now())
                              <= max(1, mc_box[0])):
            # reserve disk for THIS eval plus every already-open eval still in flight, so we never
            # admit N tabs that look fine individually but crash C: once their builds run at once.
            # PER-REPO mode sizes the reserve by each instance's actual build weight (matplotlib 7GB
            # vs requests 2GB) so light evals pair while heavy ones stay solo; flat mode uses one
            # eval_disk_gb for all. _active_open() counts just-attached tabs this sweep, so the
            # reserve grows as we admit -> same-sweep over-admission is prevented either way.
            if EVAL_DISK_PERREPO:
                # Reserve the SUM of all concurrent builds (in-flight + the one we're about to open)
                # against the crash-avoidance HARD MIN (not the soft floor): a lone heavy build may
                # dip under the soft floor and recover (admits solo: 13-7=6 >= 3), but a 2nd heavy
                # that would drag C: under the hard min is deferred; light evals pair.
                # SKIP-AHEAD: scan the queue for the FIRST job that fits alongside the in-flight
                # builds, instead of only testing the head -> a light eval (requests 2GB) behind a
                # heavy queue-head (sklearn 7GB) can still pair rather than waiting for the head.
                open_ws = [x for x in workers if _holds_slot(x)]
                base = sum(repo_eval_gb(x.cwd) for x in open_ws)
                pick = -1
                for i, p in enumerate(pending):
                    if disk_admission_ok(floor_gb=PERREPO_HARD_MIN_GB,
                                         reserve_gb=base + repo_eval_gb(p.cwd)):
                        pick = i
                        break
                if pick < 0:
                    break              # nothing in the queue fits the remaining disk this sweep
                w = pending.pop(pick)
            else:
                if not disk_admission_ok(floor_gb=disk_box[0], eval_gb=eval_disk_gb,
                                         building=_active_open()):
                    # SAY IT, ONCE. Deferring is right -- admitting past the floor is how five
                    # concurrent builds crashed C: -- but the defer was silent, so a fleet with
                    # nothing admitted looked exactly like a fleet working: the process alive,
                    # the browser fine, every worker at status=pending, turn=0. Two calibration
                    # runs sat like that for twenty-five minutes each and the only way to find
                    # out was a stack dump. Rate-limited to one line a minute so a long drain
                    # does not become the log.
                    _note_disk_defer(disk_box[0], len(pending))
                    break              # disk floor would be breached -> defer admission
                w = pending.pop(0)
            if w.status in TERMINAL:   # (shouldn't happen, but be safe)
                continue
            # BEFORE ADMITTING, make sure there is a live token to hand out -- a capture opens
            # a tab, captures and CLOSES it, so nothing is held open between refreshes. When
            # the route is off or the capture fails this is a no-op and the worker opens a tab.
            try:
                route = _socket_route()
                if route.open() and route.needs_refresh():
                    route.refresh(context, agent_url)
            except Exception:
                pass
            ok = w.attach(context, agent_url)
            if not ok:
                # attach failed. If the WHOLE Edge/context died mid-open (e.g. the
                # watchdog hard-reset it), don't burn this goal as a terminal ERROR --
                # probe the context, and if it's truly dead bail so the runner reconnects
                # and RESUMES every unfinished goal (this one included). A live context
                # means a one-off open failure -> leave the worker ERROR as before.
                try:
                    context.cookies()
                except Exception:
                    raise FleetContextLost(_unfinished())

        for w in workers:
            if w.status in TERMINAL or w.status == PENDING:
                continue
            try:
                w.poll()
            except Exception as e:
                w.status, w.outcome = "error", "ERROR"
                w.reason = type(e).__name__ + ": " + str(e)
            # the instant a worker is done, release its tab -> RAM for the next goal
            if w.status in TERMINAL and not w.closed:
                w.close()

        if on_tick:
            try:
                on_tick(workers)
            except Exception:
                pass
        time.sleep(poll_s)

    # make sure no tab is left behind
    for w in workers:
        if not w.closed:
            w.close()

    # Record what this run actually DID, per theme, so the next run on the same theme
    # starts primed instead of rediscovering. Until now only relay/code_task.py recorded
    # anything, so 2636 fleet transcripts had produced exactly one memory entry -- the
    # store existed and nothing flowed into it. Frame-side and best-effort: a memory
    # failure must never affect the run that just finished.
    try:
        from relay.project_memory import record_task, theme_from_goal
        for w in workers:
            record_task(theme_from_goal(w.goal), w.goal, w.outcome or "?",
                        note=(w.reason or getattr(w, "last_response", "") or "")[:280])
    except Exception:
        pass

    _refresh_selfimprove_dashboard()

    notify("並列自律フリート 完了",
           "%d ゴール: %s" % (len(workers), ", ".join(w.outcome or "?" for w in workers)))
    return [{"name": w.name, "goal": w.goal, "outcome": w.outcome,
             "turns": w.turn, "reason": w.reason,
             "verified": w.verified, "verify_attempts": w.verify_attempts,
             # carry the captured conversation identity into the FINAL snapshot so the
             # cockpit keeps the Copilot title/URL (and /history link) on finished cards
             # instead of reverting to the bare goal text.
             "conv_url": getattr(w, "conv_url", ""),
             "conv_title": getattr(w, "conv_title", ""),
             # full-text transcript path so finished cards can still show the WHOLE
             # conversation from disk (not just the truncated `last`).
             "transcript": getattr(w, "transcript", "") or "",
             # working dir of the goal -- orchestrators (bench/swe_run_until_done.py)
             # map workers back to instances via this in the FINAL snapshot.
             "cwd": getattr(w, "cwd", "") or "",
             # structured phase timeline (Bucket B): all status transitions this worker went
             # through, in order. Carried into the final snapshot so the UI can show the
             # complete phase spine even after the run finishes.
             "phase_events": list(getattr(w, "phase_events", [])),
             # FIX 2 (P0): carry the worker's final assistant text so fleet_runner can
             # populate display_result and keep `last` non-blank in the final snapshot.
             "last_response": getattr(w, "last_response", "") or "",
             "task_id": getattr(getattr(w, "task_envelope", None), "task_id", ""),
             "parent_task_id": getattr(getattr(w, "task_envelope", None), "parent_task_id", None),
             "campaign_id": getattr(getattr(w, "task_envelope", None), "campaign_id", ""),
             "role": getattr(getattr(w, "task_envelope", None), "role", ""),
             "depth": getattr(getattr(w, "task_envelope", None), "depth", 0),
             "goal_hash": getattr(w, "original_goal_hash", ""),
             "fresh_replay_count": getattr(w, "fresh_replay_count", 0),
             "refusal_count": getattr(w, "refusal_count", 0),
             "refusal_history": list(getattr(w, "refusal_history", [])),
             "recovery_cause": getattr(w, "recovery_cause", ""),
             "recovery_result": getattr(w, "recovery_result", ""),
             "recovery_state": getattr(w, "recovery_state", ""),
             "attempt_transcripts": list(getattr(w, "attempt_transcripts", []))}
            for w in workers]

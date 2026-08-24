"""Tool-call self-probe tracker for the bridge (self-reporting for the health layer).

Incident this closes (already diagnosed -- see the bridge/copilot_bridge.py docstring
this module is wired into): the user's interactive "desktopfile操作" chat runs through
the BRIDGE (bridge/copilot_bridge.py, Edge profile copilot-bridge-edge on CDP :9223,
HTTP :8765). FleetCockpit's health strip only probes :9222 (the FLEET Edge) and
:8000/tunnel -- none of its dots ever verify that the BRIDGE-side agent can actually
CALL an MCP tool end-to-end. So when the bridge-side MCP connector consent lapses or
its CDP session goes stale, tool calls silently die while every cockpit dot stays
green, and tools/auth_stats.py's 401 counter stays 0 because the request never even
reaches our server (the failure is entirely inside the Copilot web UI / connector,
upstream of our HTTP surface). This module is the smallest piece of state needed to
make "the bridge's agent can currently call a tool" self-report, the same way
tools/auth_stats.py made a burst of 401s self-report.

Design (mirrors tools/auth_stats.py):
  - This module is pure and import-safe: stdlib only, no playwright/pyodbc/pandas/
    fastmcp at module top, so it can be imported cheaply from anywhere (including a
    hermetic test) without paying for the bridge's heavy Playwright import chain.
  - record_probe()/get_summary() are the ONLY functions that do I/O (atomic tmp+replace
    into .fleet/tool_probe.json, same pattern as auth_stats.write_snapshot), and both are
    best-effort: any failure is swallowed so a disk hiccup or a corrupt sidecar file can
    never break the bridge's request handling or a /health read.
  - classify_probe_reply() is a separate PURE function (no I/O) so the classification
    logic itself is trivially unit-testable with canned strings, independent of the
    Playwright-driving code in bridge/copilot_bridge.py that produces the reply text.

SECOND incident this module closes (2026-08): the original probe question below --
"count the items directly under Desktop" -- has an INVARIANT answer (the count never
changes). Rotating only the wording (PROBE_INSTRUCTION_VARIANTS/next_probe_instruction) did
not help, because the agent's eventual refusal ("結果は毎回125件で不変であり...") was a
SEMANTIC objection to the answer never changing, not a lexical one about the phrasing. Worse,
because the answer is a constant the model has seen hundreds of times, it could also emit
PROBE_OK_TOKEN from memory WITHOUT calling list_directory at all -- so a green probe never
actually proved the tool path worked. new_probe_challenge()/verify_probe_reply() replace the
invariant question with one whose answer is unguessable and different every single probe (a
freshly random file name, written to a dedicated directory right before asking), while keeping
the exact same call_tool -> list_directory round-trip and the same classify_probe_reply()
`kind` vocabulary/precedence. See their docstrings below for the mechanism.

Marker lists (CONSENT_MARKERS / NO_CONNECTOR_MARKERS) are a MINIMAL LOCAL COPY of the
lists defined in relay/edge_reconnect.py (see that module's CONSENT_MARKERS /
NO_CONNECTOR_MARKERS, ~line 45). They are duplicated here -- not imported -- because
`import relay.edge_reconnect` pulls in relay.copilot_autopilot_relay's chain (playwright,
fastmcp/authlib, etc.), measured at ~23s wall-clock for the first import; that is
incompatible with this module's "stdlib-only, cheap to import anywhere" contract. If
edge_reconnect's lists change, update the copy below to match.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------------------
# Local copy of relay/edge_reconnect.py's marker lists (see module docstring above for why
# this is a copy, not an import). Keep in sync with relay/edge_reconnect.py by hand.
# ---------------------------------------------------------------------------------------
CONSENT_MARKERS = ("接続マネージャーを開く", "connection manager")
NO_CONNECTOR_MARKERS = ("実行不可", "コネクタ無し", "コネクタがありません", "ツールが使用できません",
                        # Observed live 2026-08-05: with no connector attached the chat says
                        # "ローカルのデスクトップにアクセスするツールがこの環境に存在しないため..."
                        # and offers OneDrive/SharePoint instead. None of the phrasings above
                        # match it, so the clearest "the tool is not attached" answer we get was
                        # landing in the catch-all and reading as a generic failure.
                        "ツールがこの環境に存在しない", "ツールがありません")

# Distinctive marker token the probe instruction asks the agent to emit ONLY when the
# list_directory tool call actually succeeded -- see bridge/copilot_bridge.py's
# TOOL_PROBE_INSTRUCTION for the full prompt text that requests this token.
PROBE_OK_TOKEN = "===TOOLPROBE_OK==="

# The full set of `kind` values record_probe()/classify_probe_reply() may produce.
PROBE_KINDS = ("answer", "consent_card", "canned_fallback", "timeout",
               "stale_repeat", "agent_unreachable", "error", "starting", "checking")

# Whether a probe outcome leaves the tool path usable.
#
# A failed probe and an unreachable path are not the same thing, and conflating them cost a
# real outage diagnosis: when the model hardened into refusing the probe it returned the very
# same sentence every time, the turn loop refused that repeat as a stale answer, and the
# outcome was filed as "timeout" -- "nothing came back" -- while the reply text sat in the
# failure journal the whole time. What settles this is whether a reply arrived at all, never
# what the reply says; reading the wording is the mistake that broke an earlier version of
# this check, so it stays out of here.
#
# canned_fallback is the one reply that counts against the path rather than for it: it IS the
# "no connector attached" answer, so Copilot being reachable says nothing -- the tool is
# provably not there.
PROBE_KINDS_ALIVE = ("answer", "consent_card", "stale_repeat", "error")
PROBE_KINDS_NOT_ALIVE = ("timeout", "agent_unreachable", "canned_fallback")


# ---------------------------------------------------------------------------
# When a long-lived conversation should be replaced.
#
# The bridge appends to ONE conversation forever: real user turns plus this probe every
# MCP_TOOL_PROBE_SEC, 144 a day at the default. Measured 2026-08-19, that had grown a single
# Edge tab to 1,340.9 MB -- the largest thing on a 16 GB machine, and enough to stop an
# unrelated component that needs 2000 MB free to open a page.
#
# The decision lives HERE, not in the bridge, for the same reason classify_probe_reply does:
# the bridge cannot be imported in a test (Playwright, a page-owner thread), so anything left
# in it can only be checked by reading its source for a string. A rule about when to throw
# away a working conversation is not something to verify by grep.
def should_recycle_conversation(turns: int, max_turns: int, idle_s: float,
                                min_idle_s: float) -> bool:
    """Whether the current conversation has run long enough to be replaced.

    `max_turns <= 0` disables recycling entirely -- the opt-out MCP_TOOL_PROBE_SEC=0 already
    establishes for the probe itself.

    The idle requirement is not the probe's 30-second collision guard. A recycle silently
    drops the agent's context, so it waits until nobody is plausibly mid-conversation; the
    collision guard only asks whether a turn is in flight right now.
    """
    if max_turns <= 0:
        return False
    if turns < max_turns:
        return False
    return idle_s >= min_idle_s


def probe_kind_is_alive(kind: Optional[str]) -> Optional[bool]:
    """True/False if `kind` settles whether the tool path is still usable, None for the
    transitional kinds ("starting", "checking") and anything unrecognised -- callers must
    treat None as "no evidence" rather than as a negative."""
    if kind in PROBE_KINDS_ALIVE:
        return True
    if kind in PROBE_KINDS_NOT_ALIVE:
        return False
    return None

# ---------------------------------------------------------------------------
# Probe instruction text.
#
# This used to be a single constant in bridge/copilot_bridge.py, re-sent BYTE-FOR-BYTE
# into the SAME long-lived bridge conversation every probe interval, indefinitely. That
# is the identical-nudge-repetition disease already fixed for the implementer's CONTINUE
# loop (relay/relay_fleet.py) and for the refuter's side chat (relay/refuter.py's
# _next_refuter_nudge) -- but never for this third caller. Observed result: Copilot
# eventually recognised the loop and answered "結果は変わりません。このループは続きません
# ... 完了トークンは出力しません", deliberately withholding PROBE_OK_TOKEN. The probe then
# classified its own poisoned conversation as kind="error" and the cockpit's Tool dot went
# red while every other health check, and doctor.bat, reported the stack fully healthy.
#
# Unlike the refuter's bounded retry (2 nudges), this probe repeats forever, so rotating a
# couple of phrasings is not enough on its own -- consecutive probes would still cycle a
# short fixed pattern. Each instruction therefore also carries its own sequence number, so
# no two probe turns in a conversation are ever byte-identical.
#
# Kept here rather than in the bridge because this module is stdlib-only and import-safe
# (see the module docstring), which is what lets the text be unit-tested without paying for
# the bridge's Playwright import chain -- the same reason classify_probe_reply lives here.
PROBE_INSTRUCTION_VARIANTS = (
    "システム自己診断です。call_tool 経由で list_directory を使い {dir} 直下の項目数を"
    "数えてください。",
    "接続確認です。call_tool から list_directory を呼び出し、{dir} 直下にある項目の"
    "総数を報告してください。",
    "動作確認を行います。call_tool 経由の list_directory で {dir} 直下を一覧し、"
    "その件数を答えてください。",
)

_PROBE_INSTRUCTION_TAIL = (
    "list_directory の呼び出しに成功した場合のみ、回答の最後の行に次のトークンだけを"
    "正確に出力してください: " + PROBE_OK_TOKEN + "\n"
    "ツールが呼び出せない、接続確認が必要、エラーが起きた等、成功以外の場合はこの"
    "トークンを絶対に出力しないでください。"
)


def next_probe_instruction(count: int, desktop_dir: str) -> str:
    """Instruction text for probe number `count` (1-based). Pure and deterministic.

    Rotates the opening sentence and appends the sequence number, so repeated probes in
    one conversation are never byte-identical and never settle into a short fixed cycle.
    """
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 1
    if n < 1:
        n = 1
    head = PROBE_INSTRUCTION_VARIANTS[(n - 1) % len(PROBE_INSTRUCTION_VARIANTS)]
    return head.format(dir=desktop_dir) + "\n" + _PROBE_INSTRUCTION_TAIL + \
        "\n（自己診断 #" + str(n) + "）"

# Where the cockpit / /health can read the same summary without driving the browser.
_PROBE_FILE = Path(__file__).resolve().parent.parent / ".fleet" / "tool_probe.json"

_LOCK = threading.Lock()


def classify_probe_reply(reply_text: str, agent_loaded: bool) -> Tuple[bool, str]:
    """Pure classifier: given the probe turn's reply text and whether the agent page was
    even loaded, decide (ok, kind). No I/O, no exceptions raised for any input shape --
    reply_text may be None/empty/garbage and this still returns a valid (ok, kind) pair.

    Branch order (first match wins):
      1. not agent_loaded          -> (False, "agent_unreachable")
      2. reply has a CONSENT marker -> (False, "consent_card")
      3. reply has a NO-CONNECTOR/canned marker -> (False, "canned_fallback")
      4. reply contains PROBE_OK_TOKEN -> (True, "answer")
      5. otherwise                 -> (False, "error")

    NOTE: "timeout" is NOT decided here -- it is a bridge-level outcome (DRIVER.wait_for_idle
    returning False, i.e. no exception and no text to classify) and is recorded directly by
    the caller before this function would even be invoked. See bridge/copilot_bridge.py's
    _run_tool_probe."""
    text = reply_text or ""
    if not agent_loaded:
        return False, "agent_unreachable"
    if any(m in text for m in CONSENT_MARKERS):
        return False, "consent_card"
    if any(m in text for m in NO_CONNECTOR_MARKERS):
        return False, "canned_fallback"
    if PROBE_OK_TOKEN in text:
        return True, "answer"
    return False, "error"


# ---------------------------------------------------------------------------------------
# Unguessable, ever-changing probe challenge (replaces the invariant "count items under
# Desktop" question for actually SENDING a probe -- see the module docstring's "SECOND
# incident" paragraph). classify_probe_reply()/next_probe_instruction()/PROBE_OK_TOKEN above
# are left in place: other code and tests still reference them, and they remain valid for
# anything that wants the old fixed-token contract.
# ---------------------------------------------------------------------------------------

# Dedicated directory reset before every challenge -- deliberately NOT a user-meaningful folder
# (like Desktop): resetting it means deleting everything inside, and this way that can never
# touch a file the user cares about.
#: How much of a tool call's arguments the arrival check reads. A probe names its directory
#: in its own short argument list; a real write can be megabytes, and scanning all of it on
#: every call is an observer charging the thing it observes.
_INBOUND_SCAN_CHARS = 4096

_CHALLENGE_DIR = Path(__file__).resolve().parent.parent / ".fleet" / "probe_challenge"

_CHALLENGE_INSTRUCTION_VARIANTS = (
    "システム自己診断です。call_tool 経由で list_directory を使い {dir} 直下を一覧し、"
    "そこに見つかったファイル名を一字一句そのまま報告してください。",
    "接続確認です。call_tool から list_directory を呼び出し、{dir} 直下にあるファイル名を"
    "正確に転記して答えてください。",
    "動作確認を行います。call_tool 経由の list_directory で {dir} 直下を一覧し、"
    "見つかったファイル名をそのまま答えてください。",
)

_CHALLENGE_INSTRUCTION_TAIL = (
    "list_directory の呼び出しに成功した場合のみ、回答の最後の行に見つけたファイル名だけを"
    "一字一句そのまま出力してください。ツールが呼び出せない、接続確認が必要、エラーが"
    "起きた等、成功以外の場合はファイル名を絶対に出力しないでください。"
)

# Returned instead of raising when the challenge directory/file cannot be prepared (disk full,
# permission denied, path too long, ...). FALLBACK_CHALLENGE_TOKEN can never appear in a real
# reply, so a caller that sends FALLBACK_CHALLENGE_INSTRUCTION anyway and runs the result
# through verify_probe_reply() degrades to an ordinary (False, "error") probe instead of
# crashing the probe loop.
FALLBACK_CHALLENGE_TOKEN = "PROBE_CHALLENGE_UNAVAILABLE"
FALLBACK_CHALLENGE_INSTRUCTION = (
    "[probe challenge unavailable: could not prepare the challenge file on disk. Do not call "
    "any tool and do not reply with anything for this message.]"
)


#: Where an inbound sighting is stamped. Its own file, not tool_probe.json: this is written
#: from the SERVER process on the tool-call hot path, while tool_probe.json is written by the
#: bridge, and two processes rewriting one file would race on the atomic replace.
_INBOUND_PATH = Path(__file__).resolve().parent.parent / ".fleet" / "probe_inbound.json"


def note_inbound(tool_name: str, arguments: Optional[dict] = None,
                 ts: Optional[float] = None, path: Optional[str] = None) -> bool:
    """Stamp the moment a probe's OWN tool call arrived at this server. Returns whether it did.

    THE FAILURE THIS ANSWERS WAS INVISIBLE BECAUSE IT DIED UPSTREAM OF US. When the connector's
    consent lapses, the call never reaches this process, so no counter here ever moves and every
    dot stays green. The success, though, is entirely visible here: the probe asks the agent to
    list ONE directory whose name only this server knows. So the arrival is the signal, and its
    ABSENCE during a probe window is the alarm -- and it says the same thing whether the turn
    went over a page or a socket, which reply-text parsing cannot.

    Called on the gateway's dispatch path, so it does the cheap test first and writes nothing
    unless the call is actually ours. Never raises: this must not be able to fail a tool call.

    WHAT IT CAN STILL MISTAKE. The marker is a directory name, so a call that merely MENTIONS
    that name -- a search for it, a listing of the parent -- stamps an arrival that no probe
    made. The stamp is only read inside a probe window, which bounds the damage to "a probe
    that was about to be reported as blocked is reported as fine", and that is a health signal
    saying the comfortable thing. Narrowing it needs a marker the agent cannot be asked to type
    by accident; recording the limit here until then.
    """
    try:
        # BOUNDED. This runs on every tool call, and str(arguments) on a write is the whole
        # payload -- stringified once and lowercased again, megabytes at a time, to look for a
        # short marker. A probe's own call carries its path near the front; nothing else needs
        # to be read to recognise one.
        target = str(_CHALLENGE_DIR).lower()
        raw = path if path is not None else (arguments or {})
        hay = str(raw)[:_INBOUND_SCAN_CHARS].lower()
        if target not in hay and "probe_challenge" not in hay:
            return False
        stamp = {"ts": float(ts if ts is not None else time.time()),
                 "tool": str(tool_name or "")[:64]}
        _INBOUND_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(_INBOUND_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(stamp, fh, ensure_ascii=False)
        os.replace(tmp, str(_INBOUND_PATH))
        return True
    except Exception:
        return False


def last_inbound_ts() -> float:
    """When a probe's tool call last reached this server, or 0.0 if never. Never raises.

    0.0 rather than None so a caller comparing against a window start cannot accidentally
    treat "never seen" as "seen just now" through a None comparison.
    """
    try:
        with open(str(_INBOUND_PATH), encoding="utf-8") as fh:
            return float((json.load(fh) or {}).get("ts") or 0.0)
    except Exception:
        return 0.0


def new_probe_challenge(base_dir: Optional[str] = None) -> Tuple[str, str]:
    """Create ONE fresh, unguessable probe challenge and return (instruction_text,
    expected_token).

    Resets `base_dir` (default: .fleet/probe_challenge/ next to this repo) so it contains
    EXACTLY one file, named "probe_<12 hex chars>.txt", whose 12-hex-char token is fresh
    (secrets.token_hex, 48 bits) and has never been sent in any earlier probe. The returned
    instruction asks the agent to call_tool -> list_directory that directory and report the
    file name it finds -- the instruction text itself does NOT contain the token, so the only
    way to answer correctly is to actually make that call this turn; an old answer memorized
    from a previous probe cannot satisfy a fresh one.

    Never raises: any filesystem error (disk full, permissions, path issues, concurrent access,
    ...) is swallowed and (FALLBACK_CHALLENGE_INSTRUCTION, FALLBACK_CHALLENGE_TOKEN) is returned
    instead, so the caller still has something to send, and verify_probe_reply() against that
    pair can only ever resolve to a failed probe, never a crash.
    """
    try:
        directory = Path(base_dir) if base_dir is not None else _CHALLENGE_DIR
        with _LOCK:
            directory.mkdir(parents=True, exist_ok=True)
            for entry in list(directory.iterdir()):
                try:
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink()
                except Exception:
                    pass
            token = secrets.token_hex(6)  # 12 hex chars, 48 bits -- unguessable, never repeats
            file_name = "probe_%s.txt" % token
            (directory / file_name).write_text(token, encoding="utf-8")
        dir_str = str(directory.resolve()).replace("\\", "/")
        # `run_id` is an independent random breadcrumb, NOT the answer -- it only guarantees the
        # instruction TEXT is never byte-identical across probes (mirroring
        # next_probe_instruction's sequence-number guarantee above), without giving away
        # `token`, which the agent must discover via the actual tool call.
        run_id = secrets.token_hex(4)
        head = _CHALLENGE_INSTRUCTION_VARIANTS[
            secrets.randbelow(len(_CHALLENGE_INSTRUCTION_VARIANTS))
        ]
        instruction = (
            head.format(dir=dir_str) + "\n" + _CHALLENGE_INSTRUCTION_TAIL +
            "\n（診断ID: " + run_id + "）"
        )
        return instruction, token
    except Exception:
        return FALLBACK_CHALLENGE_INSTRUCTION, FALLBACK_CHALLENGE_TOKEN


def verify_probe_reply(reply_text: Optional[str], expected_token: str,
                        agent_loaded: bool) -> Tuple[bool, str]:
    """Mirrors classify_probe_reply()'s exact contract and branch order (same `kind`
    vocabulary, same precedence), except step 4 requires the reply to contain the caller-
    supplied `expected_token` -- the token new_probe_challenge() just wrote to disk -- instead
    of the static PROBE_OK_TOKEN marker. This is what makes a green probe PROVE the tool
    round-trip happened THIS run: the model cannot satisfy it from memory (there is nothing
    fixed to remember), and a reply carrying a STALE token from an earlier challenge is
    rejected exactly like a reply with no token at all (see tools/test_tool_probe.py's
    regression test for why that distinction is the one that matters here).

      1. not agent_loaded                       -> (False, "agent_unreachable")
      2. reply has a CONSENT marker              -> (False, "consent_card")
      3. reply has a NO-CONNECTOR/canned marker  -> (False, "canned_fallback")
      4. reply contains expected_token           -> (True, "answer")
      5. otherwise                               -> (False, "error")
    """
    text = reply_text or ""
    if not agent_loaded:
        return False, "agent_unreachable"
    if any(m in text for m in CONSENT_MARKERS):
        return False, "consent_card"
    if any(m in text for m in NO_CONNECTOR_MARKERS):
        return False, "canned_fallback"
    if expected_token and expected_token in text:
        return True, "answer"
    return False, "error"


def record_probe(ok: bool, kind: str, detail: str = "", ts: Optional[float] = None,
                 alive: Optional[bool] = None, inbound: Optional[bool] = None) -> None:
    """Record the outcome of one tool-call self-probe and best-effort persist it to
    .fleet/tool_probe.json (atomic tmp+os.replace, utf-8, ensure_ascii=False -- same pattern
    as tools/auth_stats.write_snapshot). Never raises.

    `ts` defaults to time.time() but accepts a caller-supplied value so callers (and tests)
    can be deterministic instead of depending on wallclock at call time.

    `alive` is whether text actually came back on this turn. It has to be passed in because
    the caller is the only place that still holds the reply: "error" is the catch-all branch
    of verify_probe_reply, so it covers BOTH "answered, but not with our challenge" and "came
    back empty", and those two must not be read the same way. Inferring liveness from `kind`
    alone got exactly that wrong. None means the caller had no reply to judge (the transitional
    "starting"/"checking" records), and readers must treat it as no evidence."""
    try:
        now = time.time() if ts is None else ts
        payload = {"ts": now, "ok": bool(ok), "kind": kind, "detail": detail or ""}
        if alive is not None:
            payload["alive"] = bool(alive)
        # WHETHER THE PROBE'S OWN TOOL CALL REACHED THIS SERVER, stamped by the gateway. Kept
        # separate from `alive`: text can come back from a model that never called anything,
        # and a call can arrive from a turn whose reply is then unusable. The pair is what
        # separates a connector-path failure from a reply failure, and it reads the same over
        # a page or a socket -- which no amount of reply parsing does.
        if inbound is not None:
            payload["inbound"] = bool(inbound)
        with _LOCK:
            _PROBE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(_PROBE_FILE) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, str(_PROBE_FILE))
    except Exception:
        pass


# ---------------------------------------------------------------------------------------
# Append-only FAILURE evidence journal.
#
# Incident this closes: both probe callers persist only ok/kind/detail to tool_probe.json,
# and `detail` is the reply TRUNCATED to 200 chars (record_probe's contract below is left
# unchanged on purpose -- /health and the cockpit read that exact shape). For a FAILED probe
# that truncation destroys the one thing needed to tell failures apart after the fact: a
# repetition-refusal, a genuinely broken tool path, and "answered correctly but with the
# token from the PREVIOUS challenge" all collapse into kind="error" and look identical once
# the reply is cut off. A real production example: the reply was "...probe_bcac6dc36c5a.txt"
# while the current challenge's token was "e3fb184aa028" -- correctly rejected by
# verify_probe_reply, but with the 200-char detail alone there is no way to tell that apart
# from a reply that never mentioned a probe token at all.
#
# journal_probe_failure() is additive: it does not change record_probe()/tool_probe.json in
# any way, is only ever called ALONGSIDE the existing record_probe() call (never instead of
# it), and only ever writes for a FAILED probe (ok=False) -- successes are not journalled, so
# the common (healthy) path never grows this file.
# ---------------------------------------------------------------------------------------

PROBE_FAILURE_JOURNAL = Path(__file__).resolve().parent.parent / ".fleet" / "probe_failures.jsonl"

# Matches the exact file-name shape new_probe_challenge() writes ("probe_<12 hex chars>.txt"),
# so a reply can be checked for "did it mention ANY probe-challenge-shaped token" independent
# of whether that token happens to be the one THIS probe expected.
_PROBE_TOKEN_RE = re.compile(r"probe_[0-9a-f]{12}\.txt")

# Cap chosen to bound the file two ways at once, because this journal's whole point is to keep
# the FULL untruncated reply (unlike tool_probe.json's 200-char detail), so a single record's
# size is not fixed the way a normal log line's is:
#   - PROBE_FAILURE_JOURNAL_MAX_RECORDS: recent failure history is what is actionable here --
#     500 records is generous for "what went wrong recently" without keeping ancient entries.
#   - PROBE_FAILURE_JOURNAL_MAX_BYTES: a safety net independent of record count, in case a
#     handful of unusually large replies would otherwise blow past a reasonable file size well
#     before 500 records accumulate. 5 MB is small next to the 35 MB unrotated log this repo
#     already hit once, while still comfortably holding hundreds of ordinary-sized replies.
# Whichever bound is hit first wins; the OLDEST records are dropped and the NEWEST are always
# kept, since a post-incident read wants what just happened, not what happened weeks ago.
PROBE_FAILURE_JOURNAL_MAX_RECORDS = 500
PROBE_FAILURE_JOURNAL_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB

_JOURNAL_LOCK = threading.Lock()


def _find_probe_tokens(text: str) -> List[str]:
    """Pure helper: every probe_<hex>.txt-shaped token found in `text`, de-duplicated but
    order-preserved. Exposed separately from journal_probe_failure so the "does the reply
    contain a token at all" question is independently unit-testable without any file I/O."""
    seen = []
    for m in _PROBE_TOKEN_RE.findall(text or ""):
        if m not in seen:
            seen.append(m)
    return seen


def journal_probe_failure(ok: bool, kind: str, reply: Optional[str],
                           expected_token: Optional[str] = None,
                           ts: Optional[float] = None) -> None:
    """Append one evidence record for a FAILED probe to PROBE_FAILURE_JOURNAL (JSON Lines,
    newest entry last). No-ops immediately (no I/O, no lock) when `ok` is True -- a caller may
    call this unconditionally on every probe outcome without needing its own success/failure
    gate, mirroring record_probe()'s (ok, kind, detail) signature so the two calls stay easy to
    keep side by side at each call site.

    Each record carries, in addition to timestamp/kind:
      - "reply": the FULL, untruncated reply text (never cut to 200 chars like record_probe's
        `detail`) -- this is the entire point of the journal.
      - "expected_token": the challenge token this probe turn was checked against, or None if
        the caller had none (e.g. edge_reconnect's --probe override path, or the fallback
        challenge pair).
      - "found_probe_tokens": every probe_<hex>.txt-shaped token actually present in the reply
        (see _find_probe_tokens), independent of whether it matches expected_token.
      - "has_probe_token": bool(found_probe_tokens) -- the field that alone distinguishes "the
        reply carried a STALE token from an earlier challenge" (found_probe_tokens non-empty,
        none of them equal to expected_token) from "the reply carried no token at all"
        (found_probe_tokens empty), without a human needing to eyeball the full reply text.

    Best-effort like record_probe(): any failure (permissions, full disk, unwritable path,
    concurrent access, ...) is swallowed and this never raises, so a journalling hiccup can
    never break a probe or a request.
    """
    if ok:
        return
    try:
        now = time.time() if ts is None else ts
        text = reply or ""
        record = {
            "ts": now,
            "kind": kind,
            "reply": text,
            "expected_token": expected_token or None,
            "found_probe_tokens": _find_probe_tokens(text),
        }
        record["has_probe_token"] = bool(record["found_probe_tokens"])
        line = json.dumps(record, ensure_ascii=False)
        with _JOURNAL_LOCK:
            PROBE_FAILURE_JOURNAL.parent.mkdir(parents=True, exist_ok=True)
            lines: List[str] = []
            if PROBE_FAILURE_JOURNAL.exists():
                try:
                    with open(PROBE_FAILURE_JOURNAL, "r", encoding="utf-8") as f:
                        lines = [ln.rstrip("\n") for ln in f if ln.strip()]
                except Exception:
                    lines = []
            lines.append(line)
            # Record-count cap first (cheap, and the common case that actually bites).
            if len(lines) > PROBE_FAILURE_JOURNAL_MAX_RECORDS:
                lines = lines[-PROBE_FAILURE_JOURNAL_MAX_RECORDS:]

            def _total_bytes(ls: List[str]) -> int:
                return sum(len(ln.encode("utf-8")) + 1 for ln in ls)

            # Byte-size cap second, in case a run of oversized replies blows past it while
            # still under the record-count cap. Always keep at least the newest record.
            while len(lines) > 1 and _total_bytes(lines) > PROBE_FAILURE_JOURNAL_MAX_BYTES:
                lines.pop(0)
            tmp = str(PROBE_FAILURE_JOURNAL) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
            os.replace(tmp, str(PROBE_FAILURE_JOURNAL))
    except Exception:
        pass


def get_summary(now: Optional[float] = None) -> dict:
    """Read the last-recorded probe outcome from .fleet/tool_probe.json and return
    {"tool_ok": bool|None, "tool_kind": str|None, "tool_ts": float|None,
    "tool_age_s": float|None, "tool_alive": bool|None} -- the shape /health's
    payload.update(...) mirrors from tools.auth_stats.get_summary().

    tool_inbound is whether the probe's own tool call reached this server (the gateway stamps
    it); with tool_alive it separates a connector-path failure from a reply failure.
    tool_alive is whether text came back on that turn, recorded by the caller that still had
    the reply. Records written before this field existed simply lack it, so it reads as None
    ("no evidence") rather than as a negative.

    Tolerates a missing or corrupt file (never raises): returns the all-None shape in
    either case, exactly as tools.auth_stats.get_summary() zeroes out on failure. `now`
    is a caller-supplied reference time for computing tool_age_s, defaulting to
    time.time() -- deterministic for tests, real wallclock in production (e.g. /health)."""
    empty = {"tool_ok": None, "tool_kind": None, "tool_ts": None, "tool_age_s": None,
             "tool_alive": None, "tool_inbound": None}
    try:
        with open(_PROBE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        ts = raw.get("ts")
        if not isinstance(ts, (int, float)):
            return dict(empty)
        ref = time.time() if now is None else now
        return {
            "tool_ok": raw.get("ok"),
            "tool_kind": raw.get("kind"),
            "tool_ts": ts,
            "tool_age_s": max(0.0, ref - ts),
            "tool_alive": raw.get("alive"),
            "tool_inbound": raw.get("inbound"),
        }
    except Exception:
        return dict(empty)

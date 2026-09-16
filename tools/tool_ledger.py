# -*- coding: utf-8 -*-
"""What a tool was asked to do and what came back, written down where it can be cited later.

THE GAP THIS FILLS, MEASURED. The session store holds 122 MB and 15,605 turns, and NONE of it
records a tool call. 26.8 MB of user text, 4.8 MB of assistant text, and nothing else: no tool
name, no arguments, no result, no error. Everything this system knows about its own work is the
prose the two sides wrote about it.

WHAT THAT COSTS, also measured. Workers claim DONE with precision 0.718 -- 11 of 39 claims wrong
on a 40-instance slice. The refuter that exists to catch those reads the worker's ACCOUNT of
what it did, because there is nothing else to read, so it judges hearsay. An experiment on
whether skills were being consulted had to add its own side-channel for the same reason: the
transcript could not say whether a tool had been called. Every question of the form "did it
actually do that" is currently unanswerable.

ASSISTANT PROSE IS NOT EVIDENCE. That is the whole design rule. A claim and the record of the
act have to come from different places, or a worker that is mistaken -- or lying -- writes both.

APPEND-ONLY JSONL, ON PURPOSE. Two records per call, linked by one id: the CALL, written before
the tool runs, and the OUTCOME, written after. Not one record written at the end, because a call
that never returns is exactly the case worth seeing: a crash, a timeout, a killed process leave
a call with no outcome, and that orphan is a finding rather than a gap. No database, no UI, no
replay engine -- those can be built on this, and cannot be recovered without it.

WHAT IS DELIBERATELY NOT STORED: chain of thought (there is none to store here, and it would be
the wrong thing to keep), and unbounded results. A result is truncated and hashed, so the record
stays small and a later claim about what came back can still be checked against it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER_PATH = os.path.join(_REPO, ".fleet", "tool_events.jsonl")

SCHEMA_VERSION = 1

#: How much of an argument blob or a result is kept inline. Enough to recognise what happened,
#: bounded so a ledger cannot become the thing that fills the disk -- which on this machine is
#: the binding constraint, and has already stopped a benchmark run once.
MAX_INLINE = 2000

#: Argument names whose VALUE never goes in, whatever tool they belong to. The ledger is a file
#: on disk that outlives the session; a password written once is written forever.
SECRET_ARGS = {"password", "passwd", "secret", "token", "unlock_token", "api_key", "apikey",
               "authorization", "auth", "credential", "credentials", "private_key"}

_LOCK = threading.Lock()


def _repo_path():
    """Resolved at call time so a test (and the repo-wide isolation fixture) can move it."""
    return LEDGER_PATH


def new_call_id() -> str:
    return uuid.uuid4().hex[:16]


def _digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]


def _bounded(value):
    """A value small enough to store, with the full thing still identifiable by its digest."""
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                               default=str)
    except Exception:
        text = str(value)
    full = len(text)
    return {"text": text[:MAX_INLINE], "len": full, "sha16": _digest(text),
            "truncated": full > MAX_INLINE}


#: The bracket prefix tools/security.py writes when it refuses a call, and the length above
#: which a response is prose ABOUT a refusal rather than the refusal itself. Both are copied
#: rather than imported: security.py is frozen, relay_fleet.py (which owns the same two-part
#: rule in _looks_locked) is far too heavy for a module main.py imports before anything else,
#: and this file stays stdlib-only on purpose. A test asserts the literals still match.
_LOCK_REFUSAL_PREFIX = "[locked"
_LOCK_REFUSAL_MAX_CHARS = 400

#: THE SHAPE EVERY TOOL IN THIS REPO USES TO REPORT ITS OWN FAILURE. They do not raise -- an
#: MCP tool returns text -- so `[read_file error: FileNotFoundError: ...]` reaches the ledger
#: through the success path and was filed as a success for 37k rows. Two parts, the same rule
#: looks_refused uses: a distinctive marker AND dominance, so a file whose contents happen to
#: quote an error is not filed as one.
#:
#: The bound is 600 rather than 400 because it was measured, not chosen: over the whole ledger
#: the longest genuine error report of this shape is 463 characters and only 5 of 1,650 pass
#: 400. A bound under the real maximum would have left the largest failures reading green,
#: which is the half of the range where being wrong matters most.
#: Up to two name tokens, because the gateway writes "[call_tool git_status error: ...]" and
#: web_fetch writes "[web_fetch HTTP error: ...]" -- 139 rows of the latter alone. A
#: single-token rule would have left every gateway-level failure reading green, and the
#: gateway is the one place every dispatched call passes through. Zero tokens is allowed too:
#: code_exec writes a bare "[timeout: exceeded 30 seconds]".
#:
#: The keyword must be followed immediately by ":" or "]" so that a document beginning
#: "[error on page 3: ..." is not a report about itself.
_NAME = r"(?:[A-Za-z0-9_]+ ){0,2}"
_FAILURE_SHAPE = re.compile(r"^\[" + _NAME + r"(?:error|failed|refused)[:\]]"
                            # "[pwsh_exec timeout after 30s]" as well as "[timeout: ...]".
                            # Zero rows in the ledger today, but four tools write the spaced
                            # form and a shape that exists in the source will eventually be
                            # produced by it.
                            r"|^\[" + _NAME + r"timeout(?:[:\]]| after )")
#: AND THE OTHER ORDER. The gateway writes "[call_tool: refused. ...]" and fleet_intake
#: writes "[fleet_submit: refused -- ...]": the name, then the colon, THEN the word. Seen in
#: production on 2026-09-16 in a run that was otherwise reading correctly. 36 rows in the
#: whole ledger, 0.097%, and 34 of them are call_tool.* which the health reader already
#: excludes as discovery chatter -- so this is completeness, not a fix for a live symptom,
#: and it is recorded as such rather than as a save.
#:
#: Safe against the "[<name>: <prose>]" family that means a lookup found nothing, because
#: that prose never starts with one of these four words: "[memory_read: no topic found]",
#: "[which: rg not found on PATH]", "[sqlite_schema: no table named ...]".
_FAILURE_AFTER_COLON = re.compile(r"^\[[A-Za-z0-9_.]+:\s*(?:error|failed|timeout|refused)\b",
                                  re.I)
_UNAVAILABLE_SHAPE = re.compile(r"^\[" + _NAME + r"(?:unavailable|skipped|aborted)[:\]]")
_REPORT_MAX_CHARS = 600

#: COUNTED, NOT GUESSED, over the whole ledger (37,018 successes carrying a string):
#:   error 1,789 | timeout 213 | failed 37 | refused 2   -- all filed as successes
#:   [stdout] / [stderr]  8,216                          -- successful runs, must stay green
#:   skipped 20, aborted 3                               -- DELIBERATELY NOT HERE
#: skipped and aborted are NEITHER, and the first draft of this rule called them successes.
#: "[replace skipped: old text was not found]" is a correct answer to a caller's mistake --
#: but the identical word comes out of an environment fault: a missing executable, an
#: unavailable mount, an absent credential. The word cannot tell those apart, and the reason
#: text is free-form, so nothing here can either. Counting them as successes meant every one
#: of those refreshed green AND reset the consecutive-failure streak, so failure/failure/skip
#: repeating forever would never have reached red.
#:
#: They join the unavailable case instead: not evidence. A path that only ever skips reports
#: "no evidence", which is exactly what a run of skips supports -- nothing in it says whether
#: the tool can do its job. Raised by gpt-6-astra against the first draft, and it is right.


def _is_report(result, shape) -> bool:
    try:
        if not isinstance(result, str):
            return False
        text = result.strip()
        return bool(shape.match(text)) and len(text) < _REPORT_MAX_CHARS
    except Exception:
        return False


def looks_failed(result) -> bool:
    """True iff `result` IS a tool reporting its own failure, rather than content quoting one."""
    return _is_report(result, _FAILURE_SHAPE) or _is_report(result, _FAILURE_AFTER_COLON)


def looks_unavailable(result) -> bool:
    """True iff nothing happened that says anything about whether the tool path works.

    Covers two cases that read alike to a health indicator: the machine was not in a state to
    run the tool, and the tool declined because a precondition was not met.

    A THIRD STATE, AND THE REASON THIS IS NOT JUST ANOTHER FAILURE. When the workstation is
    locked, no screen can be captured and no click can be delivered -- and the tools are
    fine. Filing that as a failure lights a health indicator red and tells a reader the
    system is broken when what happened is that a person walked away. Filing it as a success
    is worse. It is neither, and the only honest rendering is "no evidence", which
    fleet_tool_health already knows how to show.
    """
    return _is_report(result, _UNAVAILABLE_SHAPE)


def looks_refused(result) -> bool:
    """True iff `result` IS a lock refusal, rather than content that merely contains one.

    Same two-part rule as relay/relay_fleet.py::_looks_locked -- distinctive bracket marker
    plus dominance -- but tightened from `in` to `startswith`, because the ledger sees the raw
    return value, where a genuine refusal is the whole of it. read_file on a file that quotes a
    refusal (this repo has several) satisfies a substring test and must not be filed as one.
    """
    try:
        if not isinstance(result, str):
            return False
        text = result.strip()
        return text.startswith(_LOCK_REFUSAL_PREFIX) and len(text) < _LOCK_REFUSAL_MAX_CHARS
    except Exception:
        return False


def row_unavailable(row) -> bool:
    """True iff this outcome says the machine was not in a state to run the tool.

    Separate from row_ok deliberately: row_ok answers "did this call do its job" and the
    answer here is no, while this answers "does this call count as evidence about the tool"
    and the answer there is also no. A reader that only has the first cannot tell an
    unattended machine from a broken one -- which is the whole defect.
    """
    try:
        result = row.get("result")
        if isinstance(result, dict):
            result = result.get("text")
        return looks_unavailable(result) or bool(row.get("unavailable"))
    except Exception:
        return False


def row_ok(row) -> bool:
    """The verdict for a STORED outcome row, with refusals corrected on read.

    record_outcome has filed refusals as failures since the commit that added it, but the rows
    written before that say ok=True with the refusal text sitting in `result` -- 320 of 11,686
    outcome rows when this was measured. The ledger is append-only and its worth is that it was
    never edited, so history is corrected here, at read time, instead.

    Read verdicts through this rather than row["ok"]: it also unwraps `result`, which is stored
    as {"text": ...} by _bounded, and reaching past that wrapper by hand is the step every
    ad-hoc query gets wrong. Truncation cannot fake a refusal -- _bounded cuts at MAX_INLINE
    (2000), well above the 400-char dominance bound, so a clipped long result still reads long.
    """
    try:
        ok = bool(row.get("ok"))
        if not ok:
            return ok
        # DELIBERATELY NOT GATED ON row["event"] == "outcome". It was, and that made the
        # correction depend on a key the caller might not have kept: relay/evidence_manifest
        # receives outcomes through for_task, and its own tests build them as bare
        # {"ok": ..., "ts": ...}. A guard that silently returns the uncorrected value for a
        # reshaped row is the seam both sides pass their tests across while nothing is
        # actually checked. A call row cannot false-positive here anyway -- it carries `args`,
        # not `result` -- so the guard bought nothing and cost the seam.
        result = row.get("result")
        if isinstance(result, dict):
            result = result.get("text")
        # All three corrections, not just the refusal one. The rows this now moves were
        # written over months by tools that report failure by returning it, and the ledger is
        # append-only, so history is corrected on read exactly as refusals already were.
        return not (looks_refused(result) or looks_failed(result)
                    or looks_unavailable(result))
    except Exception:
        # A reader that raises on one malformed row stops being used, same as read() above.
        try:
            return bool(row.get("ok"))
        except Exception:
            return False


def redact_args(arguments, _depth=0) -> dict:
    """Arguments with secret VALUES removed and the rest bounded.

    The NAMES stay. "there was a password argument" is evidence; the password is not.

    RECURSES, because this checked only the TOP level and every gated call arrives wrapped. A
    call through the gateway is logged as {"name": "unlock", "arguments": {...}}: the real
    arguments sit one level down, "arguments" is not a secret name, and the password inside it
    was written out verbatim. That was not a missing entry in SECRET_ARGS -- it was every gated
    tool bypassing this function. Found as a live plaintext MCP_UNLOCK_PASSWORD in the ledger.
    """
    if not isinstance(arguments, dict):
        return {"_": _bounded(arguments)}
    out = {}
    for key, value in arguments.items():
        if str(key).strip().lower() in SECRET_ARGS:
            out[key] = {"redacted": True, "sha16": _digest(str(value))}
        elif isinstance(value, dict) and _depth < 4:
            out[key] = redact_args(value, _depth + 1)
        else:
            out[key] = _bounded(value)
    return out


#: When the ledger is moved aside. Its neighbour faulthandler.log has rotated at 8 MB since
#: the day it was added; this file reached 64 MB without anyone choosing that. The cap is
#: larger because the ledger is read back -- fleet_tool_health tails it, and measurements are
#: taken over it -- so a generous single file is worth more here than a small one.
LEDGER_MAX_BYTES = 128 * 1024 * 1024


def _rotate_if_large(path: str) -> None:
    """Move the ledger aside once, keeping one generation. Never raises.

    ROTATION IS NOT EDITING. The rows are moved intact; none is rewritten, which is the
    property this file's worth rests on. os.replace is atomic on Windows and POSIX alike, so
    a reader holding the old path keeps reading a complete file rather than a truncated one.
    """
    try:
        if os.path.getsize(path) < LEDGER_MAX_BYTES:
            return
    except OSError:
        return          # no file yet, or unreadable: nothing to rotate
    try:
        os.replace(path, path + ".1")
    except OSError:
        pass            # a locked file is a reason to keep appending, not to lose the row


def _append(row: dict) -> None:
    """Best effort, never raises. A ledger that can fail a tool call is worse than no ledger."""
    try:
        path = _repo_path()
        line = json.dumps(row, ensure_ascii=False)
        # THE SINGLE EXIT. redact_args above keeps the row readable, but it can only
        # catch NAMES it knows, and the value can arrive anywhere: nested inside a
        # gateway wrapper, spliced into a shell command, or quoted back in a tool's own
        # result. This matches the values we actually HOLD, at the one point where bytes
        # leave the process, so a secret has to pass here whichever field carried it.
        # Regexes for token SHAPES are not a substitute -- they miss the passwords that
        # do not look like tokens, which is exactly what leaked.
        try:
            from tools.secret_store import redact_secrets
            line = redact_secrets(line)
        except Exception:
            pass
        with _LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _rotate_if_large(path)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def session_fingerprint() -> str:
    """A stable, non-reversible label for the MCP session this call arrived on, or "".

    HERE, NOT IN tools/security.py. The first draft put it beside the unlock ContextVar, which
    is where it conceptually belongs -- and that file is in FROZEN_MANIFEST and in
    DELEGATION_EXCLUDED as "the unlock boundary". Adding a diagnostic there would have required
    re-signing the constitution to land an observation that changes no authorisation at all.
    The operator approved the measurement, not a re-signing of the security boundary, and those
    are not the same permission. It lives with its only consumer instead.

    OBSERVATION ONLY. Nothing reads it. It exists to settle one measured question before any
    authorisation change is designed on top of it: does Copilot Studio keep ONE Mcp-Session-Id
    across a conversation, one per turn, or none? Nobody here knows. FastMCP issues a session id
    on streamable-HTTP; whether this client echoes it stably has never been looked at.

    IT DECIDES BETWEEN TWO DESIGNS. The unlock token is a session credential the model must
    carry in its own context, and it demonstrably loses it: 318 refusals in four days, ~22% of
    which end with the agent quietly falling back to read-only tools and reporting as though the
    work were done. If the session id is stable per conversation the credential can move to the
    transport, where the model never touches it. If it rotates per turn there is no per-call
    second factor on this transport at all, and the answer has to be something else. Guessing
    wrong costs days.

    Hashed, not stored raw: this file is appended to on every tool call, and a live session
    identifier is not a thing to leave in a log. The hash answers "same session as before?",
    which is the whole question.
    """
    sid = ""
    try:
        from fastmcp.server.dependencies import get_context

        sid = str(getattr(get_context(), "session_id", "") or "")
    except Exception:
        sid = ""
    if not sid:
        try:
            from fastmcp.server.dependencies import get_http_request

            sid = str(get_http_request().headers.get("mcp-session-id") or "").strip()
        except Exception:
            sid = ""
    if not sid:
        return ""
    return hashlib.sha256(sid.encode("utf-8", "replace")).hexdigest()[:16]


def record_call(tool: str, arguments=None, *, task: str = "", worker: str = "",
                turn=None, call_id: str = "", ts: float = None) -> str:
    """Write the CALL record, BEFORE the tool runs. Returns the id to pass to record_outcome.

    Before, not after, so a call that never returns still leaves a trace. A ledger written only
    on success records the runs that did not need recording.
    """
    cid = call_id or new_call_id()
    # WHICH MCP SESSION THIS ARRIVED ON, hashed. Recorded here rather than at either caller
    # because there are two -- the call_tool gateway and register()'s wrapper for directly
    # registered tools -- and a measurement that only sees one of them is the mistake this
    # ledger spent the night fixing. Empty outside an HTTP request (tests, CLI, in-process
    # hooks), which is correct: there is no session to name.
    #
    # PURELY OBSERVATIONAL. Nothing reads it yet. It exists to settle whether Copilot Studio
    # keeps one session id across a conversation or mints one per turn, because the answer
    # decides whether the unlock credential can move off the model's context and onto the
    # transport. See tools.security.session_fingerprint.
    try:
        _sess = session_fingerprint()
    except Exception:
        _sess = ""
    row = {
        "schema": SCHEMA_VERSION,
        "event": "call",
        "id": cid,
        "ts": float(ts if ts is not None else time.time()),
        "tool": str(tool or "")[:120],
        "task": str(task or "")[:120],
        "worker": str(worker or "")[:64],
        "turn": turn,
        "args": redact_args(arguments),
    }
    if _sess:
        row["session"] = _sess
    _append(row)
    return cid


def record_outcome(call_id: str, *, ok: bool, result=None, error: str = "",
                   ts: float = None, duration_s: float = None) -> None:
    """Write the OUTCOME record for a call. Linked by id, never merged into the call record."""
    # A REFUSAL IS NOT A SUCCESS. Every call site passes ok=True whenever the tool returned
    # without raising, and a lock refusal returns normally -- so `write_file` denied for a
    # missing unlock token was filed ok=True with an empty error. Measured on 2026-09-06:
    # three such rows in one day, indistinguishable from writes that actually wrote.
    #
    # Nothing in the server branches on this field, which is exactly why it went unnoticed for
    # 23k rows. The ledger is not control flow; it is the record every later measurement is
    # taken OVER, and counting refusals as successes silently inflates any success or recovery
    # rate computed from it. Two such rates have already had to be retracted here.
    #
    # Corrected in one place rather than at each call site: the next call site would repeat it.
    # Only ever downgrades -- an explicit ok=False is never overridden -- and the reason stays
    # a fixed string, because the refusal text is already stored verbatim in `result` and the
    # error field is the one thing a reader scans in bulk.
    unavailable = False
    if ok and looks_refused(result):
        ok = False
        error = error or "refused (locked)"
    elif ok and looks_unavailable(result):
        # Recorded as not-ok AND flagged, because those are two different facts and a reader
        # that has only the first will call an unattended machine a broken one.
        ok, unavailable = False, True
        error = error or "unavailable (the machine was not in a state to run it)"
    elif ok and looks_failed(result):
        ok = False
        error = error or "returned its own error report"
    _append({
        "schema": SCHEMA_VERSION,
        "event": "outcome",
        "id": str(call_id or ""),
        "ts": float(ts if ts is not None else time.time()),
        "ok": bool(ok),
        "duration_s": (round(float(duration_s), 3) if duration_s is not None else None),
        "error": str(error or "")[:MAX_INLINE],
        # Written as a field as well as being inferable from the text, because a reader
        # scanning 39,000 rows in bulk reads fields, and row_unavailable accepts either.
        "unavailable": True if unavailable else None,
        "result": _bounded(result) if result is not None else None,
    })


def read(path: str = None):
    """Every record, in order. Malformed lines are skipped rather than raising -- a ledger that
    cannot be read because one line is torn is a ledger that stops being consulted."""
    rows = []
    try:
        with open(path or _repo_path(), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


def orphans(rows=None):
    """Calls with no outcome. THE POINT OF WRITING TWO RECORDS.

    A crash, a timeout, or a killed process leaves one of these. It is a finding -- the tool was
    entered and never came back -- and it is invisible to any scheme that writes a single record
    when a call completes.
    """
    rows = read() if rows is None else rows
    seen_outcome = {r.get("id") for r in rows if r.get("event") == "outcome"}
    return [r for r in rows
            if r.get("event") == "call" and r.get("id") not in seen_outcome]


def for_task(task: str, rows=None, root: str = ""):
    """Every call made under one task, with its outcome attached where there is one.

    This is what a verifier reads instead of the worker's account of itself.

    ATTRIBUTION BY PATH, BECAUSE THE `task` FIELD IS USUALLY EMPTY. The gateway records
    `task=_args.get("_task")` and nothing passes `_task` -- a fleet worker is a Copilot agent
    calling a tool; it does not know which benchmark instance it is. Measured on the first real
    batch: every call landed with an empty task, so this returned nothing and the whole
    assessment came back UNVERIFIABLE. Wiring a field no producer sets is the same mistake as
    inventing an API name.

    What IS present in every call is the path being worked on, and the benchmark already maps
    an instance to its worktree. So `root` matches a call by where it operated, which is a fact
    the record actually contains.
    """
    rows = read() if rows is None else rows
    outcomes = {r.get("id"): r for r in rows if r.get("event") == "outcome"}
    needle = (root or "").replace("\\", "/").rstrip("/").lower()
    unfiltered = not task and not needle
    out = []
    for r in rows:
        if r.get("event") != "call":
            continue
        # NO FILTER MEANS EVERYTHING; A FILTER THAT MATCHES NOTHING MEANS NOTHING. The two must
        # not be confused: silently returning every call to a caller who asked about one task
        # would attribute other instances' work to it, and that is worse than no attribution.
        if unfiltered or (task and r.get("task") == task) or (needle and _touches(r, needle)):
            out.append({"call": r, "outcome": outcomes.get(r.get("id"))})
    return out


def _touches(call: dict, needle: str) -> bool:
    """Whether a recorded call operated inside `needle`. Reads the bounded argument text that
    is already stored, so nothing extra has to be recorded for this to work."""
    args = (call or {}).get("args") or {}
    if not isinstance(args, dict):
        return False
    for value in args.values():
        text = value.get("text") if isinstance(value, dict) else value
        if isinstance(text, str) and needle in text.replace("\\", "/").lower():
            return True
    return False

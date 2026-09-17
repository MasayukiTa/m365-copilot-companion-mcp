"""Server-side record of the most recent per-IP lock refusal.

Why this exists
---------------
The relay auto-unlocks by watching the AGENT's reply for the server's literal lock
error ("[locked client IP: ...] ..."). That only works while the agent pastes the
tool error back verbatim. It frequently does not: the operator discipline injected
into every turn tells it to emit "淡々と事実とタスク結果のみ", so it summarises --
"unlock パスワード欠如で確定。STUCK: unlock パスワード未提供。" -- and the marker
never appears. Detection then misses, the generic retry nudge runs instead of the
unlock injection, and the run STUCKs asking a human for a password the machine
already has in .env.

Tightening the phrase list is what created this: the markers were narrowed after a
security-review worker's prose about tools/security.py false-tripped a looser rule.
Narrow enough to avoid prose, and it also misses paraphrased reality.

So stop inferring a server fact from agent prose. Whether a call was refused for
lock is known exactly at the point of refusal; record it there and let readers ask.
Same shape as tools/tool_probe.py: stdlib only, import-safe, atomic write, every
failure swallowed so a disk hiccup can never break request handling.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

_STATE_FILE = Path(__file__).resolve().parent.parent / ".fleet" / "lock_state.json"

#: EVERY refusal, not just the latest. The slot above holds one record, which was enough while
#: one worker ran at a time and became the reason a real incident could not be reconstructed:
#: with six workers, some caller's refusal marked every other worker's reply as locked for the
#: freshness window, and the file could not say whose it was -- including the records written
#: with an empty client_ip, whose author was unknown. Refusals are rare, so this grows slowly;
#: .fleet is gitignored, so it publishes nothing.
_LOG_FILE = Path(__file__).resolve().parent.parent / ".fleet" / "lock_refusals.jsonl"
_LOCK = threading.Lock()

# A reader asking "was a call just refused for lock?" only cares about the recent
# past; an hour-old refusal says nothing about the turn being judged now.
DEFAULT_FRESH_SEC = 180.0


def _caller_site() -> str:
    """Which refusal site wrote this, as file:line:function.

    THE TOOL NAME IS NOT AVAILABLE HERE and the honest thing is to say so rather than invent a
    field. The call sites live in a frozen, delegation-excluded module, so they cannot be
    changed to pass one. The frame is what is on hand, and it answers the question that
    actually blocked the last investigation: which of the three refusal sites -- and therefore
    why some records carry an empty client_ip.
    """
    try:
        import sys as _sys
        f = _sys._getframe(2)
        return "%s:%d:%s" % (os.path.basename(f.f_code.co_filename), f.f_lineno,
                             f.f_code.co_name)
    except Exception:
        return ""


def _session() -> str:
    """The MCP session this refusal arrived on, or "" outside an HTTP request.

    WHY IT IS FETCHED HERE, AND NOT PASSED IN. The refusal sites live in tools/security.py,
    which is in FROZEN_MANIFEST as the unlock boundary -- adding a parameter there would make a
    diagnostic that changes no authorisation cost a re-signing of the baseline. tool_ledger's
    session_fingerprint() is in the request context already and answers the same question from
    here, which is the same reasoning that put session_fingerprint in tool_ledger rather than
    beside the unlock ContextVar in the first place.
    
    WHAT IT MAKES ANSWERABLE. This ledger records `client_ip` and, since 2026-09-09,
    `session_state` -- but never WHICH session, so a refusal could not be joined to the unlock
    that preceded it. Measured 2026-09-10 across three days: of 759 lock refusals, 595 happen
    before any successful unlock in the same session (the designed first-call refusal) and 164
    happen AFTER one, spread p50 461s / p90 1359s / max 2710s from that unlock. The max exceeds
    the 30-minute session TTL and is explained; the median is well inside it and is not. The
    leading hypothesis is that the forwarded IP changes under a stable session -- authorisation
    is recorded in state[ip]["sessions"], so an identity change loses it -- and this field is
    what would confirm or kill that, since the two are then in one row.

    OBSERVATION ONLY. Nothing branches on it.
    """
    try:
        from tools.tool_ledger import session_fingerprint
        return session_fingerprint()
    except Exception:
        return ""


def _append_log(payload: dict) -> None:
    """One line per refusal. Best effort; a log that cannot be written must not refuse a call."""
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8", newline=chr(10)) as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + chr(10))
    except Exception:
        pass


def record_locked(client_ip: str = "", detail: str = "", ts: Optional[float] = None,
                  presented_digest: str = "", tokens_held: Optional[int] = None,
                  session_state: str = "") -> None:
    """Note that require_unlocked() just refused a call. Never raises.

    `presented_digest`/`tokens_held`/`session_state` are OPTIONAL and separate from `detail`
    on purpose: `detail` truncates to 200 chars, and a diagnostic appended to the end of the
    fixed boilerplate sentence never survived that cut -- measured 2026-09-09, a token-state
    suffix landed past char 200 in every one of 5 refusals and was silently discarded. These
    fields cannot be pushed out by boilerplate length because they are never concatenated
    into it. `presented_digest` is a short sha256[:16] of whatever presented_token() held (or
    "" if nothing was presented) -- never the raw token -- so a later reader can tell "the
    caller never attached one" from "attached one that does not match anything currently
    held" without this file ever writing a credential to disk. `session_state` says why the
    2026-09-09 session-authorization fallback did not save this particular call (no session
    id available on the call, or one was available but not recognized/expired) -- added
    alongside it so a refusal that still occurs after that fix is legible from this ledger
    alone, the same reasoning presented_digest was added for.
    """
    payload = {
        "ts": float(ts if ts is not None else time.time()),
        "client_ip": str(client_ip or "")[:64],
        "detail": str(detail or "")[:200],
        "site": _caller_site(),
    }
    # record_locked's contract is "Never raises" -- a ledger that can fail a request is worse
    # than a ledger. _session() guards itself, and this guards against _session ITSELF being
    # the thing that breaks (a swapped implementation, an import that starts raising).
    try:
        sess = _session()
    except Exception:
        sess = ""
    if sess:
        payload["session"] = sess
    if presented_digest or tokens_held is not None:
        payload["presented_digest"] = str(presented_digest or "")[:16]
        payload["tokens_held"] = int(tokens_held) if tokens_held is not None else None
    if session_state:
        payload["session_state"] = str(session_state)[:64]
    _append_log(dict(payload, event="refused"))
    try:
        with _LOCK:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(_STATE_FILE.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                os.replace(tmp, _STATE_FILE)
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
    except Exception:
        pass


def read_state() -> dict:
    """Last recorded refusal, or {} when there is none / it is unreadable."""
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def locked_recently(within_sec: float = DEFAULT_FRESH_SEC,
                    now: Optional[float] = None) -> bool:
    """True iff a lock refusal was recorded within `within_sec`.

    `now` is injectable so callers and tests are not at the mercy of wallclock.
    """
    state = read_state()
    try:
        ts = float(state.get("ts") or 0.0)
    except (TypeError, ValueError):
        return False
    if ts <= 0.0:
        return False
    current = float(now if now is not None else time.time())
    return 0.0 <= (current - ts) <= float(within_sec)


def clear() -> None:
    """Forget the last refusal -- called after a successful unlock. Never raises."""
    try:
        with _LOCK:
            if _STATE_FILE.exists():
                _STATE_FILE.unlink()
    except Exception:
        pass


#: How far back from the end of the refusal log to read. Refusals are rare and every reader
#: scopes to its own turn, so a window is enough -- and it keeps the cost of reading independent
#: of how large the log has grown. Sized well above any plausible burst inside one turn.
_LOG_TAIL_BYTES = 65536


def matching_records(since: float, now: Optional[float] = None) -> list:
    """Every refusal recorded at or after `since` and still fresh. Oldest first.

    READS THE APPEND-ONLY LOG, NOT THE SLOT, because the slot holds exactly one record and a
    reader asking "which refusal was mine" against a one-deep slot gets whichever refusal
    landed last. That shadowing has produced three separate incidents already, all of them
    fixed on the symptom side: one worker's refusal reading as every worker's lock, a 533-char
    meeting summary classified as a lock, and a blank-ip refusal a remote caller could forge.
    The slot itself was never the evidence it was being used as.

    It also fixes the unmeasured direction of the same fault. A context-less refusal arriving
    after a genuine one overwrites the slot, and a reader that filters context-less refusals
    then concludes "not locked" while a real lock is standing.

    The slot stays: `read_state`, `clear` and the CLI still use it to show the last refusal.
    It is no longer asked to answer a question it cannot answer.
    """
    try:
        boundary = float(since)
    except (TypeError, ValueError):
        return []
    if boundary <= 0.0:
        return []
    current = float(now if now is not None else time.time())

    try:
        with open(_LOG_FILE, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _LOG_TAIL_BYTES))
            blob = fh.read()
    except FileNotFoundError:
        return []
    except Exception:
        return []

    # The window usually starts mid-record. Dropping the first line to compensate is worse
    # than useless: a torn line is not valid JSON and the parse below already rejects it, and
    # when the cut happens to land exactly on a line boundary that drop discards a real
    # refusal. Let the parse decide.
    lines = blob.decode("utf-8", "replace").split(chr(10))

    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            # Several processes append here, so the tail can hold a half-written line.
            continue
        if not isinstance(rec, dict):
            continue
        # The same file carries `classified_locked` rows written by the readers themselves.
        # Counting those would let a reader's own note read back as fresh evidence.
        if rec.get("event") != "refused":
            continue
        try:
            ts = float(rec.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts < boundary:
            continue
        if (current - ts) > DEFAULT_FRESH_SEC:
            continue
        out.append(rec)
    return out


def classifications(since: float, now: Optional[float] = None) -> list:
    """Every `classified_locked` note a reader wrote at or after `since`. Oldest first.

    THE OTHER HALF OF matching_records, and it exists so a REFUSAL CAN BE ASKED WHETHER
    ANYONE PICKED IT UP. A refusal that no reader classified is the measured failure behind
    the fallback button: 2026-09-15, twice in one day, a worker was refused for lock, no
    recovery fired, and the run carried on -- once producing a deliverable that claimed to
    have verified content it had never been able to read.

    Reusing the same scan deliberately: the tail-read, the torn-line tolerance and the
    freshness window were each bought with an incident, and a second copy of them is a second
    place for those lessons to rot.
    """
    rows = _scan(since, now)
    return [r for r in rows if r.get("event") == "classified_locked"]


def _scan(since: float, now: Optional[float] = None) -> list:
    """Every parseable record in the fresh window, whatever its event.

    matching_records and classifications both filter this. It is private because "every
    record" is not a question anyone should be asking: a caller that does not say which event
    it means is a caller that will one day count a reader's own note as evidence, which is the
    exact mistake matching_records carries a comment about.
    """
    try:
        boundary = float(since)
    except (TypeError, ValueError):
        return []
    if boundary <= 0.0:
        return []
    current = float(now if now is not None else time.time())
    try:
        with open(_LOG_FILE, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _LOG_TAIL_BYTES))
            blob = fh.read()
    except Exception:
        return []
    out = []
    for line in blob.decode("utf-8", "replace").split(chr(10)):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        try:
            ts = float(rec.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts < boundary or (current - ts) > DEFAULT_FRESH_SEC:
            continue
        out.append(rec)
    return out


def matching_record(since: float, now: Optional[float] = None) -> dict:
    """The most recent refusal `locked_since` would match, or {} when there is none.

    Kept for callers that only want to name one record (the CLI, diagnostics). A caller
    DECIDING whether it is locked wants `matching_records`: one record cannot tell it whether
    some other refusal in the same window was about it.
    """
    records = matching_records(since, now)
    return records[-1] if records else {}


def record_classification(branch: str, *, resp_len: int, since: float,
                          consumed: Optional[dict] = None,
                          attribution: Optional[dict] = None) -> None:
    """Note that a reader classified a reply as locked, and on what evidence. Never raises.

    `attribution` says HOW SURE the branch could have been: which workers had a turn open at
    the instant of the refusal it consumed, and therefore whether the refusal could only have
    been this one's. Two of the three branches do not use identity at all -- by design, since
    requiring it silenced them entirely above six workers -- and without this the cost of that
    choice is invisible per run and survives only as a figure in a docstring.
    """
    _append_log({
        "ts": time.time(),
        "event": "classified_locked",
        "branch": str(branch),
        "resp_len": int(resp_len),
        "turn_sent_at": float(since or 0.0),
        "consumed": consumed or {},
        "attribution": attribution or {},
    })


def locked_since(since: float, now: Optional[float] = None) -> bool:
    """True iff a refusal was recorded at or after `since`.

    NOT THE FORM A CALLER DECIDING SHOULD USE ANY MORE -- see `matching_records`. This reads
    the one-deep slot, so it answers "was something refused since then" and cannot say whether
    the refusal was about the caller asking. Kept for the CLI and for diagnostics, where naming
    the last refusal is exactly the question.

    `locked_recently` answers "was anything
    refused lately", which is too broad to judge one turn by: a refusal from an
    unrelated earlier call would mark every reply for the next few minutes as
    locked. Pass the moment the turn was sent and only its own refusal counts.
    """
    try:
        boundary = float(since)
    except (TypeError, ValueError):
        return False
    if boundary <= 0.0:
        return False
    state = read_state()
    try:
        ts = float(state.get("ts") or 0.0)
    except (TypeError, ValueError):
        return False
    if ts < boundary:
        return False
    current = float(now if now is not None else time.time())
    # Still bounded by freshness so a clock jump cannot resurrect an ancient record.
    return (current - ts) <= DEFAULT_FRESH_SEC


def _cli() -> None:
    """`python -m tools.lock_state [show|token-gap]`, printing JSON.

    `show` prints the last recorded refusal (or {}). The cockpit's 詳細設定/Advanced panel
    shells out to it to surface the most recently refused client without a human having to look
    the IP up by hand, and it is the default so that call keeps working unchanged.

    `token-gap` answers whether MCP_REQUIRE_UNLOCK_TOKEN can be switched on and what it would
    refuse. It is here rather than beside `python -m tools.security list` because security.py is
    in the frozen set, where a change means the operator re-signs the baseline with a reason --
    not a trade worth making for a report, and the counter it reads lives in this file anyway.

    `recent [seconds]` is the diagnostic THREE FUNCTIONS ALREADY CLAIMED TO BE KEPT FOR.
    `locked_since`, `matching_record` and `locked_recently` each say in their own docstrings
    that they exist for the CLI, and this CLI called none of them -- a justification resting on
    a surface that was never built, which reads as settled and is not. The question they answer
    together is the one a person asks about a stuck worker: was anything refused in the last N
    seconds, and which refusal was it.

    NOT `matching_records` (plural). That one answers "which refusals could have been mine",
    which is a decision a caller makes; this prints what happened. Both are wanted, and only
    the plural had a caller.
    """
    import sys

    argv = sys.argv[1:]
    cmd = argv[0] if argv else "show"
    if cmd not in ("show", "token-gap", "recent"):
        print(json.dumps({
            "error": "usage: python -m tools.lock_state [show|token-gap|recent [seconds]]"}))
        raise SystemExit(2)
    if cmd == "token-gap":
        print(json.dumps(token_gap_report(), ensure_ascii=False))
        return
    if cmd == "recent":
        try:
            within = float(argv[1]) if len(argv) > 1 else DEFAULT_FRESH_SEC
        except ValueError:
            print(json.dumps({"error": "seconds must be a number"}))
            raise SystemExit(2)
        now = time.time()
        # ALL THREE, AND LABELLED SO THEY DO NOT READ AS A CONTRADICTION. They answer different
        # questions and the first draft of this printed them as though they answered one: over
        # a 24h window it said locked_recently=true, locked_since=false, record={} -- which
        # looks like a bug and is the design. `locked_recently` honours the window it is given;
        # `locked_since` and `matching_record` are ADDITIONALLY capped at DEFAULT_FRESH_SEC, so
        # a clock jump cannot resurrect an ancient record. The cap is printed beside them.
        state = read_state()
        try:
            last_ts = float(state.get("ts") or 0.0)
        except (TypeError, ValueError):
            last_ts = 0.0
        print(json.dumps({
            "asked_window_s": within,
            "in_asked_window": locked_recently(within, now=now),
            "fresh_window_s": DEFAULT_FRESH_SEC,
            "in_fresh_window": locked_since(now - within, now=now),
            "last_refusal_ts": last_ts or None,
            "last_refusal_age_s": round(now - last_ts, 1) if last_ts else None,
            "record_if_fresh": matching_record(now - within, now=now),
        }, ensure_ascii=False))
        return
    print(json.dumps(read_state(), ensure_ascii=False))


#: Calls that PASSED the unlock gate on the strength of the identity alone -- no matching
#: unlock token was presented. Kept as a counter beside the state file rather than a single
#: latest record, because the question it answers is "how many callers would enforcement
#: break?", and that is a total over a period, not a most-recent event.
_TOKEN_GAP_FILE = _STATE_FILE.parent / "unlock_token_gap.json"


def record_token_gap(client_ip: str = "", ts: Optional[float] = None) -> None:
    """Note a call allowed without a token, so enforcement can be switched on with evidence.

    MCP_REQUIRE_UNLOCK_TOKEN defaults to off: turning it on before anyone has re-unlocked
    would refuse every existing session at once, and an outage is how a security change gets
    reverted wholesale instead of kept. This counter is what says when it is safe -- when it
    stops growing, every live caller is presenting a token and the switch costs nothing.

    Never raises: a counter that can fail a request is worse than a counter.
    """
    now = float(ts if ts is not None else time.time())
    try:
        with _LOCK:
            data = {"count": 0, "ips": {}, "first_ts": now}
            if _TOKEN_GAP_FILE.exists():
                try:
                    data = json.loads(_TOKEN_GAP_FILE.read_text(encoding="utf-8")) or data
                except Exception:
                    pass
            data["count"] = int(data.get("count", 0)) + 1
            data["last_ts"] = now
            data.setdefault("first_ts", now)
            ips = data.setdefault("ips", {})
            key = str(client_ip or "")[:64]
            ips[key] = int(ips.get(key, 0)) + 1
            _TOKEN_GAP_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(_TOKEN_GAP_FILE.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False)
                os.replace(tmp, _TOKEN_GAP_FILE)
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
    except Exception:
        pass


def token_gap() -> dict:
    """How many calls have passed without a token, and from where. {} if none."""
    try:
        if _TOKEN_GAP_FILE.exists():
            return json.loads(_TOKEN_GAP_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


#: How long the gap must stay quiet before enforcement is safe to switch on.
#:
#: DERIVED, NOT CHOSEN. An unlock grant lasts MCP_UNLOCK_TTL_DAYS (default 30), and a caller
#: that has passed the gate on identity alone holds one. Once a full TTL has gone by with no
#: such call recorded, every grant still in the table was established after the last gap, so
#: nothing is relying on the identity-only path any more.
#:
#: WHAT IT DOES NOT PROVE: a caller that holds a grant and simply has not called in that window
#: is indistinguishable from one that went away. This is the strongest statement the counter can
#: support, not a guarantee, which is why the report prints the raw numbers beside it.
def _token_gap_quiet_seconds() -> float:
    return float(os.environ.get("MCP_UNLOCK_TTL_DAYS", "30")) * 86400.0


def token_gap_report() -> dict:
    """Whether MCP_REQUIRE_UNLOCK_TOKEN can be switched on, and what it would break.

    record_token_gap() above has counted every call that passed the unlock gate on the strength
    of the identity alone since 2026-08-18, and its docstring says what the count is for -- "this
    counter is what says when it is safe ... when it stops growing, every live caller is
    presenting a token and the switch costs nothing". Nothing read it for 26 days.

    MEASURED 2026-09-13 on the live file: 154 calls, the most recent that same morning, 146 of
    them from ONE address. So the answer it had been holding was no, and emphatically --
    enforcement would have refused the live integration -- and there was no way to ask.

    `ips` is part of the answer, not decoration: the count alone cannot tell one live integration
    from a hundred stragglers, and that difference is the whole decision.
    """
    from tools.security import enforce_unlock_token   # read, not modified: see module note

    gap = token_gap()
    count = int(gap.get("count", 0) or 0)
    last = float(gap.get("last_ts", 0) or 0)
    quiet_for = (time.time() - last) if last else None
    return {
        "enforcing": enforce_unlock_token(),
        "count": count,
        "first_ts": gap.get("first_ts") or None,
        "last_ts": gap.get("last_ts") or None,
        "quiet_for_seconds": quiet_for,
        "quiet_required_seconds": _token_gap_quiet_seconds(),
        "ips": dict(gap.get("ips") or {}),
        "safe_to_enforce": count == 0 or (
            quiet_for is not None and quiet_for >= _token_gap_quiet_seconds()),
    }


def token_gap_warning() -> str:
    """One line for the server log at startup, or "" when there is nothing to say.

    A REPORT NOBODY RUNS IS THE SAME AS NO REPORT. The counter went unread for 26 days while a
    subcommand would have printed it on request; what was missing was not a formatter but a
    reader that runs without being asked. Printed once per boot, and only while there is an
    actual answer -- silence here means the switch is free.
    """
    try:
        r = token_gap_report()
    except Exception:
        return ""
    if r["enforcing"] or r["safe_to_enforce"] or not r["count"]:
        return ""
    top = sorted(r["ips"].items(), key=lambda kv: -kv[1])[:3]
    return ("[unlock] MCP_REQUIRE_UNLOCK_TOKEN is OFF; %d call(s) have passed with no token "
            "(last %s). Turning it on now would refuse: %s"
            % (r["count"],
               time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_ts"] or 0)),
               ", ".join("%s x%d" % (ip or "(unknown)", n) for ip, n in top) or "(unknown)"))


# AT THE END, NOT IN THE MIDDLE. This block used to sit directly under _cli(), which
# meant it ran while the rest of the module was still being defined -- fine for `show`,
# and a NameError for any subcommand reading something declared below it.
if __name__ == "__main__":
    _cli()

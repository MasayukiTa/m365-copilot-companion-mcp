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


def _append_log(payload: dict) -> None:
    """One line per refusal. Best effort; a log that cannot be written must not refuse a call."""
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8", newline=chr(10)) as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + chr(10))
    except Exception:
        pass


def record_locked(client_ip: str = "", detail: str = "", ts: Optional[float] = None) -> None:
    """Note that require_unlocked() just refused a call. Never raises."""
    payload = {
        "ts": float(ts if ts is not None else time.time()),
        "client_ip": str(client_ip or "")[:64],
        "detail": str(detail or "")[:200],
        "site": _caller_site(),
    }
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


def matching_record(since: float, now: Optional[float] = None) -> dict:
    """The refusal `locked_since` would match, or {} when there is none.

    Exists so a caller can say WHICH record it acted on. `locked_since` answers yes or no, and
    a yes that cannot name its evidence is what made one worker's refusal look like every
    worker's lock for a whole run.
    """
    if not locked_since(since, now):
        return {}
    return read_state()


def record_classification(branch: str, *, resp_len: int, since: float,
                          consumed: Optional[dict] = None) -> None:
    """Note that a reader classified a reply as locked, and on what evidence. Never raises."""
    _append_log({
        "ts": time.time(),
        "event": "classified_locked",
        "branch": str(branch),
        "resp_len": int(resp_len),
        "turn_sent_at": float(since or 0.0),
        "consumed": consumed or {},
    })


def locked_since(since: float, now: Optional[float] = None) -> bool:
    """True iff a refusal was recorded at or after `since`.

    This is the form callers should use. `locked_recently` answers "was anything
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
    """`python -m tools.lock_state show` -- prints the last recorded refusal (or {}) as
    JSON. The cockpit's 詳細設定/Advanced panel shells out to this to surface the most
    recently refused client without a human having to look the IP up by hand."""
    import sys

    if len(sys.argv) > 1 and sys.argv[1] != "show":
        print(json.dumps({"error": "usage: python -m tools.lock_state show"}))
        raise SystemExit(2)
    print(json.dumps(read_state(), ensure_ascii=False))


if __name__ == "__main__":
    _cli()


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

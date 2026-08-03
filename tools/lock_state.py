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
_LOCK = threading.Lock()

# A reader asking "was a call just refused for lock?" only cares about the recent
# past; an hour-old refusal says nothing about the turn being judged now.
DEFAULT_FRESH_SEC = 180.0


def record_locked(client_ip: str = "", detail: str = "", ts: Optional[float] = None) -> None:
    """Note that require_unlocked() just refused a call. Never raises."""
    payload = {
        "ts": float(ts if ts is not None else time.time()),
        "client_ip": str(client_ip or "")[:64],
        "detail": str(detail or "")[:200],
    }
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

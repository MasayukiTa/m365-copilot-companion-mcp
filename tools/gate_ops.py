"""Operator E — human-in-the-loop (HITL) gate + kill-switch.

Lets an autonomous loop pause and ask the human a question at decision points
(fixed point not reached, verification mismatch, low confidence), and lets the
human stop the whole thing.

Mechanism is deliberately simple and file-based so it works across processes and
survives restarts:
  - gate_ask: raise a desktop toast and write a pending question; returns a token.
  - gate_poll: the loop polls this token; returns the answer once the human writes it.
  - gate_answer: the human (or a UI) writes the answer for a token.
  - gate_list: show open gates.
  - stop_request / stop_check / stop_clear: a global kill-switch the loop checks
    every iteration (and during long waits) to abort promptly.

State lives under <MCP_ALLOWED_BASE>/.companion_gates/.
"""
import json
import time
import uuid
from pathlib import Path
from typing import Optional

from .file_ops import ALLOWED_BASE
from .notify_ops import notify_desktop
from .security import require_unlocked

GATE_DIR = ALLOWED_BASE / ".companion_gates"
STOP_FILE = GATE_DIR / "STOP_RELAY"


def _ensure() -> None:
    GATE_DIR.mkdir(parents=True, exist_ok=True)


def gate_ask(question: str, context: Optional[str] = None, notify: bool = True) -> str:
    """Pause and ask the human a question; returns a token to poll for the answer.

    Raises a desktop toast (if available) so the human notices, and writes a
    pending question file. The autonomous loop should then call gate_poll(token)
    until an answer appears.

    Args:
        question: The question to put to the human.
        context: Optional extra context shown alongside the question.
        notify: Whether to raise a desktop toast.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        _ensure()
        token = "gate_" + uuid.uuid4().hex[:10]
        payload = {
            "token": token,
            "question": question,
            "context": context or "",
            "asked_at": time.time(),
            "answered": False,
            "answer": None,
        }
        (GATE_DIR / f"{token}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if notify:
            notify_desktop("HITL gate — input needed", question[:180])
        return (
            f"token: {token}\n"
            f"question posted. The human answers with gate_answer(token, ...), "
            f"or by editing {GATE_DIR / (token + '.json')}. Poll with gate_poll('{token}')."
        )
    except Exception as e:
        return f"[gate_ask error: {type(e).__name__}: {e}]"


def gate_poll(token: str) -> str:
    """Check whether a gate question has been answered yet.

    Returns the answer if present, otherwise a "still waiting" marker so the loop
    can sleep and poll again.
    """
    try:
        path = GATE_DIR / f"{token}.json"
        if not path.is_file():
            return f"[gate_poll: unknown token {token}]"
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("answered"):
            return f"ANSWERED: {data.get('answer')}"
        waited = time.time() - data.get("asked_at", time.time())
        return f"WAITING ({int(waited)}s so far) — question: {data.get('question')}"
    except Exception as e:
        return f"[gate_poll error: {type(e).__name__}: {e}]"


def gate_answer(token: str, answer: str) -> str:
    """Provide a human answer for a pending gate (also usable from a UI or by the user)."""
    try:
        path = GATE_DIR / f"{token}.json"
        if not path.is_file():
            return f"[gate_answer: unknown token {token}]"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["answered"] = True
        data["answer"] = answer
        data["answered_at"] = time.time()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"answer recorded for {token}"
    except Exception as e:
        return f"[gate_answer error: {type(e).__name__}: {e}]"


def gate_list() -> str:
    """List open (unanswered) and recently answered gates."""
    try:
        if not GATE_DIR.is_dir():
            return "(no gates)"
        rows = []
        for p in GATE_DIR.glob("gate_*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                rows.append(d)
            except Exception:
                continue
        if not rows:
            return "(no gates)"
        rows.sort(key=lambda d: d.get("asked_at", 0), reverse=True)
        lines = []
        for d in rows:
            status = "ANSWERED" if d.get("answered") else "OPEN"
            lines.append(f"[{status}] {d.get('token')}  {str(d.get('question'))[:70]}")
        return "\n".join(lines)
    except Exception as e:
        return f"[gate_list error: {type(e).__name__}: {e}]"


def stop_request(reason: str = "") -> str:
    """Raise the global kill-switch. An autonomous loop checking stop_check aborts."""
    locked = require_unlocked()
    if locked:
        return locked
    try:
        _ensure()
        STOP_FILE.write_text(
            json.dumps({"reason": reason, "at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
        return "STOP requested. Loops checking stop_check() should abort."
    except Exception as e:
        return f"[stop_request error: {type(e).__name__}: {e}]"


def stop_check() -> str:
    """Return STOP if the kill-switch is set, otherwise RUN. Loops call this each iteration."""
    try:
        if STOP_FILE.is_file():
            data = json.loads(STOP_FILE.read_text(encoding="utf-8"))
            return f"STOP (reason: {data.get('reason') or '(none)'})"
        return "RUN"
    except Exception:
        return "STOP (kill-switch file unreadable; failing safe)"


def stop_clear() -> str:
    """Clear the kill-switch so loops may run again."""
    locked = require_unlocked()
    if locked:
        return locked
    try:
        if STOP_FILE.is_file():
            STOP_FILE.unlink()
        return "STOP cleared."
    except Exception as e:
        return f"[stop_clear error: {type(e).__name__}: {e}]"

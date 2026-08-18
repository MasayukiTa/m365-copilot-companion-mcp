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
import os
import uuid
from pathlib import Path
from typing import Optional

from .file_ops import ALLOWED_BASE
from .notify_ops import notify_approval_gate
from .security import require_unlocked

#: Where the kill-switch and the HITL gate files live.
#:
#: REDIRECTABLE, AND THE REASON IS NOT TEST CONVENIENCE. The default is under the account's
#: home, so every checkout and every server instance running as one user shares ONE global
#: stop: a test that trips a contract stop parks the real fleet, and a clear meant for one
#: run releases all of them. That is not hypothetical -- it happened the moment the
#: in-process stop path started working, and a stale switch then made six unrelated
#: scenarios abort while the report blamed the scenarios.
#:
#: The default is unchanged, so nothing about a normal deployment moves. What this buys is
#: the ability to give a test session, or a second instance, a namespace of its own. Proper
#: per-run scoping -- stop ids, generations, worker acknowledgements -- is a larger design
#: and is NOT what this is.
GATE_DIR = Path(os.environ.get("MCP_GATE_DIR") or (ALLOWED_BASE / ".companion_gates"))
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
        gate_path = GATE_DIR / f"{token}.json"
        gate_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if notify:
            notify_approval_gate("HITL gate - input needed", question[:180], gate_path)
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


#: What `stop_request_internal` returns when the switch is genuinely engaged. Callers that
#: need to know -- and the contract gate is one -- compare against this rather than parsing
#: prose, because the failure string is also prose.
STOP_ENGAGED = "STOP engaged."


def stop_request_internal(reason: str = "", *, source: str = "internal") -> str:
    """Raise the kill-switch from INSIDE the process, with no HTTP authorisation predicate.

    WHY THIS EXISTS SEPARATELY FROM `stop_request`.

    `require_unlocked()` answers "is this REMOTE CALLER entitled to mutate things", and it
    denies whenever there is no HTTP request context at all. That is right for a tool a model
    can invoke. It is wrong for the safety path: `contract_gate` detects a destructive
    operation class and tries to halt the fleet, and it was being refused by an
    authorisation check about a caller it is not -- so the gate printed "this fleet run is
    stopping" while the switch stayed off and the other workers kept going. The message was
    the only thing that stopped.

    So the split is by WHO IS ASKING, not by how much authority the answer carries. Code
    already running inside this process has, by construction, whatever authority let it get
    there; making it re-prove that over HTTP proves nothing and fails exactly when the
    reason to stop is most urgent.

    RELEASING the switch is unchanged and still authorised. That asymmetry is the point: the
    stop is the safe direction and the release is the privileged one.

    Returns STOP_ENGAGED only after `stop_check` actually reports STOP. The old version
    returned "STOP requested" from the line after the write, so a caller could be told the
    fleet was stopping by a function that had done nothing.
    """
    try:
        _ensure()
        payload = json.dumps({"reason": reason, "at": time.time(), "source": source},
                             ensure_ascii=False)
        # ATOMIC, same directory: a reader that catches a half-written file gets a STOP it
        # cannot parse. `stop_check` fails safe on that, so the old way was not dangerous --
        # it was merely a stop nobody could explain, which is its own problem at 3am.
        tmp = STOP_FILE.with_name(STOP_FILE.name + ".tmp-%d" % os.getpid())
        tmp.write_text(payload, encoding="utf-8")
        os.replace(str(tmp), str(STOP_FILE))
    except Exception as e:
        return f"[stop_request error: {type(e).__name__}: {e}]"
    # VERIFIED, NOT ASSUMED. The whole defect this replaces was a function reporting an
    # outcome it had not checked.
    if not stop_check().startswith("STOP"):
        return ("[stop_request error: wrote the kill-switch file but stop_check still "
                "reports RUN; the switch is NOT engaged]")
    return STOP_ENGAGED


def stop_request(reason: str = "") -> str:
    """Raise the global kill-switch. An autonomous loop checking stop_check aborts.

    The MODEL-FACING entry point, and it keeps its authorisation check -- a stop is cheap to
    trigger and latches until someone with `stop_clear` rights removes it, so an arbitrary
    remote caller does not get to park the fleet indefinitely. In-process safety callers use
    `stop_request_internal`, which is a different question with a different answer.
    """
    locked = require_unlocked()
    if locked:
        return locked
    return stop_request_internal(reason, source="tool")


def stop_check() -> str:
    """Return STOP if the kill-switch is set, otherwise RUN. Loops call this each iteration.

    Deliberately unauthenticated: reading whether to stop must never be the thing that
    fails.

    FAILS SAFE ON ANYTHING IT CANNOT READ AS "no switch". `is_file()` was the whole test, so
    a path that exists but is a directory -- or a dangling link -- answered "RUN", which is
    the one answer a kill-switch may never give by accident. Existence is now the question
    and readability only decides what the reason says.
    """
    try:
        if not STOP_FILE.exists():
            return "RUN"
        if not STOP_FILE.is_file():
            return ("STOP (kill-switch path exists but is not a regular file; failing safe)")
        data = json.loads(STOP_FILE.read_text(encoding="utf-8"))
        return f"STOP (reason: {data.get('reason') or '(none)'})"
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

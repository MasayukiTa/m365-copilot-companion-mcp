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
    token = gate_ask_local(question, context, notify)
    if not token:
        return "[gate_ask error: could not create gate]"
    return (
        f"token: {token}\n"
        f"question posted. The human answers with gate_answer(token, ...), "
        f"or by editing {GATE_DIR / (token + '.json')}. Poll with gate_poll('{token}')."
    )


def _dedupe_token(dedupe_key: str) -> str:
    """A deterministic token for `dedupe_key`, so two callers raising the SAME cause at
    the same time collide on one filename instead of writing two.

    sha256, not uuid: uuid is random by design and would defeat the whole point --
    two workers hitting the identical question text must compute the identical token
    without coordinating. 10 hex chars matches the entropy of the random tokens
    elsewhere in this file (`uuid.uuid4().hex[:10]`), which is plenty for a namespace
    this small (open gates number in the dozens, not millions).
    """
    import hashlib

    return "gate_dd" + hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:10]


def gate_ask_local(question: str, context: Optional[str] = None,
                    notify: bool = True, dedupe_key: Optional[str] = None,
                    worker_label: Optional[str] = None) -> Optional[str]:
    """Raise a gate from IN-PROCESS code, with no unlock gate. Returns the bare token
    (or None on failure) rather than gate_ask's formatted human-readable string.

    WHY THIS EXISTS, AND WHY IT IS NOT A HOLE -- same shape as memory_save_local /
    runlog_append_local (see tools/security.py's refusal text, which names both by
    example): `gate_ask` requires require_unlocked(), which answers "has the REMOTE
    caller behind this HTTP request proved possession of the password". A caller
    running inside this process (the fleet relay, deciding on a WORKER's behalf that a
    human is needed) has no remote identity and no request, so the gate cannot answer
    it -- require_unlocked() denies, and unlock() itself needs a request too, so such a
    caller has no move that works.

    THE INCIDENT THIS FIXES. relay/relay_fleet.py can conclude a worker cannot proceed
    without a person for reasons that are THEMSELVES about being locked (unlock
    exhausted after MAX_UNLOCK_ATTEMPTS -- see _inject_unlock). A relay that tried to
    raise that gate by calling the gated `gate_ask` would be refused for the very
    reason it is trying to report, and a worker that is stuck because it cannot unlock
    cannot use a tool that itself requires being unlocked. The relay is not a remote
    MCP caller -- it is the in-process supervisor that already holds MCP_ALLOWED_BASE
    and the .env password -- so it asks on the worker's behalf through this local path
    instead, the same way memory_save_local lets an in-process caller record something
    memory_save's gate would otherwise refuse.

    fleet_toolset.py's own denylist already says the other half out loud: a WORKER
    must not call gate_ask as a tool ("a worker must not create the approval it would
    then be answering"). This function is for the RELAY, not the worker.

    DEDUPE, 2026-09-24. `_raise_stuck_gate` already refuses a SECOND gate for the SAME
    worker ("if self._gate_token: return False") but nothing stopped a SECOND, THIRD, ...
    worker from each raising their OWN gate for the identical cause -- measured the same
    day: 8 fleet workers all hit "unlock exhausted after 4 attempts" within 12 minutes,
    each posting the byte-identical question, so the owner got 8 desktop toasts asking
    the same thing. `dedupe_key` (the caller passes the question text itself, by
    default -- see relay_fleet._raise_stuck_gate) maps to a DETERMINISTIC filename via
    `_dedupe_token`. If an OPEN (unanswered) gate already exists under that token,
    later callers ATTACH to it (recorded in "workers") and get the SAME token back --
    one file, one toast, one question -- instead of writing a new one. Once that gate
    is answered, the next occurrence of the same cause opens a fresh one: an answered
    gate is history, not a standing rule, and pretending otherwise would auto-resolve a
    recurrence the operator never actually saw.
    """
    try:
        _ensure()
        if dedupe_key:
            token = _dedupe_token(dedupe_key)
            gate_path = GATE_DIR / f"{token}.json"
            if gate_path.is_file():
                try:
                    existing = json.loads(gate_path.read_text(encoding="utf-8"))
                except Exception:
                    existing = None
                if existing is not None and not existing.get("answered"):
                    # ATTACH, DON'T RE-ASK. Best-effort: a worker list that misses one
                    # entry under a race is a cosmetic loss (the gate itself, and the
                    # answer every attached worker polls for, are unaffected).
                    try:
                        workers = list(existing.get("workers") or [])
                        label = worker_label or ""
                        if label and label not in workers:
                            workers.append(label)
                            existing["workers"] = workers
                            if context:
                                contexts = dict(existing.get("contexts") or {})
                                contexts[label] = context
                                existing["contexts"] = contexts
                            gate_path.write_text(
                                json.dumps(existing, ensure_ascii=False, indent=2),
                                encoding="utf-8")
                    except Exception:
                        pass
                    return token
                # THE DETERMINISTIC TOKEN IS TAKEN AND ANSWERED -- do NOT reuse it. The
                # first cut of this function fell through to create-below using the SAME
                # deterministic token, which meant "create" actually meant "collide": the
                # O_CREAT|O_EXCL open below hits FileExistsError against the old answered
                # file, the FileExistsError handler reads it back, sees answered=True, and
                # returns THAT stale token -- so a brand-new occurrence of the same cause
                # silently inherited an old answer instead of asking again. Caught by
                # relay/test_a_worker_that_needs_a_person_should_say_so.py's own suite:
                # two unrelated tests share the identical STUCK finding text, one answers
                # its gate, and the next one's _poll_gate then read "answered" on a token
                # it never itself raised. A random token for this one occurrence -- no
                # longer deduped against anything, since there is nothing open left to
                # dedupe against -- sidesteps the collision entirely; the NEXT caller
                # within THIS gate's own now-open lifetime still dedupes normally, because
                # the payload below still carries dedupe_key... except the filename must
                # differ from the answered one, so it cannot be re-derived from dedupe_key
                # by the same formula. Accept that (documented in the docstring above): a
                # cause that recurs AFTER being answered opens a fresh, non-deterministic
                # gate, exactly like dedupe_key=None.
                token = "gate_" + uuid.uuid4().hex[:10]
        else:
            token = "gate_" + uuid.uuid4().hex[:10]
        payload = {
            "token": token,
            "question": question,
            "context": context or "",
            "asked_at": time.time(),
            "answered": False,
            "answer": None,
        }
        if dedupe_key:
            payload["dedupe_key"] = dedupe_key
            payload["workers"] = [worker_label] if worker_label else []
            if worker_label and context:
                payload["contexts"] = {worker_label: context}
        gate_path = GATE_DIR / f"{token}.json"
        try:
            # Atomic-ish: O_EXCL keeps two siblings racing on the SAME deterministic
            # token from both writing a "first" version -- the loser attaches instead.
            fd = os.open(str(gate_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, indent=2))
        except FileExistsError:
            # Lost the race (or, for a non-deduped token, an impossibly unlucky uuid
            # collision). Either way someone else's file is there now; attach to it
            # exactly like the "already exists" branch above.
            try:
                existing = json.loads(gate_path.read_text(encoding="utf-8"))
            except Exception:
                existing = None
            if existing is not None and not existing.get("answered"):
                try:
                    workers = list(existing.get("workers") or [])
                    label = worker_label or ""
                    if label and label not in workers:
                        workers.append(label)
                        existing["workers"] = workers
                        gate_path.write_text(
                            json.dumps(existing, ensure_ascii=False, indent=2),
                            encoding="utf-8")
                except Exception:
                    pass
                return token
            # The racing sibling's gate was already answered by the time we lost the
            # race (vanishingly unlikely) -- there is nothing safe left to overwrite,
            # so report success on the token that exists rather than raising.
            return token
        if notify:
            notify_approval_gate("HITL gate - input needed", question[:180], gate_path)
        return token
    except Exception:
        return None


def gate_get(token: str) -> Optional[dict]:
    """Return the raw gate payload dict for `token`, or None if it does not exist or
    cannot be read. FOR IN-PROCESS CALLERS that need structured access (has it been
    answered? what did it say?) rather than gate_poll's human-readable "ANSWERED: ..."
    string, which would need parsing back apart from an answer that might itself
    contain a colon or the word ANSWERED."""
    try:
        path = GATE_DIR / f"{token}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


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

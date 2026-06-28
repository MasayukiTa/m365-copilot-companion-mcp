"""Bucket-C autonomy enforcement gate — contract_gate.py

Reads the active autonomy contract from .fleet/active_contract.json and
gates dangerous tool operations (delete, destructive shell, outbound sends)
when the contract is active.

INERT BY DEFAULT — the gate is a NO-OP unless a contract is active:
  * .fleet/active_contract.json is absent  -> check_op() returns None always
  * active_contract.json exists but active != true -> None always
  * active_contract.json has active=true  -> gate fires only for listed ops

active_contract.json schema
---------------------------
{
    "active": true,
    "scope": "C:/Users/me/project",      // informational folder scope
    "ask_before": ["delete", "outbound", "shell_destructive"],
    "stop_when":  [],                    // op classes that trigger hard stop
    "started": 1719500000.0             // epoch when the contract was activated
}

op_class values recognised by check_op():
  "delete"           - file/directory deletion (delete_path / trash_path)
  "outbound"         - email send_immediately, external publish POSTs
  "shell_destructive"- destructive shell commands (see destructive_shell())

Gate file path for cockpit to ANSWER a pending gate
----------------------------------------------------
Path: <MCP_ALLOWED_BASE>/.companion_gates/<token>.json

To APPROVE:  write {"answered": true, "answer": "approved"}   (merge into existing)
To DENY:     write {"answered": true, "answer": "denied"}

Full gate file shape (written by gate_ask, read by gate_poll):
{
    "token":      "gate_<10hex>",
    "question":   "Approve delete: C:/path/to/file?",
    "context":    "contract gate: delete",
    "asked_at":   1719500000.0,
    "answered":   false,
    "answer":     null
}
The cockpit must set answered=true and answer="approved"|"denied" atomically
(write a temp file then rename) so the gate_poll reader never sees a partial write.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

# ── Locate the .fleet directory the same way fleet_runner does (repo root) ──
_THIS = Path(__file__).resolve()          # tools/contract_gate.py
_REPO = _THIS.parent.parent              # companion-mcp/
_FLEET_DIR = _REPO / ".fleet"
_CONTRACT_FILE = _FLEET_DIR / "active_contract.json"

# ── Gate directory (mirrors gate_ops.py / GATE_DIR) ──
# Imported lazily inside functions to avoid circular imports at module load.


# ---------------------------------------------------------------------------
# Contract loading
# ---------------------------------------------------------------------------

def load_contract() -> Optional[dict]:
    """Read .fleet/active_contract.json.  Returns dict or None (missing/bad JSON/inactive)."""
    try:
        if not _CONTRACT_FILE.is_file():
            return None
        data = json.loads(_CONTRACT_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def deactivate_contract() -> None:
    """Set active=false in the contract file (called by fleet_runner on exit)."""
    try:
        if not _CONTRACT_FILE.is_file():
            return
        data = json.loads(_CONTRACT_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        data["active"] = False
        tmp = str(_CONTRACT_FILE) + ".tmp"
        Path(tmp).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, str(_CONTRACT_FILE))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Destructive shell matcher
# ---------------------------------------------------------------------------

# These patterns match ONLY genuinely destructive shell commands.
# Conservative: must NOT match pytest / git status / normal builds / git add / git commit.
_DESTRUCTIVE_PATTERNS = [
    # rm variants
    re.compile(r"\brm\s+(?:.*\s)?-[rRfFdI]*[rRfF][rRfFdI]*\b"),     # rm -rf / rm -fr etc.
    re.compile(r"\brm\s+-[rRfFdI]*[rRfF][rRfFdI]*"),                 # rm -rf at start of arg
    # del/rd/rmdir (Windows cmd)
    re.compile(r"\bdel\s+/[sS]\b"),                                   # del /s (recursive)
    re.compile(r"\brmdir\s+/[sS]\b"),                                 # rmdir /s
    re.compile(r"\brd\s+/[sS]\b"),                                    # rd /s
    # PowerShell Remove-Item -Recurse
    re.compile(r"\bRemove-Item\b(?=.*-Recurse)", re.IGNORECASE),
    # PowerShell Remove-Item -Force on dirs (lone -Force on a file is less dangerous,
    # but combined with a path pattern it can wipe dirs)
    re.compile(r"\bRemove-Item\b(?=.*-Force)(?=.*-Recurse)", re.IGNORECASE),
    # format (disk format)
    re.compile(r"\bformat\s+[A-Za-z]:", re.IGNORECASE),
    # diskpart
    re.compile(r"\bdiskpart\b", re.IGNORECASE),
    # git destructive
    re.compile(r"\bgit\s+push\s+(?:.*\s)?--force\b"),
    re.compile(r"\bgit\s+push\s+(?:.*\s)?-f\b"),
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+clean\s+(?:.*\s)?-[fF]"),                   # git clean -f / -fd / -fdx
    re.compile(r"\bgit\s+clean\s+(?:.*\s)?-x"),                      # git clean -x (also destructive)
]


def destructive_shell(cmd_text: str) -> bool:
    """Return True ONLY for clearly destructive shell commands.

    Conservative — must NOT match pytest, git status, git add, git commit,
    normal builds, npm install, python runs, or any ordinary non-mutating command.
    """
    if not cmd_text:
        return False
    for pat in _DESTRUCTIVE_PATTERNS:
        if pat.search(cmd_text):
            return True
    return False


# ---------------------------------------------------------------------------
# Stable token derivation
# ---------------------------------------------------------------------------

def _stable_token(op_class: str, detail: str) -> str:
    """Derive a stable gate token from (op_class, detail) so a re-called op after
    approval maps to the SAME gate file and can be checked for an existing answer."""
    key = f"{op_class}::{detail}"
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"gate_{h}"


def _find_existing_gate(token: str) -> Optional[dict]:
    """Return the gate file data for `token` if it exists, else None."""
    try:
        from tools.file_ops import ALLOWED_BASE
        gate_dir = ALLOWED_BASE / ".companion_gates"
        gate_file = gate_dir / f"{token}.json"
        if not gate_file.is_file():
            return None
        return json.loads(gate_file.read_text(encoding="utf-8"))
    except Exception:
        return None


def _create_gate(token: str, question: str, context: str) -> None:
    """Write a gate file for the given token (used instead of gate_ask to supply our own token)."""
    try:
        from tools.file_ops import ALLOWED_BASE
        from tools.notify_ops import notify_desktop
        gate_dir = ALLOWED_BASE / ".companion_gates"
        gate_dir.mkdir(parents=True, exist_ok=True)
        gate_file = gate_dir / f"{token}.json"
        if gate_file.is_file():
            return   # already posted; don't overwrite (may already be answered)
        payload = {
            "token": token,
            "question": question,
            "context": context,
            "asked_at": time.time(),
            "answered": False,
            "answer": None,
        }
        gate_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            notify_desktop("自律契約ゲート — 承認が必要です", question[:180])
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main gate entry point
# ---------------------------------------------------------------------------

def check_op(op_class: str, detail: str = "") -> Optional[str]:
    """Gate a dangerous operation under the active autonomy contract.

    Returns:
        None     — gate is INERT (no contract active, or op not listed): caller proceeds.
        str      — gate fired; the returned string is the tool's return value and the
                   operation MUST NOT execute. The string explains to the agent what happened.

    This function is side-effect-free when inactive (returns None immediately).
    When active, it either:
      * stop_when match  -> triggers the kill-switch and returns a stop string
      * ask_before match -> creates/checks a HITL gate and returns a pending/denied string,
                           OR returns None if the gate was already answered "approved"
      * neither          -> returns None (not listed = not gated)
    """
    # ── INERT guard: no contract or not active ──────────────────────────────
    contract = load_contract()
    if contract is None or not contract.get("active"):
        return None

    stop_when = contract.get("stop_when") or []
    ask_before = contract.get("ask_before") or []

    # ── stop_when: trigger kill-switch ─────────────────────────────────────
    if op_class in stop_when:
        try:
            from tools.gate_ops import stop_request
            stop_request(f"contract stop_when triggered by op_class={op_class!r} detail={detail!r}")
        except Exception:
            pass
        return (
            f"[自律契約停止 / Contract stop] op_class={op_class!r} が stop_when に含まれているため"
            f"このフリートランは停止します。操作は実行されませんでした。"
            f" / op_class={op_class!r} is in stop_when; this fleet run is stopping. "
            f"Operation not executed."
        )

    # ── ask_before: HITL approval gate ─────────────────────────────────────
    if op_class in ask_before:
        token = _stable_token(op_class, detail)
        existing = _find_existing_gate(token)

        if existing is not None and existing.get("answered"):
            answer = (existing.get("answer") or "").lower().strip()
            if answer == "approved":
                return None   # approved: let the operation proceed
            # denied or any other non-approved answer
            return (
                f"[自律契約拒否 / Contract denied] op_class={op_class!r} は人間に拒否されました。"
                f"操作は実行されません。トークン: {token}"
                f" / op_class={op_class!r} was denied by the human. Operation not executed. "
                f"Token: {token}"
            )

        # Not yet answered (or gate doesn't exist yet): create it and return pending
        question = f"Approve {op_class}: {detail}?" if detail else f"Approve {op_class}?"
        context = f"contract gate: {op_class}"
        _create_gate(token, question, context)

        return (
            f"[承認待ち / Awaiting approval] この操作 ({op_class}: {detail!r}) は自律契約により"
            f"人間の承認が必要です。トークン {token!r} を gate_poll で確認してください。"
            f"承認後に同じ操作を再度呼び出すと実行されます。"
            f" / [Awaiting approval] op_class={op_class!r} detail={detail!r} needs human approval "
            f"per the autonomy contract. Poll gate {token!r} with gate_poll. "
            f"Re-call the same operation after approval to proceed."
        )

    # Not listed in either list — not gated
    return None

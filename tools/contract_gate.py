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
_REPO = _THIS.parent.parent              # repo root
_FLEET_DIR = _REPO / ".fleet"
_CONTRACT_FILE = _FLEET_DIR / "active_contract.json"

# ── Gate directory (mirrors gate_ops.py / GATE_DIR) ──
# Imported lazily inside functions to avoid circular imports at module load.


# ---------------------------------------------------------------------------
# Contract loading
# ---------------------------------------------------------------------------

# HAS THIS PROCESS EVER SEEN AN ACTIVE CONTRACT, AND WAS IT RETIRED PROPERLY.
#
# The policy file lives under .fleet, which every worker can write, and `load_contract`
# answered "missing" and "corrupt" with the same value the caller uses for "no contract is
# active" -- so deleting or truncating one file turned the gate off silently. That is the
# same fail-open shape this repository has already been bitten by once, and it is recorded
# as a rule: unknown must fall to the dangerous side.
#
# A worker can write files. It cannot write this process's memory. So the server remembers
# that it saw a contract, and a contract that then VANISHES is treated as tampering rather
# than as an absence -- unless it was retired through deactivate_contract(), which is the
# legitimate way for it to go away.
_SEEN = {"active_contract": False, "retired_via_api": False}


def contract_state() -> tuple:
    """('active' | 'inactive' | 'absent' | 'unreadable', dict | None).

    Separated from load_contract because the caller must be able to tell "there is no
    policy" from "the policy could not be read". Those were the same value, and the second
    one is an attack.
    """
    try:
        if not _CONTRACT_FILE.is_file():
            return ("absent", None)
        raw = _CONTRACT_FILE.read_text(encoding="utf-8")
    except Exception:
        return ("unreadable", None)
    try:
        data = json.loads(raw)
    except Exception:
        return ("unreadable", None)
    if not isinstance(data, dict):
        return ("unreadable", None)
    if data.get("active"):
        _SEEN["active_contract"] = True
        return ("active", data)
    return ("inactive", data)


def policy_state_is_suspect() -> Optional[str]:
    """Reason the policy state cannot be trusted right now, or None.

    Two cases, and only two: the file is present and unreadable, or it is gone after this
    process had seen an active one and nothing retired it.
    """
    state, _ = contract_state()
    if state == "unreadable":
        return "the contract file exists and could not be read as a policy object"
    if state == "absent" and _SEEN["active_contract"] and not _SEEN["retired_via_api"]:
        return "an active contract was in force and its file has since disappeared"
    return None


def load_contract() -> Optional[dict]:
    """Read .fleet/active_contract.json.  Returns dict or None (missing/bad JSON/inactive).

    Kept for callers that only want the object. Anything making a SECURITY decision must use
    contract_state() or policy_state_is_suspect() instead -- None here still cannot tell an
    absent policy from an unreadable one.
    """
    _state, data = contract_state()
    return data


def deactivate_contract() -> None:
    """Set active=false in the contract file (called by fleet_runner on exit).

    Also records that the contract went away legitimately, so its later absence is not read
    as tampering."""
    _SEEN["retired_via_api"] = True
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

    # ── PowerShell ────────────────────────────────────────────────────────────────────
    # THE WEIGHTING WAS BACKWARDS. Sixteen patterns covered destructive PYTHON and two
    # covered PowerShell -- one of which was subsumed by the other -- on Windows, where
    # PowerShell is the most capable thing available. A probe of ten ordinary destructive
    # one-liners caught two. These are the eight that slipped.
    #
    # Aliases matter as much as the full names: `ri` IS Remove-Item to the interpreter, and a
    # denylist that only knows the long form is a denylist that asks nicely.
    re.compile(r"\b(?:Remove-Item|ri|rmdir|erase)\b(?=.*[\\/*?])", re.IGNORECASE),
    re.compile(r"\bRemove-Item\b(?=.*\bHK(?:LM|CU|CR|U|CC):)", re.IGNORECASE),
    # `Clear-Content` empties a file and is destructive on its own. `Set-Content` was in here
    # too and should not have been: writing an output artefact is ordinary work, and a gate
    # that fires on every generated report is one people learn to approve unread -- which
    # costs more than it protects. Only the overwrite-a-thing-that-exists shapes remain.
    re.compile(r"\bClear-Content\b", re.IGNORECASE),
    re.compile(r"\b(?:Set-Content|Out-File)\b(?=.*-Force)", re.IGNORECASE),
    # `del` / `rd` / `erase` ARE Remove-Item to the interpreter, so a denylist that only knows
    # the long name is asking politely. Bare `del $PROFILE` deletes a file with no flags at all.
    re.compile(r"\b(?:del|erase|rd)\s+[^\s|;]+", re.IGNORECASE),
    re.compile(r"\bStop-Process\b", re.IGNORECASE),
    # COMMAND NAMES BUILT AT RUN TIME. `& ('Remove-'+'Item')` is not matched by any pattern for
    # `Remove-Item`, and cannot be: the name does not exist until the expression is evaluated.
    # Like -EncodedCommand and iex, this is matched ON rather than THROUGH -- assembling a
    # cmdlet name from pieces is a decision to be unreadable, and unreadable is unjudged.
    re.compile(r"&\s*\(\s*['\"]", re.IGNORECASE),
    re.compile(r"\b(?:Invoke-Command|Start-Process)\b(?=.*\$)", re.IGNORECASE),
    re.compile(r"\b(?:Format-Volume|Clear-Disk|Initialize-Disk|Remove-Partition)\b",
               re.IGNORECASE),
    re.compile(r"\b(?:Stop-Computer|Restart-Computer|Stop-Service|Remove-Service)\b",
               re.IGNORECASE),
    re.compile(r"\b(?:Remove-ItemProperty|Set-ItemProperty)\b(?=.*\bHK(?:LM|CU|CR|U|CC):)",
               re.IGNORECASE),
    re.compile(r"\bRemove-(?:ADUser|LocalUser|Mailbox|AzResource)\b", re.IGNORECASE),

    # ── INSTALLING SOFTWARE ON THE MACHINE ────────────────────────────────────────────
    # Nothing here was gated, and on 2026-08-30 07:08 a benchmark worker ran
    #     winget install --id GoLang.Go -e --accept-source-agreements --accept-package-agreements
    # on a corporate laptop, which pulled an MSI and put Go 1.27.0 into C:\Program Files.
    # It went through as ordinary work because every pattern above asks "does this destroy
    # something", and installing destroys nothing. It changes the machine, silently, and
    # --accept-package-agreements accepts licence terms on the operator's behalf.
    #
    # THE LINE IS THE MACHINE, NOT THE PACKAGE MANAGER. `npm install` and `pip install`
    # populate a project and are ordinary work that this file's own docstring promises not to
    # gate -- gating them would fire on nearly every build and teach people to approve
    # unread. What is gated is the managers whose job is to change the computer.
    re.compile(r"\b(?:winget|choco|chocolatey|scoop)\s+(?:install|upgrade|add)\b", re.IGNORECASE),
    re.compile(r"\bmsiexec\b(?=.*/(?:i|package|update)\b)", re.IGNORECASE),
    re.compile(r"\b[^\s\"']+\.msi\b", re.IGNORECASE),
    re.compile(r"\bInstall-(?:Module|Package|Script)\b", re.IGNORECASE),
    re.compile(r"\bAdd-AppxPackage\b", re.IGNORECASE),
    re.compile(r"\b(?:Add-WindowsCapability|Enable-WindowsOptionalFeature)\b", re.IGNORECASE),
    re.compile(r"\bdism\b(?=.*/(?:add-package|add-capability|enable-feature))", re.IGNORECASE),

    # ── EXFILTRATION, which is destruction of a different kind ────────────────────────
    # A request that puts an environment variable into an outbound call is not a deletion, so
    # it fell outside every pattern above -- and it is the shape that turns a one-time
    # weakness into a permanent one.
    #
    # Scoped to network cmdlets rather than to `$env:` alone. `$env:PATH` appears in ordinary
    # scripts constantly, and a gate that fires on all of them is a gate people learn to
    # approve without reading, which is worse than not having it. The pairing is the signal.
    re.compile(r"\b(?:Invoke-WebRequest|Invoke-RestMethod|iwr|irm|curl|wget|"
               r"Start-BitsTransfer|Net\.WebClient)\b(?=.*\$env:)", re.IGNORECASE),

    # ── THE TWO THAT MAKE REGEX INSUFFICIENT, matched anyway ──────────────────────────
    # `-EncodedCommand` takes base64 and `iex` takes a string, so either one can carry
    # anything past every pattern in this file. They cannot be matched THROUGH; they can only
    # be matched ON. Treating their mere presence as destructive is not paranoia, it is the
    # only sound reading: a script that hides what it runs has declined to be judged.
    #
    # This does not make the denylist complete. `shell_exec` runs through cmd.exe and can
    # invoke `powershell -enc ...` itself, and the patterns above are the same list, so that
    # route is covered by these two lines and not by the PowerShell-specific ones. Detection
    # remains detection: it asks, it does not confine.
    re.compile(r"-e(?:nc|ncoded|ncodedcommand)?\b\s+[A-Za-z0-9+/=]{16,}", re.IGNORECASE),
    # `iex` INVOKED, not `iex` MENTIONED. The bare word matched inside a quoted string, so
    # `Write-Output 'iex is disabled by policy'` went to the approval queue -- and a gate that
    # fires on a sentence about itself is training for approving without reading. Requiring
    # something to follow it (an argument, a pipe into it) keeps the invocation and drops the
    # mention. Not a parser, and a determined author can still evade it; the point is that
    # ordinary text should not trip it.
    re.compile(r"(?:^|[;|&{(]\s*)\s*(?:iex|Invoke-Expression)\b", re.IGNORECASE),
    re.compile(r"\|\s*(?:iex|Invoke-Expression)\b", re.IGNORECASE),
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
# Destructive Python-source matcher
# ---------------------------------------------------------------------------

# run_python() executes ARBITRARY Python, so a destructive_shell() regex over the
# source text misses the Python-native ways to wreck files: os.remove/unlink/rmdir,
# shutil.rmtree/move, pathlib Path.unlink/rmdir, os.truncate, truncating/appending
# open(...,'w'|'a'|'x'|...), and the escape hatches os.system / subprocess.* which can
# run any destructive command and so bypass BOTH this and the shell gate.
#
# IMPORTANT: this is DETECTION-BASED, not a sandbox. It errs toward asking (safety),
# while letting plainly read-only code through (open(...,'r'), prints, pure compute).
_PY_DESTRUCTIVE_PATTERNS = [
    re.compile(r"\bos\.remove\s*\("),
    re.compile(r"\bos\.unlink\s*\("),
    re.compile(r"\bos\.rmdir\s*\("),
    re.compile(r"\bos\.removedirs\s*\("),
    re.compile(r"\bos\.truncate\s*\("),
    re.compile(r"\bos\.rename\s*\("),
    re.compile(r"\bos\.replace\s*\("),
    re.compile(r"\bshutil\.rmtree\s*\("),
    re.compile(r"\bshutil\.move\s*\("),
    re.compile(r"\.unlink\s*\("),                 # pathlib Path.unlink()
    re.compile(r"\.rmdir\s*\("),                  # pathlib Path.rmdir()
    re.compile(r"\.write_text\s*\("),             # pathlib Path.write_text()
    re.compile(r"\.write_bytes\s*\("),            # pathlib Path.write_bytes()
    # open(path, <mode containing w/a/x/+>) -- the MODE is the 2nd arg, so a filename
    # like open('write.txt') (1 arg, default 'r') does NOT match.
    re.compile(r"open\s*\([^,)]+,\s*(?:mode\s*=\s*)?['\"][rbt]*[wax+][rbtwax+]*['\"]"),
    # escape hatches: can run arbitrary (incl. destructive) commands, bypassing detection
    re.compile(r"\bos\.system\s*\("),
    re.compile(r"\bsubprocess\.(?:run|call|Popen|check_call|check_output)\s*\("),
]


def destructive_python(code_text: str) -> bool:
    """Return True for Python source that performs (or can perform) destructive file ops.

    Detection-based, NOT a sandbox. Catches os/shutil/pathlib deletes + truncating writes
    + os.system/subprocess escape hatches; lets read-only code (open(...,'r'), print,
    compute) pass. Used by run_python() to route such code through the contract gate.
    """
    if not code_text:
        return False
    for pat in _PY_DESTRUCTIVE_PATTERNS:
        if pat.search(code_text):
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
        from tools.notify_ops import notify_approval_gate
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
            notify_approval_gate("自律契約ゲート - 承認が必要です", question[:180], gate_file)
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
    # ── POLICY STATE MUST BE READABLE BEFORE IT CAN BE INERT ────────────────
    #
    # `check_op` is only reached for operations already detected as dangerous, so gating all
    # of them when the policy cannot be trusted is the correct fallback: the alternative is
    # what used to happen, which is that a deleted file waved them through.
    suspect = policy_state_is_suspect()
    if suspect:
        token = _stable_token(op_class, detail)
        existing = _find_existing_gate(token)
        if existing and existing.get("answer") == "approved":
            return None
        if not existing:
            _create_gate(token,
                         "契約状態が信用できないため、この操作の承認を求めます: %s" % suspect,
                         "op_class=%s detail=%s" % (op_class, detail[:400]))
        return ("[契約状態が不正 / policy state untrusted] %s。"
                "危険と判定された操作は、状態が回復するか人が承認するまで実行されません。"
                " / The policy state could not be trusted, so this operation was NOT executed."
                % suspect)

    # ── INERT guard: no contract or not active ──────────────────────────────
    contract = load_contract()
    if contract is None or not contract.get("active"):
        return None

    stop_when = contract.get("stop_when") or []
    ask_before = contract.get("ask_before") or []

    # ── stop_when: trigger kill-switch ─────────────────────────────────────
    if op_class in stop_when:
        # THE RESULT IS NOT DISCARDED. It used to be, inside `except Exception: pass`, and
        # the message below asserted that the fleet was stopping. It was not: stop_request
        # went through an HTTP authorisation check that denies in-process callers, returned
        # a "locked" string, and never wrote the switch. The offending operation was refused
        # -- that part always worked -- but every other worker kept running while the
        # operator read that the run had stopped.
        engaged, detail_msg = False, ""
        try:
            from tools.gate_ops import stop_request_internal, STOP_ENGAGED
            got = stop_request_internal(
                f"contract stop_when triggered by op_class={op_class!r} detail={detail!r}",
                source="contract_gate")
            engaged = (got == STOP_ENGAGED)
            detail_msg = "" if engaged else " (%s)" % got
        except Exception as e:
            detail_msg = " (%s: %s)" % (type(e).__name__, e)
        head = (f"[自律契約停止 / Contract stop] op_class={op_class!r} が stop_when に"
                f"含まれているため、この操作は実行されませんでした。"
                f" / op_class={op_class!r} is in stop_when; the operation was NOT executed.")
        # SAID SEPARATELY, because they are separate facts and one of them used to be
        # asserted on the strength of the other.
        if engaged:
            return head + " フリート全体の停止スイッチを立てました。 / The fleet-wide " \
                          "kill-switch is engaged."
        return head + (" 警告: フリート全体の停止スイッチは立っていません%s -- 他のワーカーは"
                       "走り続けます。 / WARNING: the fleet-wide kill-switch is NOT engaged%s"
                       " -- other workers keep running." % (detail_msg, detail_msg))

    # ── ask_before: HITL approval gate ─────────────────────────────────────
    if op_class in ask_before:
        # Bypass suppresses only human confirmation. The stop_when branch above
        # remains an always-on hard stop, and external Skill trust uses its own
        # exact-digest approval path.
        try:
            from tools.approval_policy import current_approval_mode
            if current_approval_mode() == "bypass":
                return None
        except Exception:
            pass
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

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

# ── Gate directory ──
# RESOLVED THROUGH gate_ops, NOT MIRRORED. This comment used to say the directory "mirrors
# gate_ops.py / GATE_DIR", and the two copies below built `ALLOWED_BASE / ".companion_gates"`
# by hand -- so `MCP_GATE_DIR`, which gate_ops honours, isolated gate_ops' writes and not
# these. Measured 2026-09-12: an isolated run set the variable, and its approval gate still
# landed in the operator's live directory (1433 -> 1434) while the sandbox stayed empty.
# Partial isolation is worse than none: the variable existing tells a caller the writes are
# contained, and half of them are.
# Imported lazily inside the functions to avoid circular imports at module load.


def _gate_dir():
    """Where gate files live, as gate_ops decides it.

    Falls back to the old hand-built path if gate_ops cannot be imported: a gate written in
    the default place is recoverable, and a gate that cannot be written at all silently turns
    an approval into an allow.
    """
    try:
        from tools.gate_ops import GATE_DIR
        return GATE_DIR
    except Exception:
        from tools.file_ops import ALLOWED_BASE
        return ALLOWED_BASE / ".companion_gates"


# ---------------------------------------------------------------------------
# Contract loading
# ---------------------------------------------------------------------------

# HAS ANY PROCESS EVER SEEN AN ACTIVE CONTRACT, AND WAS THAT ONE RETIRED PROPERLY.
#
# The policy file lives under .fleet, which every worker can write, and `load_contract`
# answered "missing" and "corrupt" with the same value the caller uses for "no contract is
# active" -- so deleting or truncating one file turned the gate off silently. That is the
# same fail-open shape this repository has already been bitten by once, and it is recorded
# as a rule: unknown must fall to the dangerous side.
#
# A worker can write files. So the server remembers that it saw a contract, and a contract
# that then VANISHES is treated as tampering rather than as an absence -- unless it was
# retired through deactivate_contract(), which is the legitimate way for it to go away.
#
# WHY THIS MEMORY IS A FILE, NOT A MODULE GLOBAL. It used to be a dict in this module. The
# only place that records a legitimate retirement -- deactivate_contract() at the end of a
# run -- executes in the fleet-runner PROCESS, while the gate that must honour it runs in
# the MCP SERVER process. A module global cannot cross that boundary: the runner set its
# flag and the server never saw it, so the server suspected forever and every gated op
# queued a human approval. The same split appeared in tests -- one test setting the flag
# left it set for the next, which then refused real git operations. Both are the same root:
# per-process memory for a fact two processes share. The record now lives on disk beside the
# contract, where any process reading .fleet sees the same answer.
#
# Two sidecar files, both under _FLEET_DIR next to active_contract.json:
#   _SEEN_FILE     -- the identity of the last active contract observed (its `started`
#                     stamp, or a hash of the contract when `started` is absent).
#   _RETIRED_FILE  -- the identity of the contract that deactivate_contract() last retired.
# A vanished contract is legitimate ONLY when a retirement record exists whose identity
# matches the last-seen contract. A different contract's retirement does not excuse it, so
# deleting a NEW active contract is still flagged -- the fail-closed default is preserved.


def _seen_file() -> Path:
    return _CONTRACT_FILE.parent / "contract_seen.json"


def _retired_file() -> Path:
    return _CONTRACT_FILE.parent / "contract_retired.json"


def _contract_identity(data: dict) -> str:
    """A stable id for one contract, independent of its mutable `active` flag.

    `started` is the epoch a contract was activated and does not change while it is in
    force, so it names THIS contract and not the next one. When it is absent, fall back to a
    hash of the contract with `active` removed, so toggling active=false at retirement does
    not change the identity. Never derive identity from `active` itself.
    """
    started = data.get("started")
    if started is not None:
        return "started:%r" % (started,)
    ident = {k: v for k, v in data.items() if k != "active"}
    blob = json.dumps(ident, sort_keys=True, ensure_ascii=False)
    return "hash:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _atomic_write_json(path: Path, obj: dict) -> None:
    tmp = str(path) + ".tmp"
    path.parent.mkdir(parents=True, exist_ok=True)
    Path(tmp).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, str(path))


def _read_json_file(path: Path) -> Optional[dict]:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _record_seen(identity: str) -> None:
    """Persist that an active contract with this identity was observed.

    Best-effort: a filesystem that will not accept the write leaves the sidecar absent,
    which reads back as "never saw an active contract" -- the safe direction, because a
    later absence is then treated as an ordinary no-contract case rather than excused.
    """
    try:
        current = _read_json_file(_seen_file())
        if current and current.get("identity") == identity:
            return
        _atomic_write_json(_seen_file(), {"identity": identity, "at": time.time()})
    except Exception:
        pass


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
        _record_seen(_contract_identity(data))
        return ("active", data)
    return ("inactive", data)


def policy_state_is_suspect() -> Optional[str]:
    """Reason the policy state cannot be trusted right now, or None.

    Two cases, and only two: the file is present and unreadable, or it is gone after some
    process had seen an active one and no matching retirement was recorded.

    All three inputs are on disk, so this answer is the same in the MCP server process and
    the fleet-runner process. FAIL CLOSED is preserved: an absence is excused ONLY when a
    retirement record exists AND names the same contract that was last seen active. If the
    seen record is missing (write failed, or genuinely never active) an absence is the
    ordinary no-contract case; if it is present but no matching retirement exists, the
    absence is tampering and gating stands.
    """
    state, _ = contract_state()
    if state == "unreadable":
        return "the contract file exists and could not be read as a policy object"
    if state == "absent":
        seen = _read_json_file(_seen_file())
        if not seen or not seen.get("identity"):
            return None
        retired = _read_json_file(_retired_file())
        if retired and retired.get("identity") == seen.get("identity"):
            return None
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


#: The only op_class values check_op() recognises (see its own docstring / the module
#: header). Not otherwise enforced anywhere in this file until activate_contract() below --
#: a contract naming "delet" instead of "delete" would previously write successfully and
#: gate nothing, silently, forever. Kept as a tuple rather than duplicated as a set literal
#: in two places.
KNOWN_OP_CLASSES = ("delete", "outbound", "shell_destructive")


def activate_contract(scope: str = "", ask_before=(), stop_when=(), budget_turns=None) -> dict:
    """Write .fleet/active_contract.json with active=true. The missing half of
    deactivate_contract() (codex-plan item 3, 2026-09-09): nothing in this codebase could
    turn a contract ON before this, only off.

    REFUSES rather than clobbers when a contract is ALREADY active -- two activations in a
    row would mean the second one's `started` (its identity) silently replaces the first's,
    and whoever is relying on the first contract's identity for their own retirement record
    would then retire a contract that is no longer the one enforcing anything. Call
    deactivate_contract() first if replacing an active contract is genuinely intended.

    Validates ask_before/stop_when against KNOWN_OP_CLASSES for the same reason check_op's
    own docstring enumerates them: an op_class this file does not recognise gates nothing,
    silently, and a contract that silently gates nothing is worse than no contract -- it
    reads as protection that was never there.

    `scope` is accepted and stored for the operator's own reference (it appears in the
    written file) but is NOT enforced anywhere in this module -- see the module docstring's
    own "// informational folder scope". Once active, ask_before/stop_when apply to every
    call to a gated tool on this machine, regardless of what path it touches. Callers who
    need a narrow blast radius get it by choosing a narrow op_class list, not a narrow scope.

    `budget_turns` IS enforced, and not by this module. relay/relay_fleet.py reads it once at
    fleet launch and tightens every worker's cap to min(max_turns, budget_turns), with its own
    stop reason for the case. That branch existed before this parameter did: `budget_turns`
    appeared nowhere in this file, so the only function able to write a contract could never set
    it, and the launcher's tightening was unreachable however carefully it had been written.
    None leaves it out of the file entirely, which is what keeps `effective_max_turns ==
    max_turns` the untouched default rather than a value this function chose.

    Returns {"ok": True, "contract": <dict written>} or {"ok": False, "detail": <why>}.
    """
    state, _ = contract_state()
    if state == "active":
        return {"ok": False, "detail": "a contract is already active; "
                                       "call deactivate_contract() first"}
    bad = [c for c in list(ask_before) + list(stop_when) if c not in KNOWN_OP_CLASSES]
    if bad:
        return {"ok": False, "detail": "unknown op_class %r; known: %r" % (bad, KNOWN_OP_CLASSES)}
    # VALIDATED HERE, BECAUSE THE READER CANNOT COMPLAIN. relay_fleet's check is
    # `isinstance(..., int) and > 0`; anything else is silently ignored there, so a contract
    # written with budget_turns="3" or 0 would read as a budget that was set and enforce
    # nothing -- the same silent-no-op the op_class validation above exists to prevent.
    # A bool is refused for the reason it is refused elsewhere in this repo: isinstance(True,
    # int) is True, and a budget of "one turn" is not what anyone meant by passing True.
    if budget_turns is not None:
        if isinstance(budget_turns, bool) or not isinstance(budget_turns, int):
            return {"ok": False,
                    "detail": "budget_turns must be a positive int or None, not %r"
                              % (budget_turns,)}
        if budget_turns <= 0:
            return {"ok": False,
                    "detail": "budget_turns must be > 0; the launcher ignores anything else, so "
                              "writing %r would look like a budget and enforce nothing"
                              % (budget_turns,)}
    data = {
        "active": True,
        "scope": str(scope or ""),
        "ask_before": list(ask_before),
        "stop_when": list(stop_when),
        "started": time.time(),
    }
    if budget_turns is not None:
        data["budget_turns"] = int(budget_turns)
    try:
        _atomic_write_json(_CONTRACT_FILE, data)
    except Exception as e:
        return {"ok": False, "detail": "write failed: %s" % e}
    return {"ok": True, "contract": data}


def deactivate_contract() -> None:
    """Set active=false in the contract file (called by fleet_runner on exit).

    Also records, ON DISK, that the contract went away legitimately, so its later absence is
    not read as tampering by ANY process. The record names the specific contract retired
    (its identity), so it excuses only that contract's disappearance and not a different one
    that a later worker might delete. Written before the file is flipped/removed so the
    record is never missing for a contract already gone."""
    try:
        data = None
        if _CONTRACT_FILE.is_file():
            loaded = json.loads(_CONTRACT_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        if data is None:
            # Nothing to identify; fall back to whatever we last saw active, so a retirement
            # issued after the file is already gone still excuses that same contract.
            seen = _read_json_file(_seen_file())
            identity = seen.get("identity") if seen else None
        else:
            identity = _contract_identity(data)
        if identity:
            _atomic_write_json(_retired_file(), {"identity": identity, "at": time.time()})
        if data is not None:
            data["active"] = False
            _atomic_write_json(_CONTRACT_FILE, data)
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

    # -- FETCHING A SCRIPT AND RUNNING IT ---------------------------------------------
    # The PowerShell spelling of this was already covered further down by the pattern that
    # matches Invoke-Expression on a downloaded string. The shell spelling was not.
    # `curl -sL https://... | bash` is not "populating a project" -- the line drawn two
    # comments above -- it is running code from the internet that nobody has read, and this
    # machine has Git Bash, so it runs. Measured 2026-08-31: `iwr ... | iex` was caught and
    # `curl ... | bash` passed. Half a class is not a class.
    #
    # The pipe has to reach an INTERPRETER. `curl ... | jq` and `curl ... -o file` are
    # ordinary and must not fire, or the gate becomes noise people approve unread -- which is
    # the reason npm and pip are deliberately left alone.
    re.compile(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z|k|da)?sh\b", re.IGNORECASE),
    re.compile(r"\b(?:curl|wget)\b[^|]*\|\s*(?:python[23]?|perl|ruby|node)\b", re.IGNORECASE),
    # DownloadString inside a quoted -Command never reaches a pipeline pattern, because there
    # is no literal pipe outside the string.
    re.compile(r"\bDownloadString\s*\(", re.IGNORECASE),
    re.compile(r"\bDownloadFile\s*\(", re.IGNORECASE),
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

    # ── WHAT THE ADVERSARIAL PASS OF 2026-08-31 PROVED WAS MISSING ────────────────────
    # Fifty hostile commands through the net: twenty caught, thirty not, zero false alarms on
    # ten ordinary controls. A net that is precise and half-blind is a net that will be
    # trusted at exactly the wrong moment.
    #
    # Only the misses whose effect is NOT a judgement call are added here. `npm publish` and a
    # branch delete are the SAME DESTRUCTION under a different verb, and a denylist that knows
    # `rm -rf` but not `find -delete` knows a spelling, not a class. The rest of the thirty --
    # ambiguous scope, a plausible-looking path, an install from somewhere unusual -- stay with
    # the judge, which is what it is for. Adding them here as guesses would produce the alarms
    # that teach people to approve unread.
    re.compile(r"\bfind\b[^|]*\s-delete\b", re.IGNORECASE),
    re.compile(r"\brobocopy\b(?=.*\s/(?:MIR|PURGE)\b)", re.IGNORECASE),
    re.compile(r"\btruncate\b\s+-s\s*0\b", re.IGNORECASE),
    re.compile(r"\bsc(?:\.exe)?\s+delete\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\b(?=.*(?:--delete\b|\s-d\b|\s:\w))", re.IGNORECASE),
    re.compile(r"\b(?:npm|yarn|pnpm|poetry)\s+publish\b|\btwine\s+upload\b", re.IGNORECASE),
    # `ri` with a flag and no path. The alias pattern above requires a path character, so
    # `gci . -r | ri -fo` -- delete everything the pipeline hands you -- did not match it.
    re.compile(r"\bri\b\s+[-$]", re.IGNORECASE),
    # Deleting a local branch discards commits that may exist nowhere else.
    re.compile(r"\bgit\s+branch\b[^|]*\s-D\b"),
    # Installing from somewhere that is not the configured index. `pip install pkg` and
    # `npm install` stay ordinary work -- what is gated is redirecting where the code comes
    # from. tools/env_ops.py's pip_install passes only --trusted-host, so the supported path
    # cannot trip this.
    re.compile(r"\bpip3?\s+install\b[^|]*--(?:extra-)?index-url\b", re.IGNORECASE),
    re.compile(r"\b(?:npm|pnpm|yarn)\s+(?:install|add|i)\b[^|]*\shttps?://", re.IGNORECASE),
    # DOWNLOAD, THEN RUN IT. The pipe-into-an-interpreter patterns cover the one-liner form;
    # this covers the two-step form, which is the same act with a semicolon in it.
    re.compile(r"\b(?:curl|wget|iwr|Invoke-WebRequest|Invoke-RestMethod|irm)\b[\s\S]*?"
               r"(?:;|&&)\s*[.\\/]*[\w.-]+\.(?:exe|ps1|bat|cmd|sh|msi|jar)\b", re.IGNORECASE),
    # A REMOTE ADDED AND PUSHED TO IN ONE BREATH. Adding a remote alone is ordinary; adding
    # one and pushing the repository to it in the same command is the workspace leaving.
    re.compile(r"\bgit\s+remote\s+add\b[\s\S]*\bgit\s+push\b", re.IGNORECASE),

    # PERSISTENCE. It survives the session, and every pattern above asks only whether
    # something is destroyed now.
    re.compile(r"\bschtasks\b(?=.*/create\b)", re.IGNORECASE),
    re.compile(r"\breg(?:\.exe)?\s+add\b(?=.*\\Run\b)", re.IGNORECASE),
    re.compile(r"\bNew-ItemProperty\b(?=.*\bRun\b)", re.IGNORECASE),
    re.compile(r"\bRegister-ScheduledTask\b", re.IGNORECASE),

    # READING A CREDENTIAL, which travels as far as the transcript does. tools/file_ops.py
    # refuses `.env` and `.companion_gates` outright, so a shell that will `type` them is that
    # refusal with a way around it. Kept in step with tools/command_triage.py's
    # _SENSITIVE_TARGET, which stops the same paths being exempted from judgement.
    re.compile(r"\.ssh[/\\]|\bid_(?:rsa|dsa|ecdsa|ed25519)\b", re.IGNORECASE),
    re.compile(r"\.git-credentials\b|\.companion_gates\b|\.npmrc\b|\.pypirc\b|\.netrc\b",
               re.IGNORECASE),
    # `.env` -- with a lookahead, because a bare `\.env` also matches `os.environ`, which
    # appears in ordinary Python constantly and would make this the alarm people mute.
    re.compile(r"\.env(?:\.[A-Za-z0-9_-]+)?(?=$|[\s\"'/\\])", re.IGNORECASE),
    re.compile(r"\bcmdkey\b\s*/list", re.IGNORECASE),
    re.compile(r"\bGet-Content\b(?=.*\bCredentials\b)", re.IGNORECASE),

    # SENDING THE WORKSPACE SOMEWHERE. The $env: pairing further up catches a variable placed
    # in an outbound request; these catch the file and the directory.
    re.compile(r"\b(?:scp|rsync|sftp)\b[^|]*\s[\w.-]+@[\w.-]+:", re.IGNORECASE),
    re.compile(r"\bcurl\b[^|]*\s(?:-T\b|--upload-file\b|-d\s*@|--data-binary\s*@|-F\s*\w+=@)",
               re.IGNORECASE),

    # A SHORT base64 payload. The existing -EncodedCommand pattern needs sixteen characters,
    # and `powershell -enc cm0gLXJmIC8=` -- twelve characters, decoding to `rm -rf /` -- went
    # straight through it. Anchored to powershell/pwsh so it cannot fire on `docker run -e
    # SOMEVAR=1`, which is why the general pattern's threshold stays where it is.
    re.compile(r"\b(?:powershell|pwsh)(?:\.exe)?\b[^|]*\s-e\w*\s+[A-Za-z0-9+/=]{8,}",
               re.IGNORECASE),

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

    # THE SAME ARGUMENT, IN THE OTHER LANGUAGES, and it was missing until a real command
    # turned up in this repository's own root:
    #
    #     node -e "eval(Buffer.from('<5.7 KB of base64>','base64').toString())"
    #
    # written by a benchmark worker on 2026-08-31 09:47 (cmd.txt). Decoded it was harmless --
    # an in-memory NodeBB stub for reproducing a bug, exactly the reproduce-first behaviour the
    # harness asks for, base64'd to get past Windows shell quoting. The net did not fire, and
    # `-EncodedCommand` two lines up exists because a base64 payload "has declined to be
    # judged". That reasoning is about the shape, not about PowerShell, so leaving JS and
    # Python out was an inconsistency rather than a decision.
    #
    # THE COST IS REAL AND ACCEPTED: our own workers build reproductions this way, so this
    # fires on ordinary benchmark work. It costs a judging call and a log line, not a block --
    # and a decode-then-execute that nobody can read is the one shape where asking is cheaper
    # than being wrong. Matched on decode INSIDE an execute, so a plain
    # Buffer.from(...).toString() that only prints does not trip it.
    re.compile(r"\b(?:eval|Function)\s*\(\s*(?:new\s+)?(?:Buffer\.from|atob)\s*\(",
               re.IGNORECASE),
    re.compile(r"\bnew\s+Function\s*\(\s*(?:Buffer\.from|atob)\s*\(", re.IGNORECASE),
    re.compile(r"\b(?:eval|exec)\s*\(\s*(?:base64|codecs|bytes)\b[^)]*decode", re.IGNORECASE),
    re.compile(r"\b(?:eval|exec)\s*\(\s*\w+\.decode\s*\(", re.IGNORECASE),
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
    if _INLINE_INTERPRETER.search(cmd_text):
        # THE HOLE BETWEEN THE TWO GATES. run_python() screens its source with
        # destructive_python(); shell_exec() screens its command with the patterns above. A
        # command that IS an interpreter carrying source -- `python -c "shutil.rmtree(...)"`,
        # `node -e "fs.rmSync(...)"` -- is checked by neither: the shell patterns cannot read
        # code and the Python screen never sees it. Measured 2026-08-31: three such commands
        # went through untouched. Run the source screen over the command text, plus the
        # equivalents in the other languages this machine can run.
        if destructive_python(cmd_text):
            return True
        for pat in _INLINE_DESTRUCTIVE:
            if pat.search(cmd_text):
                return True
    return False


#: An interpreter invoked with inline source rather than a file.
_INLINE_INTERPRETER = re.compile(
    r"\b(?:python[23]?|py|node|nodejs|perl|ruby|php)(?:\.exe)?\b[^|]*?\s-(?:c|e|r)\b",
    re.IGNORECASE)

#: Destroying a file in the languages destructive_python() does not speak.
_INLINE_DESTRUCTIVE = [
    re.compile(r"\b(?:rmSync|unlinkSync|rmdirSync|writeFileSync|truncateSync)\s*\("),
    re.compile(r"\bfs\.(?:rm|unlink|rmdir|writeFile)\b"),
    re.compile(r"\bunlink\b\s*(?:\(|glob\b|['\"$@])"),          # perl
    re.compile(r"\b(?:File|FileUtils|Dir)\.(?:delete|unlink|rm|rm_rf|rmdir)\b"),  # ruby
]


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
        gate_file = _gate_dir() / f"{token}.json"
        if not gate_file.is_file():
            return None
        return json.loads(gate_file.read_text(encoding="utf-8"))
    except Exception:
        return None


def _create_gate(token: str, question: str, context: str) -> None:
    """Write a gate file for the given token (used instead of gate_ask to supply our own token)."""
    try:
        from tools.notify_ops import notify_approval_gate
        gate_dir = _gate_dir()
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

def _auto_verdict(detail: str) -> str:
    """"stop" | "ask" | "clean" for `detail`, under the `auto` approval mode.

    REUSES THE VOCABULARY THAT IS ALREADY LIVE rather than inventing a second one:
    relay.autonomy_gate's _STOP_PATTERNS/_ASK_PATTERNS are what task_router._static_risk
    already classifies local shell and python payloads with. Two lists that are supposed to
    mean the same thing and are maintained separately is how a gate ends up refusing here and
    allowing there.

    FAILS CLOSED TO "ask". If the vocabulary cannot be imported there is no classifier, and a
    mode that silently became "allow everything" would be `bypass` wearing another name.
    """
    text = str(detail or "")
    try:
        from relay.autonomy_gate import _STOP_PATTERNS, _ASK_PATTERNS, _matches
    except Exception:
        return "ask"
    try:
        if _matches(text, _STOP_PATTERNS):
            return "stop"
        if _matches(text, _ASK_PATTERNS):
            return "ask"
    except Exception:
        return "ask"
    return "clean"


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
    # THE DEMOTION WAS REMOVED, AND THE REASON IS STRUCTURAL RATHER THAN CAUTIOUS.
    #
    # The plan was to demote these regexes to an audit signal once execution moved into a
    # container, on the argument that holding a command for approval when it cannot run here
    # anyway turns an approval queue into noise. That argument is right, and it is already
    # satisfied without any code: a call that IS routed returns from the gateway before local
    # dispatch, so it never reaches this function at all.
    #
    # What the demotion actually did was check whether the SWITCH was on, and demote on that.
    # But the switch being on does not mean this call was routed -- an operator's call, or any
    # call naming a path no container owns, passes through and executes here. So the condition
    # fired precisely for the commands that were still going to run on this machine, which is
    # the opposite of what it was for.
    #
    # Reaching this function is itself the evidence that the command is about to run locally.
    # There is nothing left to demote.

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
        # NAME THE WAY OUT. This said only "until the state recovers or a human approves"
        # and never said how the state recovers, and the state is two files whose names
        # appear nowhere the reader can see. Meanwhile every gated op opens its own approval,
        # so a suspicion nobody knows how to clear becomes a queue nobody can drain: 308 of
        # them accumulated behind exactly this message on 2026-09-08 and stalled the fleet
        # for close to three hours.
        return ("[契約状態が不正 / policy state untrusted] %s。"
                "危険と判定された操作は、状態が回復するか人が承認するまで実行されません。"
                "状態を戻すには、契約ファイル %s を復元するか、正規に終了させて（deactivate_contract）"
                "%s に終了記録を残してください。契約が二度と使われないなら %s を削除すれば"
                "「有効な契約を見たことがある」という記録自体が消えます。"
                " / The policy state could not be trusted, so this operation was NOT executed."
                % (suspect, _CONTRACT_FILE, _retired_file(), _seen_file()))

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

    # ── ask_before: decided by the operator's approval mode ────────────────
    if op_class in ask_before:
        # THREE MODES, NOT TWO. `auto` has been a valid, selectable setting since
        # approval_policy was written and this function read only `bypass`, so an operator who
        # chose it got the manual gate regardless -- a capability with no caller, inside the
        # safety machinery, where nothing looks wrong because the gate still appears.
        #
        # The semantics are task_router.job_gate's, deliberately, so one word means one thing
        # on both paths:
        #
        #   bypass   proceed; ask nobody -- the mode for discarding the list entirely
        #   auto     ESCALATE ONLY: a STOP-pattern detail is refused outright instead of being
        #            put to a human who could approve it; everything else on the list still
        #            asks
        #   default  every occurrence asks a human
        #
        # `auto` DOES NOT DROP THE ask_before LIST, and the first version of it did. CI caught
        # that: `activate_contract(ask_before=["delete"])` then `check_op("delete", "demo
        # scratch file")` returned None, because the classifier read the detail text, found it
        # innocuous, and allowed an operation the operator had explicitly asked to be shown.
        #
        # Approval fatigue -- the reason this mode exists -- lives in task_router's `default`,
        # where EVERY first-seen job class asks, forever, with no list and no end. A contract's
        # ask_before fires only while a contract is active and only for the handful of classes
        # a person wrote down; it is short and deliberate. Fixing the first by discarding the
        # second leaves no mode meaning "decide the routine things for me but keep the promises
        # I made explicitly" -- and `bypass` already exists for those who want the list gone.
        #
        # So here `auto` is strictly at least as strict as `default`: it can refuse where
        # `default` would have asked, and it never allows where `default` would have asked.
        #
        # NEITHER MODE TOUCHES THE STOP_WHEN BRANCH ABOVE, which is an always-on hard stop
        # that engages the fleet kill-switch, and external Skill trust keeps its own
        # exact-digest path.
        mode = "default"
        try:
            from tools.approval_policy import current_approval_mode
            mode = current_approval_mode()
        except Exception:
            mode = "default"
        if mode == "bypass":
            return None
        if mode == "auto" and _auto_verdict(detail) == "stop":
            return (
                f"[自動判定で拒否 / Refused by the automatic classifier] op_class={op_class!r} "
                f"の内容が禁止パターンに一致したため実行しません。承認モードを『毎回確認』に"
                f"変更すれば人間が判断できます。"
                f" / op_class={op_class!r} matched a prohibited pattern and was not executed. "
                f"Switch the approval mode to manual confirmation to have a human decide."
            )
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

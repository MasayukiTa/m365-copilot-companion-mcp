# -*- coding: utf-8 -*-
"""Checking a DONE claim against what the ledger says actually happened.

STEP 3. Steps 1 and 2 put the two halves in place: tools/tool_ledger.py records every tool call
and its outcome, and relay/acceptance_contract.py fixes the terms before the worker starts.
This is where they meet a claim.

THE RULE: A CLAIM IS CHECKED AGAINST THE RECORD, NOT AGAINST ITSELF. Today a DONE is accepted
because the worker said so -- precision 0.718, 11 of 39 wrong on a 40-instance slice -- and the
refuter that exists to catch those reads the worker's own account, because until today there
was nothing else to read.

WHAT THIS ANSWERS, mechanically, with no model call:

  * did the worker run the contract's acceptance command at all?
  * did it run AFTER the last edit, or before it -- a green from before the change proves nothing
  * did it succeed?
  * did the worker edit anything at all? (a DONE with no write is the commonest wrong claim)

WHAT IT DOES NOT DO. It does not promote anything and it does not run commands. It reports a
verdict object. Promotion belongs to the supervisor-owned verifier in step 4, which reruns the
commands itself against a frozen worktree -- because a check that only reads what the worker
happened to run is still trusting the worker's choice of when to run it.

SHADOW FIRST. `assess` is a pure function over records. Nothing here changes an outcome; the
caller records the verdict beside the existing one so the two can be compared over a real run
before anything is gated on it. Switching a gate from permissive to closed without measuring
first is a mistake this repository has already been corrected for.
"""
from __future__ import annotations

import fnmatch
import re

#: A claim can be checked, and the check passed.
SUPPORTED = "SUPPORTED"
#: A claim can be checked, and the record contradicts it.
CONTRADICTED = "CONTRADICTED"
#: The record cannot settle it -- no contract, no ledger entries, nothing to compare.
#: NOT a pass. "We could not check" and "we checked and it was fine" are different answers.
UNVERIFIABLE = "UNVERIFIABLE"

#: Tools whose call means the worktree changed. A DONE with none of these is a claim to have
#: finished work without doing any.
#: Tools that change the workspace by name. NOT the whole story, and it cannot be: a write
#: performed through an exec tool -- open(p,'w') inside run_python, sed -i inside shell_exec --
#: has no tool name of its own. See the no-writes branch in assess(), which is what keeps that
#: blind spot from being reported as a fact about the worker.
WRITE_TOOLS = {
    "write_file", "append_file", "replace_in_file", "multi_edit", "move_path", "copy_path",
    "delete_path", "trash_path", "create_directory", "write_excel", "write_json",
    # ADDED AFTER IT WAS MISSED: 17 calls in the ledger, none of them counted, while the
    # instances that used it were being told nothing in their workspace had changed.
    "edit_and_verify",
}

#: Tools that run something. The acceptance command arrives through one of these.
EXEC_TOOLS = {"shell_exec", "run_python", "pwsh_exec", "pwsh_exec_file",
              "run_in_background", "run_python_in_background"}


def _command_of(call: dict) -> str:
    args = (call or {}).get("args") or {}
    for key in ("command", "code", "cmd", "script"):
        val = args.get(key)
        if isinstance(val, dict):
            val = val.get("text")
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _segments(command: str):
    """The commands inside one recorded call.

    A worker almost never writes the acceptance command as the whole of a line. It chains a `cd`,
    prefixes a `set VAR=value`, or shells out from run_python with an argv list. Each of those
    put the head of the line somewhere other than the binary, and the check read the line as one
    string -- so a real test run was recorded as no test run at all. Measured: pytest ran 21
    times across instances whose contract names pytest, and one was recognised.

    Yields the whole line first, then each piece, so a plain command costs nothing extra.
    """
    text = (command or "").strip()
    if not text:
        return
    yield text

    # SHELL CHAINS. `cd /d <path> && pytest ...`, `a; b`, `x || y`. Splitting on the operators is
    # enough: each piece is then an ordinary command line and gets the ordinary check.
    parts = re.split(r"&&|\|\||;|(?<![|>])\|(?!\|)", text)
    if len(parts) > 1:
        for p in parts:
            p = p.strip()
            if p:
                yield p

    # WINDOWS ENV PREFIX. `set FOO=bar&& cmd` survives the split above only when the shell used
    # `&&`; `set FOO=bar & cmd` and the no-space form do not, so strip a leading set explicitly.
    m = re.match(r"(?i)^\s*set\s+[^=\s]+=[^&]*&+\s*(.+)$", text)
    if m:
        yield m.group(1).strip()

    # ARGV LISTS INSIDE run_python. subprocess.run(["python","-m","pytest","tests/x","-q"]) is a
    # command; the recorded "command" is the surrounding Python source, whose head is `import`.
    for lst in re.findall(r"\[([^\[\]]*?)\]", text):
        items = re.findall(r"['\"]([^'\"]+)['\"]", lst)
        if len(items) >= 2:
            yield " ".join(items)


def matches_check(command: str, check_command: str) -> bool:
    """True when any command inside this call is the acceptance command."""
    for seg in _segments(command):
        if _matches_check(seg, check_command):
            return True
    return False


def _matches_check(command: str, check_command: str) -> bool:
    """Whether a command the worker ran is the acceptance command the contract named.

    Deliberately loose on the tail and strict on the head. A contract saying `pytest -x` is
    satisfied by `pytest -x tests/test_retry.py` -- narrowing to the relevant file is ordinary
    and correct -- but not by `pytest --collect-only`, which runs nothing, nor by `echo pytest`.
    """
    cmd = " ".join((command or "").split()).lower()
    want = " ".join((check_command or "").split()).lower()
    if not cmd or not want:
        return False

    parts = cmd.split()
    # THE BINARY HAS TO BE THE ONE BEING RUN, not merely a word in the line. "in cmd.split()"
    # accepted `echo pytest -x`, which runs no test and prints the name of one. Leading
    # environment assignments and an interpreter prefix are skipped, because `FOO=1 pytest -x`
    # and `python -m pytest -x` are the same act.
    head_want = _basename(want.split()[0])
    idx = 0
    while idx < len(parts) and ("=" in parts[idx] and not parts[idx].startswith("-")):
        idx += 1
    while idx < len(parts) and _basename(parts[idx]) in ("python", "python3", "py", "npx", "-m"):
        idx += 1
    if idx >= len(parts) or _basename(parts[idx]) != head_want:
        return False

    # FLAGS THAT MAKE IT RUN NOTHING. Not exhaustive and not meant to be -- a determined
    # evasion is exactly what step 4's supervisor rerun exists for, since it runs the command
    # itself rather than reading what the worker chose to run. This catches the accident.
    if any(f in parts for f in ("--collect-only", "--co", "--dry-run", "--help", "-h",
                                "--version", "-n0")):
        return False

    if fnmatch.fnmatch(cmd, want + "*"):
        return True
    # The named binary with its flags in some other order; a worker reordering flags is not
    # evading anything. A PACKAGE SELECTOR is matched by kind rather than by text: `go test
    # ./...` names its target in the tail, so narrowing it replaces that token instead of
    # appending one, and requiring the literal `./...` rejected every narrowed run. Seven
    # instances read as "never run" that way in one night, across three languages -- and the
    # docstring above says narrowing is ordinary and correct. What is NOT accepted is dropping
    # the target: a selector must still be answered by a selector.
    for part in want.split()[1:]:
        if part in parts:
            continue
        if _is_selector(part) and any(_is_selector(p) for p in parts):
            continue
        return False
    return True


def _is_selector(token: str) -> bool:
    """A package or path target, as opposed to a flag or a subcommand.

    Only these are matched by kind. Anything else in the acceptance command -- `test`, `-x`,
    `--count=1` -- still has to be there literally, which is what keeps `go build ./...` from
    satisfying a contract that asked for `go test ./...`.
    """
    t = (token or "").strip()
    if not t or t.startswith("-"):
        return False
    return t.startswith("./") or t.startswith("../") or t == "..." or "/..." in t


def _basename(token: str) -> str:
    name = (token or "").replace("\\", "/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".exe") else name


def assess(claim_done: bool, contract: dict, events, now: float = None) -> dict:
    """Compare a DONE claim against the ledger. Pure; never raises.

    `events` is what tool_ledger.for_task returns: [{"call": {...}, "outcome": {...}}].
    """
    try:
        return _assess(claim_done, contract, events)
    except Exception as exc:
        return {"verdict": UNVERIFIABLE, "reasons": ["assessment failed: %s" % type(exc).__name__],
                "evidence": {}, "error": str(exc)[:200]}


def _assess(claim_done, contract, events):
    events = list(events or [])
    reasons = []
    ev = {"tool_calls": len(events)}

    if not claim_done:
        return {"verdict": UNVERIFIABLE, "reasons": ["no DONE was claimed"], "evidence": ev}

    if not events:
        # A DONE with no recorded tool call at all. Either the worker did nothing, or the
        # ledger was not running -- and those must not be reported as the same thing.
        return {"verdict": UNVERIFIABLE,
                "reasons": ["no tool calls recorded for this task; either nothing was done or "
                            "the ledger was not writing -- this is not evidence of success"],
                "evidence": ev}

    writes = [e for e in events if (e["call"].get("tool") in WRITE_TOOLS)
              and (e.get("outcome") or {}).get("ok")]
    ev["writes"] = len(writes)
    last_write_ts = max([w["call"].get("ts", 0) for w in writes], default=0.0)
    ev["last_write_ts"] = last_write_ts

    if not writes:
        # A BLIND SPOT IS NOT A FINDING. run_python and shell_exec can write anything, and no
        # list of tool names will ever see them do it. Nine instances were told "nothing in the
        # workspace changed" on this branch; all nine had exec calls and four of them produced
        # a patch that graded RESOLVED. Where the record cannot settle it, say so.
        if any(e["call"].get("tool") in EXEC_TOOLS for e in events):
            reasons.append("no write tool was called, but commands were run that can write "
                           "(run_python, shell_exec); the ledger cannot see a write made that "
                           "way, so this claim cannot be settled from records")
            return {"verdict": UNVERIFIABLE, "reasons": reasons, "evidence": ev}
        reasons.append("claimed DONE with no successful write and nothing was run that could "
                       "have written; nothing in the workspace changed")
        return {"verdict": CONTRADICTED, "reasons": reasons, "evidence": ev}

    checks = (contract or {}).get("checks") or []
    if not checks:
        # Honest about which of the two situations this is -- see acceptance_contract.
        why = ("the contract records no mechanical check for this task"
               if contract else "no acceptance contract was recorded for this task")
        reasons.append(why + "; the work is real but this claim cannot be settled from records")
        return {"verdict": UNVERIFIABLE, "reasons": reasons, "evidence": ev}

    runs = [e for e in events if e["call"].get("tool") in EXEC_TOOLS]
    matched, after, passed = [], [], []
    for check in checks:
        want = check.get("command") or ""
        for e in runs:
            if not matches_check(_command_of(e["call"]), want):
                continue
            matched.append(e)
            if e["call"].get("ts", 0) >= last_write_ts:
                after.append(e)
                if (e.get("outcome") or {}).get("ok"):
                    passed.append(e)
    ev["check_runs"] = len(matched)
    ev["check_runs_after_last_write"] = len(after)
    ev["check_runs_passed_after_last_write"] = len(passed)

    if not matched:
        reasons.append("the contract's acceptance command was never run: %s"
                       % ", ".join((c.get("command") or "")[:60] for c in checks))
        return {"verdict": CONTRADICTED, "reasons": reasons, "evidence": ev}
    if not after:
        # THE ORDERING CHECK. A green from before the final edit says nothing about the edit.
        reasons.append("the acceptance command ran, but only BEFORE the last write; a result "
                       "from before the change does not test the change")
        return {"verdict": CONTRADICTED, "reasons": reasons, "evidence": ev}
    if not passed:
        reasons.append("the acceptance command ran after the last write and did not succeed")
        return {"verdict": CONTRADICTED, "reasons": reasons, "evidence": ev}

    reasons.append("acceptance command ran after the last write and succeeded")
    return {"verdict": SUPPORTED, "reasons": reasons, "evidence": ev}


def summarise(verdicts) -> dict:
    """Counts over a run, for comparing shadow verdicts against reported outcomes."""
    out = {SUPPORTED: 0, CONTRADICTED: 0, UNVERIFIABLE: 0}
    for v in verdicts or []:
        key = (v or {}).get("verdict")
        if key in out:
            out[key] += 1
    total = sum(out.values())
    out["total"] = total
    # Deliberately NOT called precision. Precision needs the external grade; this is the share
    # of claims the records could support, which is a different and weaker statement.
    out["supported_share"] = (out[SUPPORTED] / total) if total else None
    return out

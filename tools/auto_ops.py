# -*- coding: utf-8 -*-
"""Model-facing wrappers for the atomic edit-verify cell.

WHY THESE EXIST SEPARATELY. tools/auto/autoloop.py is the mechanism and is called in-process,
where the unlock gate has no remote identity to ask about. This is the model-facing door, and
it carries the gates every other executing tool carries.

THE GATE THAT MATTERS MOST HERE. edit_and_verify runs an ARBITRARY COMMAND. Exposing it without
the command-safety net would make it the cheapest way around that net: a caller refused
`shell_exec("rm -rf ...")` could pass the same string as a verification command and have it run.
So the screening is not reimplemented here -- the very functions shell_exec uses are imported
and applied to the same string, so the two cannot drift apart. A new entry point that skips the
existing check is the classic way a guarded system stops being guarded.
"""
from __future__ import annotations

import json

from tools.security import require_unlocked


def _screen(command: str, working_dir: str):
    """Run the command through EXACTLY what shell_exec runs it through. Returns a refusal
    string, or None to proceed."""
    from tools import contract_gate as _cg
    from tools.code_exec import _gate_detail, _judged
    if _cg.destructive_shell(command):
        refusal = _cg.check_op("shell_destructive", _gate_detail(command))
        if refusal is not None:
            return refusal
    return _judged("shell", command, working_dir)


def edit_and_verify(edits: list, verify_command: str = "", repo: str = ".",
                    run_id: str = "", revert_on_fail: bool = True,
                    timeout_s: int = 900) -> str:
    """Apply edits ACROSS FILES atomically, verify, and undo all of them if it does not hold.

    multi_edit is atomic within one file and nothing sits above it, so a change spanning four
    files is four independent writes and a failing test leaves a tree half-edited across a
    boundary no tool can see. This makes the whole set one thing.

    Args:
        edits: [{"path", "old", "new", "expected_replacements"?}, ...] -- paths may be relative
            to `repo`. Every touched file's contents are captured BEFORE the first write.
        verify_command: shell command whose exit status decides, e.g. "pytest -x". Empty means
            the compile check is the whole verification, and the result says so rather than
            implying that tests passed.
        repo: working directory for the edits and the command.
        run_id: when given, each iteration is appended to that run's log so the failure
            trajectory can be read back with loop_trajectory.
        revert_on_fail: leave false ONLY to inspect a broken tree deliberately.
        timeout_s: the command is killed as a process TREE if it overruns.
    """
    locked = require_unlocked()
    if locked:
        return locked
    if not isinstance(edits, list) or not edits:
        return "[edit_and_verify: `edits` must be a non-empty list of {path, old, new}]"
    if verify_command:
        refusal = _screen(verify_command, repo)
        if refusal is not None:
            return refusal
    from tools.auto import autoloop as A
    try:
        result = A.edit_and_verify(edits, verify=verify_command or None, repo=repo,
                                   run_id=run_id, revert_on_fail=bool(revert_on_fail),
                                   timeout_s=float(timeout_s))
    except Exception as exc:
        return "[edit_and_verify error: %s: %s]" % (type(exc).__name__, exc)
    return json.dumps(result, ensure_ascii=False, indent=1)


def loop_trajectory(run_id: str) -> str:
    """What the recorded iterations show: failures falling, flat, rising, or unknown.

    Read back off the log rather than accumulated in memory, so a loop that reports progress it
    did not make is contradicted by its own record. "Flat" is stated explicitly, because a loop
    that is not converging looks busy.
    """
    from tools.auto import autoloop as A
    try:
        return json.dumps(A.trajectory(run_id), ensure_ascii=False, indent=1)
    except Exception as exc:
        return "[loop_trajectory error: %s: %s]" % (type(exc).__name__, exc)


def restore_point(repo: str = ".", snapshot_path: str = "") -> str:
    """Establish a way back BEFORE editing starts. ok=false means do not start.

    A dirty git tree is refused: rolling back would discard work that was already uncommitted
    and is not this session's to throw away. No branch is created -- a recorded commit is a
    restore point that leaves nothing behind.
    """
    locked = require_unlocked()
    if locked:
        return locked
    from tools.auto import autoloop as A
    try:
        return json.dumps(A.restore_point(repo, snapshot_path), ensure_ascii=False, indent=1)
    except Exception as exc:
        return "[restore_point error: %s: %s]" % (type(exc).__name__, exc)


def roll_back(point: dict) -> str:
    """Return the tree to a restore_point. Untracked files are LISTED AND LEFT ALONE.

    Deleting them would make the rollback look complete; it is also how someone's unsaved work
    disappears. A commit made since the restore point stops the rollback rather than being
    discarded silently.
    """
    locked = require_unlocked()
    if locked:
        return locked
    if not isinstance(point, dict):
        return "[roll_back: pass the dict returned by restore_point]"
    from tools.auto import autoloop as A
    try:
        return json.dumps(A.roll_back(point), ensure_ascii=False, indent=1)
    except Exception as exc:
        return "[roll_back error: %s: %s]" % (type(exc).__name__, exc)

# -*- coding: utf-8 -*-
"""Running the acceptance commands ourselves, against a frozen tree, after the worker stops.

STEP 4, AND THE REASON STEP 3 IS NOT ENOUGH. evidence_manifest reads what the worker HAPPENED
to run. That catches the accident -- a test run before the last edit, a DONE with no write --
and it cannot catch a choice: a worker that runs a narrower command, or runs the right command
at the right moment and then edits once more, produces a record that reads correctly.

So the supervisor runs the commands itself:

  1. the worker stops touching the tree
  2. the tree's state is captured -- a hash over the files, so "what was verified" is a fact
     rather than "the directory, at some point"
  3. the CONTRACT'S commands run, not commands chosen now and not commands the worker suggested
  4. the tree is hashed again; if it moved during verification, the result is void

Step 4 is where DONE is decided. Everything before it produces a CANDIDATE.

WHAT IT WILL NOT DO. It does not fix anything. A verifier that repairs what it is verifying has
signed off on its own work, and the independence is the entire value. It also does not ask a
model: the commands are already written down, and their exit codes are not a matter of opinion.

FAILURE IS NOT PROMOTION. A command that cannot be run -- missing binary, timeout, unreadable
tree -- leaves the claim a CANDIDATE. It does not become DONE because the check broke, which is
the same rule the rest of this system runs on.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time

#: The worker's claim, before anything independent has been established.
CANDIDATE_DONE = "CANDIDATE_DONE"
#: Promoted: the contract's commands ran here, after the work, against an unchanged tree.
DONE = "DONE"
#: Ran, and did not pass.
VERIFY_FAILED = "VERIFY_FAILED"
#: Could not be established either way. NOT a promotion.
VERIFY_UNAVAILABLE = "VERIFY_UNAVAILABLE"

#: Anything larger is not source and is not worth hashing on every verification.
_MAX_HASH_BYTES = 2_000_000

#: Directories whose contents change without the work changing.
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".venv", "venv",
              "dist", "build", ".tox", ".mypy_cache", ".ruff_cache"}

DEFAULT_TIMEOUT_S = float(os.environ.get("MCP_VERIFY_TIMEOUT_S", "900"))


def tree_hash(root: str, max_files: int = 20000) -> str:
    """A digest over the source tree. Same tree -> same value; any edit -> a different one.

    Names and sizes and content, sorted, so the result does not depend on walk order. Build
    outputs and caches are skipped: they move on their own and would make every verification
    look like the tree had changed underneath it.
    """
    h = hashlib.sha256()
    count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, root).replace("\\", "/")
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                h.update(rel.encode("utf-8", "replace"))
                h.update(str(size).encode("ascii"))
                if size <= _MAX_HASH_BYTES:
                    try:
                        with open(path, "rb") as fh:
                            h.update(fh.read())
                    except OSError:
                        pass
                count += 1
                if count >= max_files:
                    h.update(b"TRUNCATED")
                    return h.hexdigest()[:32]
    except OSError:
        return ""
    return h.hexdigest()[:32]


#: Output that means the runner never started, rather than that the tests failed. Deliberately
#: about the ENVIRONMENT -- a missing binary, a missing project file, a missing script. Not a
#: complete list and cannot be: what it must never contain is a pattern that a genuine test
#: failure could also print, because that would turn a real failure into "could not check".
_NOT_RUN_MARKERS = (
    "is not recognized as an internal or external command",
    "command not found",
    "no such file or directory",
    "enoent",
    "could not read package.json",
    "npm err! missing script",
    "no test specified",
    "can't open file",
    "no module named pytest",
    "error: file not found",
    "指定されたファイルが見つかりません",
    "認識されていません",

    # COLLECTION NEVER COMPLETED, so no test in the suite was executed. Measured on this
    # benchmark: `pytest -x` in a staged worktree died in 3.5 seconds with
    # "ModuleNotFoundError: No module named 'web'" from the project's own conftest, because the
    # repository's dependencies are not installed in the worktree. Every instance was being
    # recorded VERIFY_FAILED -- a verdict about code that had never been exercised.
    #
    # THE HONEST VERDICT HERE IS "DO NOT KNOW", AND THAT IS WHY THIS IS SAFE. A collection
    # error can also be the patch's own fault, and nothing in the output distinguishes the two
    # cases. Routing both to UNAVAILABLE neither credits nor condemns the patch, which is
    # exactly right when the evidence cannot tell them apart. Calling it a failure asserts
    # something the run did not establish.
    "error during collection",
    "errors during collection",
    "importerror while loading conftest",

    # The command ran and exercised nothing. Not a pass, and not evidence of a defect either.
    "no tests ran",
)


def not_actually_run(output: str) -> bool:
    """Whether the command failed to START, as opposed to running and reporting failure."""
    low = (output or "").lower()
    return any(m in low for m in _NOT_RUN_MARKERS)


def run_check(command: str, cwd: str, timeout_s: float = None):
    """Run one acceptance command. Returns a record; never raises.

    The whole process tree is killed on timeout, for the reason measured elsewhere in this
    repository: killing only the shell leaves grandchildren running, and 40 of them accumulated
    over 33 hours before anyone noticed.
    """
    started = time.time()
    try:
        from tools.code_exec import _run_with_tree_timeout
        out = _run_with_tree_timeout(command, timeout_s or DEFAULT_TIMEOUT_S, cwd)
        ok = ("[timeout" not in out) and ("[returncode:" not in out)
        record = {"command": command, "ok": ok, "output": out[-4000:],
                  "duration_s": round(time.time() - started, 2)}
        if not ok and not_actually_run(out):
            # THE DISTINCTION THE FAILURE TAXONOMY TURNS ON. "the tests failed" and "the test
            # runner never started" are different facts, and only the first is evidence about
            # the work. Measured while wiring this: an absent worktree made `npm test` exit
            # non-zero, which read as VERIFY_FAILED -- a verdict about code that was never run.
            record["unavailable"] = True
            record["why_unavailable"] = "the command did not get as far as running the tests"
        return record
    except Exception as exc:
        return {"command": command, "ok": False,
                "output": "%s: %s" % (type(exc).__name__, str(exc)[:400]),
                "duration_s": round(time.time() - started, 2),
                "unavailable": True}


def verify(contract: dict, cwd: str = "", timeout_s: float = None, runner=None) -> dict:
    """Establish whether a candidate may be promoted. Never raises.

    `runner` is injected so the decision logic can be tested without executing anything -- the
    logic is the part that must not be wrong, and a test that shells out tests the shell.
    """
    result = {"state": VERIFY_UNAVAILABLE, "checks": [], "reasons": [],
              "tree_before": "", "tree_after": ""}
    try:
        if not contract:
            result["reasons"].append("no acceptance contract; nothing independent to run")
            return result
        checks = contract.get("checks") or []
        if not checks:
            # Stated, not inferred: the contract says this task has no mechanical oracle.
            result["reasons"].append(
                "the contract records no mechanical check for this task; a candidate here "
                "cannot be promoted by running anything, and must not be promoted by default")
            return result

        root = cwd or contract.get("cwd") or ""
        if not root or not os.path.isdir(root):
            result["reasons"].append("the working tree %r is not present" % root)
            return result

        result["tree_before"] = tree_hash(root)
        exec_check = runner or (lambda c: run_check(c, root, timeout_s))
        for check in checks:
            record = exec_check(check.get("command") or "")
            record["id"] = check.get("id") or ""
            result["checks"].append(record)
        result["tree_after"] = tree_hash(root)

        if result["tree_before"] and result["tree_before"] != result["tree_after"]:
            # THE TREE MOVED WHILE WE WERE LOOKING. Whatever the commands said, they did not
            # describe a stable state -- and a test that edits the thing it tests is exactly
            # the case that must not be promoted.
            result["state"] = VERIFY_UNAVAILABLE
            result["reasons"].append(
                "the working tree changed during verification; the result does not describe "
                "any single state and cannot promote anything")
            return result

        if any(c.get("unavailable") for c in result["checks"]):
            result["state"] = VERIFY_UNAVAILABLE
            result["reasons"].append("a check could not be run; a broken check is not a pass")
            return result

        if all(c.get("ok") for c in result["checks"]):
            result["state"] = DONE
            result["reasons"].append("every acceptance command ran here and passed")
        else:
            failed = [c.get("id") or c.get("command", "")[:40]
                      for c in result["checks"] if not c.get("ok")]
            result["state"] = VERIFY_FAILED
            result["reasons"].append("acceptance command(s) failed: %s" % ", ".join(failed))
        return result
    except Exception as exc:
        result["state"] = VERIFY_UNAVAILABLE
        result["reasons"].append("verification raised %s" % type(exc).__name__)
        result["error"] = str(exc)[:200]
        return result


def promote(claimed_done: bool, verification: dict) -> str:
    """The final state for one task. The ONLY place DONE is produced.

    A claim is a candidate. It becomes DONE when, and only when, an independent run of the
    contract's own commands passed against the tree the work left behind.
    """
    if not claimed_done:
        return ""
    state = (verification or {}).get("state")
    if state == DONE:
        return DONE
    if state == VERIFY_FAILED:
        return VERIFY_FAILED
    return CANDIDATE_DONE

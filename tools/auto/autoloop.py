# -*- coding: utf-8 -*-
"""Apply a set of edits ACROSS FILES, verify, and put everything back if it does not hold.

THE GAP THIS FILLS. multi_edit is atomic within one file and there is nothing above it. A
change that spans four files is four independent writes, so a failing test leaves a tree that
is half-edited across a boundary no tool can see -- and the next turn starts from a state
nobody chose. Every entry in the plan's "Claude Code difference" table sits on top of this one
missing property.

WHY IT IS A TOOL AND NOT A HABIT. The binding constraint here is the shared tool planner, which
refuses by concurrency: a median 35 concurrent replies at a refusal against 5 at a recovery.
Discovering "it does not compile" or "three tests fail" costs a model turn, and the turn is the
scarce thing. This does the whole apply-check-test-revert cell in one call and hands back the
compact failure, so the turn is spent on the fix rather than on finding out.

WHAT IT DOES NOT DO. It does not invent the fix -- no model runs inside it. The loop is the
caller calling it again; what this contributes is that every iteration starts from a tree that
is either fully changed or fully unchanged, and that the trajectory is on disk rather than in
the caller's account of itself.

REVERT IS THE DEFAULT, and pre-images are captured for EVERY file before the FIRST write. Read
after the first write and the pre-image of file one is already the edited file, which restores
the tree to the broken state while reporting success.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from tools.coding_ops import multi_edit_local, python_check
from tools.gate_ops import stop_check
from tools.runlog_ops import runlog_append_local

#: An iteration that runs longer than this is not converging, it is hung. The number is the
#: repository's own suite plus room: relay/ takes six minutes.
DEFAULT_TIMEOUT_S = 900


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _restore(pre):
    """Put every captured file back. Returns the paths that could NOT be restored.

    A failure here is the worst state this module can reach -- a partly reverted tree -- so it
    is returned rather than swallowed: the caller has to be told which files are wrong.
    """
    bad = []
    for path, blob in pre.items():
        try:
            if blob is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                with open(path, "wb") as fh:
                    fh.write(blob)
        except OSError:
            bad.append(path)
    return bad


def _run(cmd, cwd, timeout_s):
    """Run the verification command and kill the WHOLE TREE if it overruns.

    subprocess timeouts kill the direct child only. That is not a theoretical gap here: a
    benchmark once left forty hung npx processes holding gigabytes, because the shell was
    killed and everything it had started was not.
    """
    shell = isinstance(cmd, str)
    proc = subprocess.Popen(cmd, cwd=cwd, shell=shell, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        return proc.returncode, (out or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        except Exception:
            proc.kill()
        try:
            out, _ = proc.communicate(timeout=30)
        except Exception:
            out = b""
        return None, (out or b"").decode("utf-8", "replace")


def _abs(repo, path):
    return path if os.path.isabs(path) else os.path.abspath(os.path.join(repo, path))


def edit_and_verify(edits, verify=None, repo=".", run_id="", revert_on_fail=True,
                    timeout_s=DEFAULT_TIMEOUT_S):
    """Apply `edits` across files atomically, verify, and revert the whole set on failure.

    edits: [{"path", "old", "new", "expected_replacements"?}, ...]
    verify: a command (list or string) whose exit status decides. None means the syntax check
            is the whole verification, which is STATED in the result rather than implied.

    Returns a dict: ok, stage, applied, reverted, files, check, exit_code, output,
    restore_failed, stopped.
    """
    result = {"ok": False, "stage": "", "applied": [], "reverted": False, "files": [],
              "check": "", "exit_code": None, "output": "", "restore_failed": [],
              "stopped": False}

    switch = stop_check()
    if switch != "RUN":
        # THE KILL SWITCH IS READ BEFORE THE FIRST WRITE, not between iterations. Stopping
        # after the tree has been edited is not stopping.
        result.update(stage="stop", stopped=True, output=switch)
        return _record(run_id, result)

    if not edits:
        result.update(stage="nothing to do", ok=True)
        return _record(run_id, result)

    paths = []
    for e in edits:
        p = _abs(repo, e["path"])
        if p not in paths:
            paths.append(p)
    result["files"] = paths

    # EVERY PRE-IMAGE BEFORE THE FIRST WRITE.
    pre = {}
    for p in paths:
        try:
            pre[p] = _read(p) if os.path.exists(p) else None
        except OSError as exc:
            result.update(stage="pre-image", output="cannot read %s (%s)" % (p, exc))
            return _record(run_id, result)

    for e in edits:
        p = _abs(repo, e["path"])
        # multi_edit_local, NOT multi_edit. The gated tool asks whether the REMOTE caller
        # proved possession of the password for its IP; a caller inside this process has no
        # remote identity for that question, so every call is refused -- and the refusal is a
        # returned STRING. The first version of this line used the gated one, every edit came
        # back "[locked: no HTTP request context]", and the cell would have shipped reverting
        # a tree it had never changed while reporting that it had.
        out = multi_edit_local(p, [{"old": e["old"], "new": e["new"],
                                    "expected_replacements": e.get("expected_replacements", 1)}])
        result["applied"].append({"path": e["path"], "result": out})
        if out.startswith("[multi_edit error") or out.startswith("[multi_edit aborted"):
            # ONE EDIT FAILING UNDOES THE WHOLE SET. A set that half-applies is the state this
            # module exists to make impossible.
            result.update(stage="edit", reverted=True,
                          restore_failed=_restore(pre), output=out)
            return _record(run_id, result)

    for p in paths:
        if p.endswith(".py") and os.path.exists(p):
            chk = python_check(p)
            if not chk.startswith("OK:"):
                result.update(stage="syntax", check=chk, reverted=revert_on_fail,
                              restore_failed=_restore(pre) if revert_on_fail else [])
                return _record(run_id, result)
    result["check"] = "OK"

    if verify is None:
        # SAID, NOT IMPLIED. "compiles" and "passes its tests" are different claims, and a
        # caller reading ok=True has to be able to tell which one it got.
        result.update(ok=True, stage="syntax only (no verification command was given)")
        return _record(run_id, result)

    code, out = _run(verify, repo, timeout_s)
    result["exit_code"] = code
    result["output"] = out[-4000:]
    if code == 0:
        result.update(ok=True, stage="verified")
        return _record(run_id, result)

    result["stage"] = "timeout" if code is None else "verify"
    if revert_on_fail:
        result["reverted"] = True
        result["restore_failed"] = _restore(pre)
    return _record(run_id, result)


def _record(run_id, result):
    """Write the iteration to the append-only runlog.

    IN-PROCESS, so runlog_append_local: the gated tool answers "has the REMOTE caller proved
    possession of the password", which a caller inside this process has no identity for -- and
    every one of the relay's sixteen gated calls was silently refused for exactly that reason.
    """
    if run_id:
        try:
            runlog_append_local(run_id, {
                "kind": "autoloop",
                "ok": result["ok"],
                "stage": result["stage"],
                "reverted": result["reverted"],
                "files": [os.path.basename(p) for p in result["files"]],
                "exit_code": result["exit_code"],
                "fails": count_failures(result["output"]),
            })
        except Exception:
            pass
    return result


def count_failures(output):
    """Failures reported by the verification output, or None when it does not say.

    NONE IS NOT ZERO. A command whose output this cannot parse has an unknown failure count,
    and calling that zero would turn every unrecognised runner into a green trajectory.
    """
    if not output:
        return None
    m = re.search(r"(\d+) failed", output)
    if m:
        return int(m.group(1))
    if re.search(r"^\s*OK\s*$", output, re.M) or re.search(r"\d+ passed", output):
        return 0
    return None


def trajectory(run_id, root=None):
    """What the recorded iterations show: failures going down, flat, worse, or unknown.

    READ BACK FROM THE LOG, not accumulated in memory, so the answer comes from the same place
    a later reader would get it -- and a loop that claims progress it did not make is
    contradicted by its own record.
    """
    rows = [r for r in _read_runlog(run_id, root) if r.get("kind") == "autoloop"]
    counts = [r.get("fails") for r in rows]
    known = [c for c in counts if isinstance(c, int)]
    out = {"iterations": len(counts), "fails": counts, "converged": False,
           "verdict": "no iterations recorded"}
    if not counts:
        return out
    if rows[-1].get("ok"):
        out.update(converged=True, verdict="converged: the last iteration verified")
        return out
    if len(known) < 2:
        out["verdict"] = "unknown: the runner's output did not report a failure count"
        return out
    if known[-1] < known[0]:
        out["verdict"] = "improving: %d -> %d failures" % (known[0], known[-1])
    elif known[-1] == known[0]:
        out["verdict"] = "flat at %d failures -- iterating is not moving it" % known[-1]
    else:
        out["verdict"] = "worse: %d -> %d failures" % (known[0], known[-1])
    return out


def _read_runlog(run_id, root=None):
    from tools import runlog_ops
    # RUNS_DIR, read from the module that owns it rather than repeated here. The first draft
    # of this line said RUNLOG_DIR, a name that does not exist, and would have fallen through
    # to a guessed home directory and read an empty trajectory forever -- reporting "no
    # iterations recorded" for a loop that ran fine.
    base = root or runlog_ops.RUNS_DIR
    path = os.path.join(str(base), "%s.jsonl" % run_id)
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        return []
    return rows


# ── the restore point ─────────────────────────────────────────────────────────────────────
#
# edit_and_verify makes ONE change undoable. A session is many of them, and a sequence that
# each verified can still end up somewhere nobody wanted. The plan calls for a fixed guard
# before any of it starts; this is that guard, and it FAILS CLOSED -- if the tree cannot be
# put back, editing does not begin.
#
# TWO THINGS IT DELIBERATELY DOES NOT DO.
#
# It does not create a branch per run. Branch and worktree sprawl is a standing problem in this
# repository, and a recorded commit is a restore point without leaving anything behind.
#
# It does not delete untracked files, ever. `git clean` would make rollback "complete", and it
# is also how someone's unsaved work disappears. Untracked files are REPORTED and left where
# they are; a rollback that says what it did not undo is honest, one that quietly removes
# things is not.


def _git(args, repo):
    try:
        out = subprocess.run(["git"] + args, cwd=repo, capture_output=True, text=True,
                             timeout=120)
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except Exception as exc:
        return 1, str(exc)


def restore_point(repo=".", snapshot_path=""):
    """Establish a way back before any editing starts. Returns a dict; ok=False means do not
    start.

    A DIRTY GIT TREE IS REFUSED. Rolling back would discard whatever was already uncommitted,
    which is someone else's work, not this session's. Commit or stash it first -- the guard
    will not make that choice on anyone's behalf.
    """
    repo = os.path.abspath(repo)
    point = {"ok": False, "kind": "", "repo": repo, "head": "", "snapshot": "", "why": ""}

    code, head = _git(["rev-parse", "HEAD"], repo)
    if code == 0:
        code, status = _git(["status", "--porcelain"], repo)
        if code != 0:
            point["why"] = "git status failed: %s" % status.strip()[:200]
            return point
        dirty = [l for l in status.splitlines() if l.strip() and not l.startswith("??")]
        if dirty:
            point["why"] = ("the tree has %d uncommitted change(s); commit or stash them first "
                            "-- a rollback here would discard work this session did not do"
                            % len(dirty))
            return point
        point.update(ok=True, kind="git", head=head.strip())
        return point

    if not snapshot_path:
        point["why"] = ("not a git repository and no snapshot_path was given; there would be "
                        "no way back")
        return point
    from tools.archive_ops import zip_create
    out = zip_create(snapshot_path, [repo])
    if out.startswith("["):
        point["why"] = "snapshot failed: %s" % out[:200]
        return point
    point.update(ok=True, kind="zip", snapshot=snapshot_path)
    return point


def roll_back(point):
    """Return the tree to `point`. Returns a dict with what was undone and what was NOT.

    `untracked` is the honest part: files that appeared since the restore point are listed and
    LEFT ALONE. Deleting them would make the rollback look complete and is how unsaved work
    disappears.
    """
    out = {"ok": False, "kind": point.get("kind", ""), "untracked": [], "why": ""}
    if not point.get("ok"):
        out["why"] = "there is no restore point to go back to"
        return out

    if point["kind"] == "git":
        repo = point["repo"]
        code, msg = _git(["checkout", "--", "."], repo)
        if code != 0:
            out["why"] = msg.strip()[:300]
            return out
        _c, status = _git(["status", "--porcelain"], repo)
        out["untracked"] = [l[3:] for l in status.splitlines() if l.startswith("??")]
        _c, head = _git(["rev-parse", "HEAD"], repo)
        out["ok"] = head.strip() == point["head"]
        if not out["ok"]:
            out["why"] = ("HEAD moved to %s since the restore point at %s -- something "
                          "committed during the session, and discarding that is not this "
                          "function's decision" % (head.strip()[:12], point["head"][:12]))
        return out

    out["why"] = ("the snapshot at %s has to be restored deliberately; unzipping over a live "
                  "tree is not something this does on its own" % point.get("snapshot"))
    return out

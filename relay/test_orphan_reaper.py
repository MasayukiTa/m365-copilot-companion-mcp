"""Killing only what this fleet left behind.

On 2026-08-30 seventeen npx processes were still running fourteen hours after their runs
ended. They held 672 MB and, worse, they held the worktree files open: git worktree remove
failed, the capture step left husks resolving to the harness's own repository, and the
free-disk figure the fleet admits work against was wrong by six checkouts.

The operator's rule is not "no killing" -- it is "do not touch a process you did not start".
So every test here is about provenance, never about size.
"""
import json
import time

import pytest

from relay import orphan_reaper as OR


class _Run:
    def __init__(self, payload):
        self.stdout = json.dumps(payload)
        self.returncode = 0


def _proc(pid, cmd, age_s):
    t = time.strftime("%Y%m%d%H%M%S", time.localtime(time.time() - age_s))
    return {"ProcessId": pid, "CreationDate": t + ".000000+540", "CommandLine": cmd}


def test_only_processes_inside_this_runs_work_tree_are_candidates(monkeypatch, tmp_path):
    root = str(tmp_path / "work")
    rows = [_proc(1, "npx tsc --noEmit " + root + r"\p00\\x", 10000),
            _proc(2, r"npx tsc --noEmit C:\somebody\else\p00", 10000)]
    monkeypatch.setattr(OR.subprocess, "run", lambda *a, **k: _Run(rows))
    got = [p for p, _, _ in OR.candidates(min_age_s=60, work_root=root)]
    assert got == [1], "a process outside the work tree was treated as ours"


def test_a_young_process_is_left_alone(monkeypatch, tmp_path):
    """A live build looks exactly like an orphan except for its age."""
    root = str(tmp_path / "work")
    rows = [_proc(3, "npx jest " + root + r"\p01", 30)]
    monkeypatch.setattr(OR.subprocess, "run", lambda *a, **k: _Run(rows))
    assert OR.candidates(min_age_s=3600, work_root=root) == []


def test_size_is_never_the_test():
    """A large process that belongs to somebody else is not this module's business.

    Asserted against the CODE with comments and docstrings removed. The first version checked
    the raw source and failed on its own explanation -- the same trap this repository already
    carries a rule about, and the second time it has been walked into today."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(OR))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:]
    code = ast.unparse(tree)
    for bad in ("WorkingSet", "WorkingSet64", "PrivateMemorySize"):
        assert bad not in code, "the reaper started judging by resource usage: %s" % bad


def test_it_reports_before_it_kills(monkeypatch, tmp_path):
    """A reaper nobody can audit is the kind that eventually takes the wrong process."""
    root = str(tmp_path / "work")
    rows = [_proc(4, "npx mocha " + root + r"\p02", 20000)]
    monkeypatch.setattr(OR.subprocess, "run", lambda *a, **k: _Run(rows))
    r = OR.reap(min_age_s=60, dry_run=True, work_root=root)
    assert r["dry_run"] is True and r["killed"] == []
    assert [f["pid"] for f in r["found"]] == [4]


def test_an_unattributable_process_is_left_alone(monkeypatch, tmp_path):
    """No usable creation time means no age, and no age means no decision."""
    root = str(tmp_path / "work")
    rows = [{"ProcessId": 9, "CreationDate": "", "CommandLine": "npx tsc " + root + r"\p03"}]
    monkeypatch.setattr(OR.subprocess, "run", lambda *a, **k: _Run(rows))
    assert OR.candidates(min_age_s=1, work_root=root) == []


def test_it_never_raises_into_the_caller(monkeypatch):
    """Housekeeping must not be able to fail the thing it is tidying up after."""
    def boom(*a, **k):
        raise OSError("no powershell here")
    monkeypatch.setattr(OR.subprocess, "run", boom)
    assert OR.candidates() == []

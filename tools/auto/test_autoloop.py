# -*- coding: utf-8 -*-
"""Applying a change that spans files, and being able to undo all of it.

THE PROPERTY UNDER TEST. multi_edit is atomic within one file and nothing sits above it, so a
four-file change is four independent writes and a failing test leaves a tree half-edited across
a boundary no tool can see. Every row of the plan's "Claude Code difference" table assumes this
property exists; none of them work without it.

THE SECOND PROPERTY. The trajectory is read back off disk, not accumulated in memory, so a loop
that claims progress is contradicted by its own record rather than believed.
"""
import json
import os

import pytest

from tools.auto import autoloop as A


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """Two files that must change together, and a stop switch that says RUN."""
    (tmp_path / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("from a import VALUE\nUSE = VALUE\n", encoding="utf-8")
    monkeypatch.setattr(A, "stop_check", lambda: "RUN")
    return tmp_path


def both(edits_ok=True):
    return [
        {"path": "a.py", "old": "VALUE = 1", "new": "VALUE = 2"},
        {"path": "b.py", "old": "USE = VALUE" if edits_ok else "NOT PRESENT",
         "new": "USE = VALUE * 2"},
    ]


def text(tree, name):
    return (tree / name).read_text(encoding="utf-8")


# -- all of it, or none of it ------------------------------------------------------------

def test_a_failing_verification_puts_every_file_back(tree):
    """THE WHOLE POINT. Without this the next turn starts from a tree nobody chose."""
    r = A.edit_and_verify(both(), verify=["python", "-c", "raise SystemExit(1)"],
                          repo=str(tree))
    assert r["ok"] is False and r["reverted"] is True and r["restore_failed"] == []
    assert text(tree, "a.py") == "VALUE = 1\n"
    assert "USE = VALUE\n" == text(tree, "b.py").splitlines(True)[1]


def test_one_edit_failing_undoes_the_edits_that_had_already_landed(tree):
    """The first file is written before the second is even attempted. A set that half-applies
    is the state this module exists to make impossible."""
    r = A.edit_and_verify(both(edits_ok=False), verify=None, repo=str(tree))
    assert r["ok"] is False and r["stage"] == "edit" and r["reverted"] is True
    assert text(tree, "a.py") == "VALUE = 1\n", "the first edit was left applied"


def test_a_syntax_error_anywhere_in_the_set_reverts_the_set(tree):
    """The compile check runs before anything is executed, and it covers every touched file,
    not just the one that broke."""
    r = A.edit_and_verify([{"path": "a.py", "old": "VALUE = 1", "new": "VALUE = ("}],
                          verify=["python", "-c", ""], repo=str(tree))
    assert r["stage"] == "syntax" and r["reverted"] is True
    assert text(tree, "a.py") == "VALUE = 1\n"


def test_a_passing_verification_keeps_the_change(tree):
    r = A.edit_and_verify(both(), verify=["python", "-c", ""], repo=str(tree))
    assert r["ok"] is True and r["stage"] == "verified" and r["reverted"] is False
    assert text(tree, "a.py") == "VALUE = 2\n"


def test_revert_can_be_turned_off_deliberately(tree):
    """Sometimes the broken tree is what you want to look at. It has to be asked for."""
    r = A.edit_and_verify(both(), verify=["python", "-c", "raise SystemExit(1)"],
                          repo=str(tree), revert_on_fail=False)
    assert r["ok"] is False and r["reverted"] is False
    assert text(tree, "a.py") == "VALUE = 2\n"


def test_pre_images_are_taken_before_the_first_write(tree, monkeypatch):
    """READ AFTER THE FIRST WRITE and the pre-image of file one is already the edited file --
    the revert would then restore the broken state while reporting success. This asserts the
    ordering directly, because the bug it prevents looks like a clean pass."""
    order = []
    real_read, real_edit = A._read, A.multi_edit_local
    monkeypatch.setattr(A, "_read", lambda p: (order.append(("read", p)), real_read(p))[1])
    monkeypatch.setattr(A, "multi_edit_local",
                        lambda p, e: (order.append(("write", p)), real_edit(p, e))[1])
    A.edit_and_verify(both(), verify=["python", "-c", "raise SystemExit(1)"], repo=str(tree))
    kinds = [k for k, _ in order]
    assert kinds.index("write") > max(i for i, k in enumerate(kinds) if k == "read")


def test_a_file_that_did_not_exist_is_removed_again_on_revert(tree):
    """Restoring "it was not there" means deleting it, not writing an empty file."""
    A.edit_and_verify([{"path": "a.py", "old": "VALUE = 1", "new": "VALUE = 2"}],
                      verify=["python", "-c", "raise SystemExit(1)"], repo=str(tree))
    pre = {str(tree / "new.py"): None}
    (tree / "new.py").write_text("x", encoding="utf-8")
    assert A._restore(pre) == []
    assert not (tree / "new.py").exists()


def test_a_restore_that_fails_is_reported_not_swallowed(tree, monkeypatch):
    """A partly reverted tree is the worst state this module can reach. Silence about it is
    how a caller carries on editing a file it thinks is clean."""
    monkeypatch.setattr(A, "_restore", lambda pre: ["a.py"])
    r = A.edit_and_verify(both(), verify=["python", "-c", "raise SystemExit(1)"],
                          repo=str(tree))
    assert r["restore_failed"] == ["a.py"]


# -- the stop switch ---------------------------------------------------------------------

def test_the_switch_is_read_before_the_first_write(tree, monkeypatch):
    """Stopping after the tree has been edited is not stopping."""
    monkeypatch.setattr(A, "stop_check", lambda: "STOP (reason: operator)")
    r = A.edit_and_verify(both(), verify=None, repo=str(tree))
    assert r["stopped"] is True and r["stage"] == "stop"
    assert text(tree, "a.py") == "VALUE = 1\n"


# -- what ok actually claims -------------------------------------------------------------

def test_compiling_is_not_reported_as_passing_tests(tree):
    """"It compiles" and "it passes its tests" are different claims. A caller that reads ok=True
    has to be able to tell which one it was given, or the weaker claim silently becomes DONE --
    which is the exact defect the whole verification pipeline exists to stop."""
    r = A.edit_and_verify(both(), verify=None, repo=str(tree))
    assert r["ok"] is True
    assert "no verification command" in r["stage"]


def test_a_hung_verification_is_not_a_pass(tree):
    r = A.edit_and_verify(both(), verify=["python", "-c", "import time; time.sleep(30)"],
                          repo=str(tree), timeout_s=2)
    assert r["ok"] is False and r["stage"] == "timeout" and r["exit_code"] is None
    assert text(tree, "a.py") == "VALUE = 1\n"


# -- counting failures -------------------------------------------------------------------

def test_an_unparsable_output_is_unknown_rather_than_zero():
    """NONE IS NOT ZERO. Calling an unrecognised runner's output zero turns every one of them
    into a green trajectory."""
    assert A.count_failures("some tool printed something else") is None
    assert A.count_failures("") is None


def test_the_usual_runners_are_read():
    assert A.count_failures("3 failed, 5 passed in 2s") == 3
    assert A.count_failures("120 passed in 4s") == 0
    assert A.count_failures("Ran 12 tests\n\nOK\n") == 0


# -- the trajectory ----------------------------------------------------------------------

@pytest.fixture
def runs(tmp_path, monkeypatch):
    from tools import runlog_ops
    monkeypatch.setattr(runlog_ops, "RUNS_DIR", tmp_path, raising=False)
    return tmp_path


def write_rows(runs, run_id, rows):
    with open(os.path.join(str(runs), "%s.jsonl" % run_id), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_a_falling_failure_count_reads_as_improving(runs):
    write_rows(runs, "r1", [{"kind": "autoloop", "fails": 7, "ok": False},
                            {"kind": "autoloop", "fails": 2, "ok": False}])
    assert "improving: 7 -> 2" in A.trajectory("r1")["verdict"]


def test_a_flat_count_says_so_instead_of_looking_busy(runs):
    """The plan's completion criterion is a DECREASING trajectory. A loop stuck at the same
    number is the case worth naming: iterating is not moving it."""
    write_rows(runs, "r2", [{"kind": "autoloop", "fails": 3, "ok": False}] * 4)
    assert "flat at 3" in A.trajectory("r2")["verdict"]


def test_a_rising_count_is_not_hidden(runs):
    write_rows(runs, "r3", [{"kind": "autoloop", "fails": 1, "ok": False},
                            {"kind": "autoloop", "fails": 6, "ok": False}])
    assert "worse: 1 -> 6" in A.trajectory("r3")["verdict"]


def test_a_verified_last_iteration_is_converged(runs):
    write_rows(runs, "r4", [{"kind": "autoloop", "fails": 3, "ok": False},
                            {"kind": "autoloop", "fails": None, "ok": True}])
    t = A.trajectory("r4")
    assert t["converged"] is True and "converged" in t["verdict"]


def test_unknown_counts_do_not_become_a_verdict(runs):
    write_rows(runs, "r5", [{"kind": "autoloop", "fails": None, "ok": False}] * 3)
    assert A.trajectory("r5")["verdict"].startswith("unknown")


def test_a_run_that_never_recorded_says_so(runs):
    assert A.trajectory("missing")["iterations"] == 0


def test_iterations_reach_the_log_the_reader_will_open(tree, runs):
    """END TO END, and the point of it: the record a later reader gets is the one the loop
    wrote, not a summary the loop kept in memory about itself."""
    A.edit_and_verify(both(), verify=["python", "-c", "raise SystemExit(1)"],
                      repo=str(tree), run_id="live1")
    A.edit_and_verify(both(), verify=["python", "-c", ""], repo=str(tree), run_id="live1")
    t = A.trajectory("live1")
    assert t["iterations"] == 2 and t["converged"] is True


def test_the_runlog_constant_is_the_one_the_module_owns():
    """The first draft of the reader named RUNLOG_DIR, which does not exist -- it would have
    fallen through to a guessed directory and reported "no iterations recorded" forever, for a
    loop that ran perfectly well."""
    from tools import runlog_ops
    assert hasattr(runlog_ops, "RUNS_DIR")
    assert not hasattr(runlog_ops, "RUNLOG_DIR")


def test_the_cell_does_not_call_a_gated_tool_in_process():
    """THE DEFECT THAT NEARLY SHIPPED, and its whole class. Tools gated by require_unlocked
    return a refusal STRING when called in-process, so a caller that does not inspect the
    return carries on as if the write happened. Every edit in the first draft came back
    "[locked: no HTTP request context]" and the cell reverted a tree it had never changed.

    The relay hit the identical thing at sixteen call sites and its audit log was empty for
    six weeks before anyone noticed."""
    from tools.coding_ops import multi_edit_local
    import inspect
    assert "require_unlocked" not in inspect.getsource(multi_edit_local)
    assert "multi_edit_local" in inspect.getsource(A.edit_and_verify)


def test_an_in_process_edit_actually_reaches_the_disk(tmp_path, monkeypatch):
    """The assertion that would have caught it with no knowledge of gates at all: after a
    successful call, the bytes on disk are different."""
    monkeypatch.setattr(A, "stop_check", lambda: "RUN")
    (tmp_path / "x.py").write_text("N = 1\n", encoding="utf-8")
    r = A.edit_and_verify([{"path": "x.py", "old": "N = 1", "new": "N = 2"}],
                          verify=None, repo=str(tmp_path))
    assert r["ok"] is True
    assert (tmp_path / "x.py").read_text(encoding="utf-8") == "N = 2\n"


# -- the restore point -------------------------------------------------------------------

def git(tmp, *args):
    import subprocess
    return subprocess.run(["git"] + list(args), cwd=str(tmp), capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("N = 1\n", encoding="utf-8")
    git(tmp_path, "add", "a.py")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def test_a_clean_repo_gives_a_restore_point(repo):
    p = A.restore_point(str(repo))
    assert p["ok"] is True and p["kind"] == "git" and len(p["head"]) >= 7


def test_a_dirty_tree_is_refused_rather_than_rolled_over(repo):
    """FAIL CLOSED, and for a specific reason: rolling back would discard whatever was already
    uncommitted, which is someone else's work, not this session's. The guard does not make
    that choice on anyone's behalf."""
    (repo / "a.py").write_text("N = 999\n", encoding="utf-8")
    p = A.restore_point(str(repo))
    assert p["ok"] is False
    assert "uncommitted" in p["why"]


def test_untracked_files_alone_do_not_block(repo):
    """An untracked file is not work this session would destroy by rolling back tracked
    changes, so it is not a reason to refuse to start."""
    (repo / "scratch.txt").write_text("notes", encoding="utf-8")
    assert A.restore_point(str(repo))["ok"] is True


def test_a_non_git_directory_without_a_snapshot_is_refused(tmp_path):
    """No way back means do not start. This is the whole point of the guard."""
    p = A.restore_point(str(tmp_path))
    assert p["ok"] is False and "no way back" in p["why"]


def test_rolling_back_undoes_the_edits(repo):
    p = A.restore_point(str(repo))
    A.edit_and_verify([{"path": "a.py", "old": "N = 1", "new": "N = 2"}],
                      verify=None, repo=str(repo))
    assert (repo / "a.py").read_text(encoding="utf-8") == "N = 2\n"
    r = A.roll_back(p)
    assert r["ok"] is True
    assert (repo / "a.py").read_text(encoding="utf-8") == "N = 1\n"


def test_untracked_files_are_reported_and_left_alone(repo):
    """`git clean` would make the rollback look complete. It is also how unsaved work
    disappears. Listing what was NOT undone is the honest version."""
    p = A.restore_point(str(repo))
    (repo / "new_thing.txt").write_text("someone's work", encoding="utf-8")
    r = A.roll_back(p)
    assert "new_thing.txt" in r["untracked"]
    assert (repo / "new_thing.txt").exists()


def test_a_commit_made_during_the_session_stops_the_rollback(repo):
    """Discarding a commit is not this function's decision to make silently."""
    p = A.restore_point(str(repo))
    (repo / "a.py").write_text("N = 3\n", encoding="utf-8")
    git(repo, "add", "a.py")
    git(repo, "commit", "-qm", "during")
    r = A.roll_back(p)
    assert r["ok"] is False and "HEAD moved" in r["why"]


def test_rolling_back_without_a_point_is_refused(repo):
    assert A.roll_back({"ok": False})["ok"] is False


def test_the_guard_creates_no_branches(repo):
    """Branch and worktree sprawl is a standing problem here. A recorded commit is a restore
    point that leaves nothing behind."""
    before = git(repo, "branch", "--list").stdout
    A.restore_point(str(repo))
    assert git(repo, "branch", "--list").stdout == before

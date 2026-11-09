# -*- coding: utf-8 -*-
"""Unit tests for scripts/agents_janitor.py.

The janitor deletes a finished worker clone only when ALL four conditions hold. Each test
flips exactly one condition and asserts the folder is KEPT, then a final test asserts a clone
that satisfies all four is deleted under --apply and left alone under dry run. git is driven
for real against throwaway repos in a tmp dir, so the push/dirty checks exercise the same code
path production does. No Windows-only assumptions: paths go through os.path and git behaves the
same on the Linux CI runner.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tools import childproc  # noqa: E402
import agents_janitor as aj  # noqa: E402


def _git(cwd, *args):
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "t")
    env.setdefault("GIT_AUTHOR_EMAIL", "t@t")
    env.setdefault("GIT_COMMITTER_NAME", "t")
    env.setdefault("GIT_COMMITTER_EMAIL", "t@t")
    r = childproc.run(["git", "-C", cwd, *args], capture_output=True, env=env)
    assert r.returncode == 0, (args, r.stderr)
    return r


def _make_remote(tmp_path):
    remote = tmp_path / "remote.git"
    childproc.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
    return remote


def _make_clone(tmp_path, remote, name, *, with_commit=True, old=True):
    """A clone that is clean, fully pushed, and (by default) old -> deletable."""
    clone = tmp_path / name
    childproc.run(["git", "clone", str(remote), str(clone)], capture_output=True, check=True)
    _git(str(clone), "config", "user.email", "t@t")
    _git(str(clone), "config", "user.name", "t")
    if with_commit:
        (clone / "a.txt").write_text("hello")
        _git(str(clone), "add", "a.txt")
        _git(str(clone), "commit", "-m", "init")
        _git(str(clone), "push", "origin", "HEAD:refs/heads/main")
    if old:
        _age(clone)
    return clone


def _age(path, seconds=None):
    """Push every mtime under `path` safely into the past so clause 4 passes.

    bottom-up (topdown=False) so a directory's mtime is set AFTER its children -- setting a
    parent first and then writing a child bumps the parent's mtime back to now on some
    filesystems. Children, then the dir itself, then finally the root.
    """
    old = time.time() - (seconds or aj.QUIET_SECONDS + 3600)
    for dp, dns, fns in os.walk(path, topdown=False):
        for n in list(fns) + list(dns):
            try:
                os.utime(os.path.join(dp, n), (old, old))
            except OSError:
                pass
        try:
            os.utime(dp, (old, old))
        except OSError:
            pass
    os.utime(path, (old, old))


def _eval(clone, tokens=frozenset()):
    return aj.evaluate(str(clone), set(tokens), self_path=str(clone) + "__not_self")


def test_clean_pushed_old_unnamed_is_deletable(tmp_path):
    remote = _make_remote(tmp_path)
    clone = _make_clone(tmp_path, remote, "w1")
    deletable, reason = _eval(clone)
    assert deletable, reason
    assert reason == "deletable"


def test_dirty_working_tree_is_kept(tmp_path):
    remote = _make_remote(tmp_path)
    clone = _make_clone(tmp_path, remote, "w2")
    (clone / "untracked.txt").write_text("x")  # untracked -> porcelain non-empty
    deletable, reason = _eval(clone)
    assert not deletable
    assert reason == "working-tree-dirty"


def test_unpushed_commit_is_kept(tmp_path):
    remote = _make_remote(tmp_path)
    clone = _make_clone(tmp_path, remote, "w3")
    (clone / "b.txt").write_text("local only")
    _git(str(clone), "add", "b.txt")
    _git(str(clone), "commit", "-m", "unpushed")
    _age(clone)
    deletable, reason = _eval(clone)
    assert not deletable
    assert reason == "has-unpushed-commits"


def test_recently_modified_is_kept(tmp_path):
    remote = _make_remote(tmp_path)
    clone = _make_clone(tmp_path, remote, "w4")
    # Touch a tracked file's mtime to now, without changing content (stays clean).
    os.utime(clone / "a.txt", None)
    deletable, reason = _eval(clone)
    assert not deletable
    assert reason == "modified-within-60min"


def test_named_by_live_worker_is_kept(tmp_path):
    remote = _make_remote(tmp_path)
    clone = _make_clone(tmp_path, remote, "w5")
    deletable, reason = aj.evaluate(str(clone), {"run of worker w5 doing things"},
                                    self_path=str(clone) + "__not_self")
    assert not deletable
    assert reason == "named-by-live-worker"


@pytest.mark.parametrize("protected", sorted(aj.PROTECTED_NAMES))
def test_protected_name_is_kept_even_if_deletable(tmp_path, protected):
    """A clone that satisfies all four delete conditions is STILL kept if its name is protected.

    This proves the name guard is independent of git state: build an otherwise-deletable clone
    (clean, pushed, old, unnamed) but give it a protected basename, and it must be refused with
    'protected-name' before any git call.
    """
    remote = _make_remote(tmp_path)
    clone = _make_clone(tmp_path, remote, protected)
    deletable, reason = aj.evaluate(str(clone), set(), self_path=str(clone) + "__not_self")
    assert not deletable
    assert reason == "protected-name"


def test_self_clone_is_kept(tmp_path):
    remote = _make_remote(tmp_path)
    clone = _make_clone(tmp_path, remote, "janitor")
    deletable, reason = aj.evaluate(str(clone), set(), self_path=str(clone))
    assert not deletable
    assert reason == "is-the-janitor-clone"


def test_non_git_folder_is_kept(tmp_path):
    plain = tmp_path / "notrepo"
    plain.mkdir()
    (plain / "f.txt").write_text("x")
    _age(plain)
    deletable, reason = _eval(plain)
    assert not deletable
    assert reason == "not-a-git-repo"


def test_no_remote_is_kept(tmp_path):
    """A repo with commits but no remote has everything unpushed -> keep, not vacuously delete."""
    repo = tmp_path / "lonely"
    repo.mkdir()
    _git(str(repo), "init")
    _git(str(repo), "config", "user.email", "t@t")
    _git(str(repo), "config", "user.name", "t")
    (repo / "a.txt").write_text("x")
    _git(str(repo), "add", "a.txt")
    _git(str(repo), "commit", "-m", "init")
    _age(repo)
    deletable, reason = _eval(repo)
    assert not deletable
    assert reason == "has-unpushed-commits"


def test_unreadable_status_protects_everything(tmp_path):
    bad = tmp_path / "status.json"
    bad.write_text("{ this is not json")
    assert aj.live_worker_names(str(bad)) is None
    remote = _make_remote(tmp_path)
    clone = _make_clone(tmp_path, remote, "w6")
    deletable, reason = aj.evaluate(str(clone), None, self_path=str(clone) + "__x")
    assert not deletable
    assert reason == "status-unreadable"


def test_missing_status_means_no_live_workers(tmp_path):
    missing = tmp_path / "nope" / "status.json"
    assert aj.live_worker_names(str(missing)) == set()


def test_terminal_worker_does_not_protect(tmp_path):
    status = tmp_path / "status.json"
    status.write_text('{"workers": [{"name": "w7", "state": "done"}]}')
    tokens = aj.live_worker_names(str(status))
    assert tokens == set()  # a DONE worker protects nothing


def test_live_worker_token_extracted(tmp_path):
    status = tmp_path / "status.json"
    status.write_text('{"workers": [{"name": "w8", "state": "running", "cwd": "/tmp/x/w8"}]}')
    tokens = aj.live_worker_names(str(status))
    assert "w8" in tokens


def test_dry_run_deletes_nothing(tmp_path):
    root = tmp_path / "agents"
    root.mkdir()
    remote = _make_remote(tmp_path)
    clone = _make_clone(tmp_path, remote, "agents/d1")
    logs = []
    summary = aj.run(apply=False, root=str(root),
                     self_path=str(tmp_path / "self"),
                     status_path=str(tmp_path / "missing.json"), log=logs.append)
    assert os.path.isdir(clone), "dry run must not delete"
    assert any("WOULD-DELETE" in m for m in logs)
    assert summary["free_before"] is not None


def test_apply_deletes_a_finished_clone(tmp_path):
    root = tmp_path / "agents"
    root.mkdir()
    remote = _make_remote(tmp_path)
    clone = _make_clone(tmp_path, remote, "agents/d2")
    logs = []
    summary = aj.run(apply=True, root=str(root),
                     self_path=str(tmp_path / "self"),
                     status_path=str(tmp_path / "missing.json"), log=logs.append)
    assert not os.path.isdir(clone), "apply must delete a finished clone"
    assert any("DELETE" in m for m in logs)
    assert ("d2", "deletable") in summary["deleted"]


def test_apply_keeps_a_dirty_clone(tmp_path):
    root = tmp_path / "agents"
    root.mkdir()
    remote = _make_remote(tmp_path)
    clone = _make_clone(tmp_path, remote, "agents/d3")
    (clone / "dirty.txt").write_text("uncommitted")
    logs = []
    aj.run(apply=True, root=str(root),
           self_path=str(tmp_path / "self"),
           status_path=str(tmp_path / "missing.json"), log=logs.append)
    assert os.path.isdir(clone), "apply must never delete uncommitted work"


def test_runs_as_a_script_from_another_cwd(tmp_path):
    """The supervisor runs this file as `python scripts/agents_janitor.py` with the working
    directory and sys.path[0] = scripts/ (scripts/supervisor.ps1, Invoke-AgentsJanitorSweep).
    In that shape the repo root is NOT on sys.path, so a bare `from tools import childproc`
    raised ModuleNotFoundError and the whole sweep was error output (measured 2026-11-10);
    pytest hid it because it puts the repo root on sys.path. This runs the script the way the
    supervisor does -- a dry run, in a subprocess, with -I (ignore env/user site, so the only
    thing that can make `tools` importable is the fix in the script itself) and -X utf8, from a
    DIFFERENT working directory -- and asserts it exits 0 and prints the DRY-RUN line.

    The agents root is pointed at an empty tmp dir via --root so the script does not walk the
    real agents directory, keeping the test hermetic and fast. No Windows-only assumptions: the
    script, the flags and the paths all behave the same on the Linux CI runner.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(aj.__file__)))
    script = os.path.join(repo_root, "scripts", "agents_janitor.py")
    empty_root = tmp_path / "agents_empty"
    empty_root.mkdir()
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = childproc.run(
        [sys.executable, "-I", "-X", "utf8", script, "--root", str(empty_root)],
        capture_output=True, cwd=str(other_cwd), env=env, timeout=120,
    )
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, out
    assert "[agents-janitor] DRY-RUN" in out, out
    assert "ModuleNotFoundError" not in out, out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

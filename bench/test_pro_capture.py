"""What may be written into a predictions file as an instance's patch.

The worktrees are git worktrees and the cleanup is rmtree(ignore_errors=True). On Windows that
removes the checked-out files and fails silently on the locked `.git` entry, leaving a
directory containing nothing but a pointer into the main repository. `git -C <that>` resolves
to THE HARNESS'S OWN REPOSITORY, so the capture step would write whatever was uncommitted in
it into the predictions file as that instance's patch -- and a grader would score it.

Measured on the live tree: all forty surviving worktrees reported this repository's latest
commit as HEAD and a dirty count of 36, which was the checkout being edited at the time.
"""
import os
import subprocess

import pytest


def _git(*args, cwd=None):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _repo(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    _git("init", "-q", cwd=str(d))
    _git("config", "user.email", "t@example.invalid", cwd=str(d))
    _git("config", "user.name", "t", cwd=str(d))
    (d / "a.txt").write_text("one\n", encoding="utf-8")
    _git("add", "-A", cwd=str(d))
    _git("commit", "-qm", "base", cwd=str(d))
    return d


def test_a_husk_resolves_to_its_parent_repository():
    """The property that made this dangerous, stated as a fact about git rather than a guess:
    a directory inside a repository, with no repository of its own, IS that repository as far
    as `git -C` is concerned."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sub = os.path.join(here, "bench")
    top = _git("rev-parse", "--show-toplevel", cwd=sub).stdout.strip()
    assert os.path.normcase(os.path.abspath(top)) != os.path.normcase(os.path.abspath(sub))


def test_the_capture_checks_the_directory_is_the_repository_root(tmp_path):
    """The guard, run directly: a path whose toplevel is not itself must be refused."""
    outer = _repo(tmp_path, "outer")
    inner = outer / "inner"
    inner.mkdir()
    top = _git("rev-parse", "--show-toplevel", cwd=str(inner)).stdout.strip()
    assert os.path.normcase(os.path.abspath(top)) != os.path.normcase(os.path.abspath(str(inner)))


def test_a_real_repository_passes_the_same_check(tmp_path):
    """The guard must not close the door on the case capture exists for."""
    r = _repo(tmp_path, "real")
    top = _git("rev-parse", "--show-toplevel", cwd=str(r)).stdout.strip()
    assert os.path.normcase(os.path.abspath(top)) == os.path.normcase(os.path.abspath(str(r)))


def test_staged_changes_are_invisible_to_a_bare_diff(tmp_path):
    """WHY THE DIFF IS TAKEN AGAINST HEAD. `git diff` alone shows unstaged changes to tracked
    files, so a worker that staged its edit produced nothing -- and nothing is exactly what a
    wrong answer looks like, so the two were indistinguishable."""
    r = _repo(tmp_path, "staged")
    (r / "a.txt").write_text("two\n", encoding="utf-8")
    _git("add", "-A", cwd=str(r))
    assert _git("diff", cwd=str(r)).stdout.strip() == ""
    assert "two" in _git("diff", "HEAD", cwd=str(r)).stdout


def test_an_untracked_new_file_is_invisible_to_both(tmp_path):
    """A fix that adds a file is a fix. Neither diff sees it, so capture lists untracked files
    separately."""
    r = _repo(tmp_path, "untracked")
    (r / "new.py").write_text("x = 1\n", encoding="utf-8")
    assert _git("diff", "HEAD", cwd=str(r)).stdout.strip() == ""
    others = _git("ls-files", "--others", "--exclude-standard", cwd=str(r)).stdout.split()
    assert others == ["new.py"]


def test_the_capture_source_takes_the_diff_against_head_and_lists_untracked():
    import io
    src = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "pro_capture.py"), encoding="utf-8").read()
    assert '"diff", "HEAD"' in src
    assert '"ls-files", "--others", "--exclude-standard"' in src
    assert '"rev-parse", "--show-toplevel"' in src


def test_skipped_instances_are_announced():
    """A skipped instance is one the run did not measure. Silence is how a husk's
    parent-repository diff would have passed for an answer."""
    import io
    src = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "pro_capture.py"), encoding="utf-8").read()
    assert "SKIPPED" in src and "skipped %d" in src

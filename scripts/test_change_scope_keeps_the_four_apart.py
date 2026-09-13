# -*- coding: utf-8 -*-
"""Staging by hand is the control, and staging by hand is where a file gets missed.

`scripts/change_scope.py` exists for one rule this repository enforces by hand: never
`git add -A`; name the files this work changed. The rule is not stylistic. On this machine the
working tree carries nine untracked paths that are neither ignored nor part of the project --
analysis dumps, a security-tooling checkout, scratch output -- and `check_no_identifying_names`
reports 86 of their files as carrying identifying content, with the line that explains the whole
rule: "They are not public, and they are one `git add` from being so."

So the four categories are four different decisions, and conflating them is what `-A` does.
UNTRACKED is never included in the `--add-line` suggestion: a path that git has never seen is a
judgement, not a default.

The git invocation is pinned deterministic (fsmonitor off, GIT_OPTIONAL_LOCKS=0, LANG=C) for the
reasons in the module docstring; a stale index or a localised status word would each produce a
wrong answer that looks like a right one.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts import change_scope as CS  # noqa: E402


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repository, because this module is a git parser and a stub would be testing
    the stub."""
    r = tmp_path / "r"
    r.mkdir()
    if _git(r, "init", "-q").returncode != 0:
        pytest.skip("git is not usable here")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "kept.txt").write_text("one\n", encoding="utf-8")
    _git(r, "add", "kept.txt")
    _git(r, "commit", "-q", "-m", "base")
    monkeypatch.setattr(CS, "REPO", str(r))
    return r


# ── the four categories ───────────────────────────────────────────────────────────────────

def test_a_clean_tree_reports_four_empty_lists(repo):
    """Empty lists rather than missing keys: a caller iterating must not need a guard."""
    s = CS.scope()
    assert set(s) == {"committed", "staged", "unstaged", "untracked"}
    assert all(v == [] for v in s.values()), s


def test_staged_and_unstaged_are_told_apart(repo):
    (repo / "kept.txt").write_text("two\n", encoding="utf-8")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "new.txt")
    s = CS.scope()
    assert s["staged"] == ["new.txt"]
    assert s["unstaged"] == ["kept.txt"]
    assert s["untracked"] == []


def test_a_file_both_staged_and_modified_appears_in_both(repo):
    """THE ONE THAT MATTERS FOR STAGING BY HAND. The index holds one version and the working
    tree another; a commit takes the first, and a reader who sees only "staged" believes the
    second went in."""
    (repo / "kept.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "kept.txt")
    (repo / "kept.txt").write_text("three\n", encoding="utf-8")
    s = CS.scope()
    assert s["staged"] == ["kept.txt"]
    assert s["unstaged"] == ["kept.txt"]


def test_untracked_is_its_own_category(repo):
    (repo / "scratch.txt").write_text("x\n", encoding="utf-8")
    s = CS.scope()
    assert s["untracked"] == ["scratch.txt"]
    assert s["staged"] == [] and s["unstaged"] == []


def test_a_rename_is_reported_by_its_new_name(repo):
    """"old -> new" is what git prints; the new name is the one anybody acts on, and the raw
    string is not a path."""
    _git(repo, "mv", "kept.txt", "moved.txt")
    s = CS.scope()
    assert s["staged"] == ["moved.txt"], s


# ── the suggestion never includes the dangerous category ──────────────────────────────────

def test_the_add_line_never_names_an_untracked_path(repo, capsys):
    """THE POINT OF THE TOOL. `git add -A` here would sweep in directories that carry
    identifying content into a public repository."""
    (repo / "kept.txt").write_text("two\n", encoding="utf-8")
    (repo / "secret_scratch.txt").write_text("x\n", encoding="utf-8")
    CS.main(["--add-line"])
    out = capsys.readouterr().out
    assert "git add kept.txt" in out
    assert "secret_scratch.txt" not in out.split("git add")[1], out
    assert "deliberately NOT included" in out


def test_it_says_so_when_there_is_nothing_to_stage(repo, capsys):
    CS.main(["--add-line"])
    assert "nothing tracked to stage" in capsys.readouterr().out


# ── the determinism the parser depends on ─────────────────────────────────────────────────

def test_git_runs_with_the_ambient_state_switched_off():
    """A stale fsmonitor index, an optional-lock read failure, or a localised status word each
    produce a wrong answer that looks like a right one. Asserted on the source because the
    alternative is reproducing a localised git."""
    import io

    src = io.open(os.path.join(REPO, "scripts", "change_scope.py"), encoding="utf-8").read()
    for token in ("GIT_OPTIONAL_LOCKS", "core.fsmonitor=", 'env["LANG"]'):
        assert token in src, token


def test_a_git_failure_is_an_empty_answer_not_a_crash(repo, monkeypatch):
    """Called from a shell prompt beside real work; it must not raise into whatever is running
    it."""
    monkeypatch.setattr(CS, "REPO", str(repo / "does-not-exist"))
    s = CS.scope()
    assert all(v == [] for v in s.values()), s

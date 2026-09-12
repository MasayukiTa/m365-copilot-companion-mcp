# -*- coding: utf-8 -*-
"""The identity guard enumerated `git ls-files`, so it could only ever speak too late.

MEASURED 2026-09-13 by being caught by it. The sequence that actually happens is

    git add  ->  commit  ->  push  ->  CI runs the guard  ->  it is already public

and that morning an employee id reached a tracked file exactly that way. The guard was correct;
it was standing in the wrong place, and the fix cost a history rewrite and a force push.

TWO REACHES, both before anything is public:

  STAGED     `git diff --cached --name-only`. The last moment before a commit, and what makes
             `python scripts/check_no_identifying_names.py .` usable as the pre-commit check.
             Nothing is staged in CI, so CI behaviour is unchanged.
  UNTRACKED  `git ls-files --others --exclude-standard`. One `git add` away. ADVISORY only:
             the rule protects what becomes PUBLIC, an untracked file is not public, and
             failing on scratch files would train people to silence the check -- which is how
             a guard stops being read. Measured the same day: 86 untracked files in this
             working tree carry the owner's home path.

Every test here builds its OWN git repository in tmp_path. Staging a file with an employee id
into the real index, even briefly, is the thing being guarded against.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import check_no_identifying_names as G  # noqa: E402

#: An employee-id-shaped string that is not the owner's. The guard matches the SHAPE, so a
#: fabricated one exercises it without putting a real identity in a tracked file -- which is
#: the whole point of the file it lives in.
#:
#: COMPOSED, NOT WRITTEN. Spelled as a literal it matches the shape, so the guard refused this
#: very file -- correctly, by its own rule. The alternative was an ALLOWED entry, which is a
#: standing exemption and would hide the next REAL leak in this file too. Building it here
#: leaves no matching literal in the source while the tests still exercise the matcher with
#: the same value. (Same reason this repository composes backslashes with chr(92).)
FAKE_ID = "Z" + "999" + "X" + "1234"
BAD_LINE = "path = r'C:" + chr(92) + "Users" + chr(92) + FAKE_ID + chr(92) + "x.txt'\n"


def _repo(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(d)] + args, check=True, capture_output=True)
    (d / "ok.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(d), "add", "ok.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", "base"], check=True,
                   capture_output=True)
    return d


# ── the reach that matters ────────────────────────────────────────────────────────────────

def test_a_staged_file_is_refused_before_it_is_committed(tmp_path):
    """THE DEFECT. This file would have been reported only after it was pushed."""
    d = _repo(tmp_path)
    (d / "leak.py").write_text(BAD_LINE, encoding="utf-8")
    subprocess.run(["git", "-C", str(d), "add", "leak.py"], check=True, capture_output=True)

    assert G.staged_files(str(d)) == ["leak.py"]
    hits = G.offences(str(d), names=[], files=["leak.py"])
    assert hits, "a staged file carrying an employee-id shape was not reported"


def test_main_fails_on_a_staged_leak(tmp_path, capsys):
    d = _repo(tmp_path)
    (d / "leak.py").write_text(BAD_LINE, encoding="utf-8")
    subprocess.run(["git", "-C", str(d), "add", "leak.py"], check=True, capture_output=True)

    rc = G.main([str(d)])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "STAGED" in out
    assert "history rewrite" in out, (
        "the message does not say why catching it here matters")


def test_a_clean_stage_still_passes(tmp_path, capsys):
    d = _repo(tmp_path)
    (d / "fine.py").write_text("y = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(d), "add", "fine.py"], check=True, capture_output=True)
    assert G.main([str(d)]) == 0


# ── the advisory reach ────────────────────────────────────────────────────────────────────

def test_an_untracked_leak_warns_and_does_not_fail(tmp_path, capsys):
    """Not public, so not a failure. Failing here would train people to silence the check, and
    a guard nobody reads is the failure mode this repository keeps rediscovering."""
    d = _repo(tmp_path)
    (d / "scratch.py").write_text(BAD_LINE, encoding="utf-8")

    rc = G.main([str(d)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "WARNING" in out and "untracked" in out
    assert "one `git add`" in out


def test_an_ignored_file_is_not_even_warned_about(tmp_path, capsys):
    """`.gitignore`d paths are where operational records legitimately live -- .fleet, the gate
    directory, docs/research. Warning about those every run is noise that buries the one line
    that matters."""
    d = _repo(tmp_path)
    (d / ".gitignore").write_text("secrets/\n", encoding="utf-8")
    (d / "secrets").mkdir()
    (d / "secrets" / "notes.py").write_text(BAD_LINE, encoding="utf-8")

    rc = G.main([str(d)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "secrets/notes.py" not in out


# ── what must not have changed ────────────────────────────────────────────────────────────

def test_the_tracked_check_still_fails_the_build(tmp_path, capsys):
    """The reach was widened, not moved. A committed leak must still fail."""
    d = _repo(tmp_path)
    (d / "leak.py").write_text(BAD_LINE, encoding="utf-8")
    subprocess.run(["git", "-C", str(d), "add", "leak.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", "oops"], check=True,
                   capture_output=True)

    rc = G.main([str(d)])
    out = capsys.readouterr().out
    assert rc != 0, out
    assert "TRACKED FILES" in out


def test_a_repository_it_cannot_enumerate_is_not_a_pass(tmp_path):
    """Unchanged and load-bearing: a failed `git ls-files` once yielded an empty list, and an
    empty list reads as 'nothing identifying in 0 tracked files'."""
    with pytest.raises(G.CheckFailed):
        G.tracked_files(str(tmp_path / "not-a-repo"))

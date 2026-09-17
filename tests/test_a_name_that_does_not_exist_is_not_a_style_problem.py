# -*- coding: utf-8 -*-
"""An undefined name is a crash waiting for the branch that reaches it.

WHAT HAPPENED. On 2026-09-17 a block was lifted out of a 700-line main() into a named function
so the wiring could be exercised instead of read. That turned four CLOSURES into four
ARGUMENTS. A closure over `mc_box` resolves when the callback fires; an argument resolves at
the call -- and `mc_box` is assigned 56 lines further down. Every fleet run died at startup:

    UnboundLocalError: local variable 'mc_box' referenced before assignment

The whole hermetic suite was green. It exercised the new function directly -- the half that
became testable -- and nothing calls main(), which is the half the wiring came out of. So the
extraction improved what could be tested and broke what could not, and the only signal was a
queue entry saying a fleet had started for a fleet that had already died.

WHY A LINTER AND NOT A TEST. A test for this particular line would pass the day it was written
and cover nothing else. F821 is the whole class: "this name is not defined", anywhere, in
every tracked file, in under a second. Across the repository it found exactly two other hits
the day it was adopted -- a missing `typing.Any` import and a permanently-empty footer guarded
by `"_SKILL_FOOTER" in globals()`, which is how an undefined name hides in plain sight.

The first test below does not assert that the linter exists. It reconstructs the shape that
shipped and requires the linter to flag it -- because a gate that has never been shown to
catch the thing it was bought for is a gate nobody has checked.
"""
from __future__ import annotations

import io
import os
import shutil
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools.childproc import run as _run          # noqa: E402  -- locale-safe child output

#: The gate runs in CI where ruff is installed explicitly. On a workstation that has not
#: installed it, these skip rather than fail: a red test for a missing developer tool is how a
#: check stops being read.
_RUFF = shutil.which("ruff") or os.path.join(REPO, ".venv", "Scripts", "ruff.exe")


def _ruff(*args):
    if not os.path.isfile(_RUFF):
        pytest.skip("ruff is not installed here; CI installs it explicitly")
    # cwd=REPO so relative paths stay short; the caller below chunks for the same reason.
    return _run([_RUFF, "check", "--select", "F821", "--isolated", *args], cwd=REPO)


def test_the_gate_catches_the_shape_that_actually_shipped(tmp_path):
    """Closures became arguments, and the argument was resolved too early."""
    src = tmp_path / "shipped.py"
    src.write_text(
        "def build(disk_box, ram_box, mc_box, asc_box):\n"
        "    return (disk_box, ram_box, mc_box, asc_box)\n"
        "\n"
        "def main():\n"
        "    disk_box = [1.0]\n"
        "    ram_box = [512.0]\n"
        "    asc_box = [0, 100]\n"
        "    follower = build(disk_box, ram_box, mc_box, asc_box)\n"
        "    mc_box = [3]\n"
        "    return follower, mc_box\n",
        encoding="utf-8")
    out = _ruff(str(src))
    assert out.returncode != 0, "the gate did not object to the defect it exists for"
    assert "F821" in out.stdout and "mc_box" in out.stdout, out.stdout[:800]


def test_the_closure_version_is_not_flagged(tmp_path):
    """THE OTHER HALF, or the test proves only that ruff dislikes something. The code was
    legal for as long as the reference lived inside a function that could not run yet; the
    gate must not object to that, or it would have been switched off long before today."""
    src = tmp_path / "before.py"
    src.write_text(
        "def main():\n"
        "    def set_maxtabs(v):\n"
        "        mc_box[0] = v\n"
        "    cb = set_maxtabs\n"
        "    mc_box = [3]\n"
        "    return cb, mc_box\n",
        encoding="utf-8")
    out = _ruff(str(src))
    assert out.returncode == 0, ("the gate objects to code that was correct: %s"
                                 % out.stdout[:800])


def test_no_tracked_python_file_names_something_undefined():
    """SCOPED TO `git ls-files`. A walk of the working tree sees scratch files, virtualenvs and
    generated output, and the gap between what a local walk sees and what CI sees is exactly
    how a check comes to pass here and fail there."""
    listed = _run(["git", "-C", REPO, "ls-files", "*.py"])
    assert listed.returncode == 0, listed.stderr[:400]
    files = [f.strip() for f in listed.stdout.splitlines() if f.strip()]
    assert files, "no tracked Python files -- this test lost its subject"
    # CHUNKED. Windows caps a command line near 32k characters and 543 paths clear it; the
    # failure arrives as WinError 206, whose message reads as "the file could not be found"
    # and sends you looking for a missing file. CI pipes through `xargs -0`, which chunks for
    # the same reason without anyone having to know it.
    problems = []
    for i in range(0, len(files), 120):
        out = _ruff(*files[i:i + 120])
        if out.returncode != 0:
            problems.append(out.stdout)
    assert not problems, "".join(problems)[-3000:]


def test_ci_runs_this_gate():
    """A gate only the workstation runs protects only the workstation."""
    body = io.open(os.path.join(REPO, ".github", "workflows", "ci.yml"),
                   encoding="utf-8").read()
    assert "--select F821" in body, "CI does not run the undefined-name gate"
    assert "ruff==" in body, "CI installs ruff without pinning it, so the gate can change "\
                             "under the repository without anyone choosing that"

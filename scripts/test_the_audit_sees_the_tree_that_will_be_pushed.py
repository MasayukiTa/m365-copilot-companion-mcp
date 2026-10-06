# -*- coding: utf-8 -*-
"""The manifest audit reads the git INDEX, and preflight is run before `git add`.

WHAT HAPPENED, 2026-09-14. A new test file was written, `python scripts/preflight.py` reported
**all 6 gates passed**, the file was committed and pushed, and CI failed on
`Audit hermetic test manifest` with *"test files missing from CI and EXCLUDED"* -- naming that
exact file. Nothing was broken and nothing was flaky: `discover_tests()` filters against
`git ls-files --cached`, so a file that exists but has not been staged is not in the tree the
audit is auditing.

THE CHECK ALREADY KNEW. It printed the file under *"not tracked yet, so not required yet -- but
required the moment you `git add` them"*, and its own docstring had recorded the same failure
once before: 「実際に CI で落ちた原因はチェックの欠陥ではなく、add する前にチェックを走らせた
手順のほうにある」 -- the cause is the procedure, not the check. So the information was on the
screen, in the right words, and it did not stop the push.

A NOTE THAT IS IGNORED IS NOT A CONTROL. The gate keeps its lenient default, because run by
hand while a test is being written leniency is correct. `preflight` passes `--strict-untracked`,
because preflight's entire contract is *"Everything CI runs, run here"* -- and that is false the
moment the tree it audits is not the tree that gets pushed. In CI the flag changes nothing: a
fresh checkout has no untracked files.
"""
from __future__ import annotations

import io
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import check_ci_test_manifest as M  # noqa: E402


def _run(monkeypatch, capsys, argv, pending):
    """`main(argv)` with `pending` as the untracked set, and everything else genuine."""
    real = M._git

    def fake(*args):
        if args[:2] == ("ls-files", "--others"):
            return set(pending)
        return real(*args)

    monkeypatch.setattr(M, "_git", fake)
    rc = M.main(argv)
    return rc, capsys.readouterr().out


UNTRACKED = "tools/test_a_file_that_was_never_staged.py"


def test_strict_refuses_an_untracked_test_file(monkeypatch, capsys):
    """THE PUSH THAT SHOULD NOT HAVE HAPPENED."""
    rc, out = _run(monkeypatch, capsys, ["--strict-untracked"], [UNTRACKED])
    assert rc == 1, out
    assert UNTRACKED in out
    assert "ERROR" in out


def test_the_default_still_only_notes_it(monkeypatch, capsys):
    """Leniency by hand is deliberate: a test being written is not yet a defect."""
    rc, out = _run(monkeypatch, capsys, [], [UNTRACKED])
    assert rc == 0, out
    assert UNTRACKED in out
    assert "NOTE" in out and "ERROR" not in out


def test_strict_is_silent_when_there_is_nothing_untracked(monkeypatch, capsys):
    """A CI checkout has none, so the flag must be a no-op there rather than a new failure."""
    rc, out = _run(monkeypatch, capsys, ["--strict-untracked"], [])
    assert rc == 0, out
    assert "ERROR" not in out


def test_a_file_that_is_not_a_test_is_not_demanded(monkeypatch, capsys):
    """The untracked set is every untracked file in the repository. Demanding that a scratch
    note or a generated artefact be listed in ci.yml would make the flag unusable."""
    rc, out = _run(monkeypatch, capsys, ["--strict-untracked"],
                   ["notes.md", "tools/helper.py", "outputs/run.json"])
    assert rc == 0, out


# ── and preflight must actually pass the flag ─────────────────────────────────────────────

def test_preflight_runs_the_audit_in_strict_mode():
    """Otherwise this whole file guards a flag nobody sets.

    Read out of preflight's gate table rather than asserted as a substring of the file, so a
    mention in a comment cannot satisfy it -- the failure class one directory over, where a
    scan matched `fleet_toolset` in a comment saying the call site had been REMOVED.
    """
    src = io.open(os.path.join(REPO, "scripts", "preflight.py"), encoding="utf-8").read()
    gate = re.search(
        r'\(\s*"Audit hermetic test manifest"\s*,\s*\[(?P<argv>[^\]]*)\]', src, re.S)
    assert gate, "preflight no longer has a gate by that name"
    argv = gate.group("argv")
    assert "--strict-untracked" in argv, (
        "preflight runs the audit without --strict-untracked, so an untracked test file "
        "passes here and fails in CI -- which is the thing this file is about:\n%s" % argv)

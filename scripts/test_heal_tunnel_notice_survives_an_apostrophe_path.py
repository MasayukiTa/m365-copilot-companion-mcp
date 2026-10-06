# -*- coding: utf-8 -*-
r"""An install path containing an apostrophe must not silently disable tunnel-heal toasts.

ROOT CAUSE (scripts/heal_tunnel.ps1, Send-TunnelHealNotice, fixed 2026-09-24). The function
built a Python one-liner by splicing the repo root straight into a raw-string literal:

    $code = "import sys; sys.path.insert(0, r'$root'); ..."

`r'...'` is a Python raw string delimited by single quotes. Any apostrophe inside `$root`
(e.g. `D:\repos\o'brien-clone`) closes that literal early and leaves the rest of `$root`
as bare, unparsable tokens -- a SyntaxError in the child `python -c` process. Because the
whole `& $py -c $code ...` call sits inside Invoke-TunnelHeal's outer `try { } catch { }`
(see the driver footer below and heal_tunnel.ps1's own structure), that SyntaxError was never
surfaced anywhere: the toast just never appeared, on every machine whose path has one, with no
error in any log this repo writes.

THE FIX. $root is now passed as `sys.argv[1]` -- a separate process argument, not text
substituted into the code string -- and the generated code reads `sys.path.insert(0,
sys.argv[1])` instead of the raw-string splice. No character in a Windows path can break out
of an argv element the way it can break out of a quoted string literal.

WHY A GIT CLONE, AND WHY THE PATH ITSELF NEEDS THE APOSTROPHE. Extracting just the PowerShell
function (as scripts/test_a_silent_death_leaves_its_exit_code.py does for
Get-ServerExitRecord) proves the syntax fix in isolation, but the defect is specifically about
$root -- a real filesystem path -- containing `'`, so the regression test needs an actual
directory whose path has one. This clones this repo (`git clone --local`, per this task's hard
rules -- never operates on the real checkout) into a scratch folder under %TEMP% whose name
itself contains an apostrophe, for a realistic layout (tools/notify_ops.py present, no .venv,
exactly what a fresh clone looks like) -- then overwrites the clone's scripts/heal_tunnel.ps1
with the WORKING-TREE copy before extracting Send-TunnelHealNotice from it, so the test (and
its mutation check) exercise whatever is actually on disk rather than the last commit.

WHAT THIS PROVES, AND WHAT IT DOES NOT. It proves the generated `python -c` child no longer
raises SyntaxError when $root has an apostrophe (see test_notice_call_survives_apostrophe_root
below) and, as a written-down contrast, that the PRE-FIX splice pattern reliably did (see
test_the_old_raw_string_splice_pattern_did_break, which builds that pattern by hand rather than
by reverting the fix -- reverting it is what the mutation check does separately). It does not
open a real toast: tools/notify_ops.notify_desktop no-ops under PYTEST_CURRENT_TEST, which the
child process inherits from this one, so no window is shown to the operator, and the point
being tested (does the child parse cleanly) is unaffected by that no-op happening first.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402  (see: repository ratchet against text=True)

_POWERSHELL = (
    shutil.which("powershell")
    or shutil.which("powershell.exe")
    or (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") else None)
)
_GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not _POWERSHELL or not _GIT,
    reason=(
        "scripts/heal_tunnel.ps1 and this test are Windows/PowerShell/git-only "
        "(os.name=%r, powershell found=%r, git found=%r)" % (os.name, bool(_POWERSHELL), bool(_GIT))
    ),
)


def _extract_braced_block(text: str, start_marker: str) -> str:
    """The balanced-brace block starting at the first '{' at/after `start_marker`.

    Same approach as scripts/test_a_silent_death_leaves_its_exit_code.py -- not
    PowerShell-aware, but there is no unbalanced brace inside a string literal in
    Send-TunnelHealNotice to fool it (checked by hand).
    """
    idx = text.index(start_marker)
    brace_start = text.index("{", idx)
    depth = 0
    for i in range(brace_start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[idx:i + 1]
    raise AssertionError("unbalanced braces extracting block at %r" % (start_marker,))


@pytest.fixture(scope="module")
def apostrophe_clone(tmp_path_factory):
    """A throwaway `git clone --local` of THIS repo's current commit, at a path with a `'`.

    Cleaned up unconditionally: this fixture is module-scoped so every test in this file
    shares one clone (cloning is the slow part; nothing here mutates the clone).

    scripts/heal_tunnel.ps1 IS THEN OVERWRITTEN FROM THE WORKING TREE, deliberately. A plain
    `git clone` gives the last COMMITTED heal_tunnel.ps1, which is right for "does the shipped
    fix work" but wrong for two things this suite needs: testing an edit before it is
    committed, and this file's own mutation check (which edits the on-disk .ps1 and expects
    the very next test run to see it -- a git-history clone would still show the last commit
    and the check would pass for the wrong reason, which is exactly what happened the first
    time this test was written: reverting the fix on disk and rerunning still showed 3 green).
    Everything else in the clone (tools/notify_ops.py, the absence of .venv) still comes from
    git, which is what makes the apostrophe directory a realistic install layout rather than a
    single stray file.
    """
    base = tmp_path_factory.mktemp("heal_apostrophe_base")
    # tmp_path_factory dirs never contain "'" themselves -- the apostrophe has to be on the
    # CLONE'S OWN leaf directory, which git creates for us.
    dest = os.path.join(str(base), "o'brien's-clone")
    proc = childproc.run(
        [_GIT, "clone", "--quiet", "--local", "--no-hardlinks", REPO, dest],
        timeout=120,
        creationflags=childproc.headless_creationflags(),
    )
    assert proc.returncode == 0, (
        "throwaway git clone failed\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (proc.stdout, proc.stderr)
    )
    assert "'" in dest, "test setup bug: clone path does not actually contain an apostrophe"
    shutil.copyfile(
        os.path.join(REPO, "scripts", "heal_tunnel.ps1"),
        os.path.join(dest, "scripts", "heal_tunnel.ps1"),
    )
    try:
        yield dest
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def _strip_ps_comment_lines(text: str) -> str:
    """Drop every line that is, after leading whitespace, a '#' comment.

    Same helper as scripts/test_a_silent_death_leaves_its_exit_code.py, duplicated rather than
    imported (this repo has no shared PowerShell-source-probe module; each test that needs it
    keeps its own copy, matching the existing pattern). Needed here specifically because this
    file's own header comment explains the fixed function by quoting its PRE-FIX code
    (`` r'$root' ``) -- without stripping comments first, the source check below would match
    that explanation instead of real code, the exact class of false positive
    conftest.py's code_only() was written to avoid for Python.
    """
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


@pytest.fixture(scope="module")
def notice_function_from_clone(apostrophe_clone: str) -> str:
    heal_path = os.path.join(apostrophe_clone, "scripts", "heal_tunnel.ps1")
    with open(heal_path, "r", encoding="utf-8") as fh:
        source = fh.read()
    return _extract_braced_block(source, "function Send-TunnelHealNotice")


def test_the_fix_is_present_in_the_cloned_source(notice_function_from_clone: str):
    """Source-level guard: the raw-string splice must be gone, and the argument-passing form
    must be present. Written against the CLONE (i.e. the committed state this task leaves
    behind), not the working tree, so it also catches "fixed the file but forgot to commit"."""
    code_only = _strip_ps_comment_lines(notice_function_from_clone)
    assert "r'$root'" not in code_only, (
        "Send-TunnelHealNotice still splices $root into a Python raw-string literal -- "
        "this is exactly the pattern an apostrophe in the path breaks"
    )
    assert "sys.argv[1]" in code_only, (
        "Send-TunnelHealNotice no longer passes $root as sys.argv[1]"
    )
    assert "-c $code $root" in code_only, (
        "Send-TunnelHealNotice no longer passes $root as a separate process argument to python"
    )


_DRIVER_HEADER = '''param(
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][string]$OutFile
)

$ErrorActionPreference = "Continue"
$root = $Root
'''

# Deliberately a plain (non-f, non-.format) string: concatenated around PowerShell text full
# of literal '{'/'}' that must not be read as Python format placeholders.
_DRIVER_FOOTER = r'''
Send-TunnelHealNotice "Apostrophe path test" "Body text, no apostrophe here."
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 0 }
Set-Content -Path $OutFile -Value $code -Encoding ASCII
'''


def test_notice_call_survives_apostrophe_root(notice_function_from_clone: str, apostrophe_clone: str, tmp_path):
    """RUNTIME CHECK, against the real cloned source, with a real apostrophe-bearing $root.

    Send-TunnelHealNotice resolves $py to <$root>\\.venv\\Scripts\\python.exe, which does not
    exist in this bare clone (no venv was built here), so it falls back to "python" on PATH --
    the exact fallback branch real machines hit before setup.bat runs. The child's exit code
    (captured via $LASTEXITCODE right after the call, before anything else can reset it) must
    be 0: any non-zero code here means the generated `python -c` text failed to parse or run,
    which is precisely what the apostrophe used to cause.
    """
    driver_path = os.path.join(str(tmp_path), "driver.ps1")
    out_path = os.path.join(str(tmp_path), "out.txt")
    driver_text = _DRIVER_HEADER + "\n\n" + notice_function_from_clone + "\n\n" + _DRIVER_FOOTER
    with open(driver_path, "w", encoding="utf-8") as fh:
        fh.write(driver_text)

    proc = childproc.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver_path,
         "-Root", apostrophe_clone, "-OutFile", out_path],
        timeout=60,
        creationflags=childproc.headless_creationflags(),
    )
    assert proc.returncode == 0, (
        "driver powershell exited %s\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (proc.returncode, proc.stdout, proc.stderr)
    )
    assert os.path.isfile(out_path), "driver did not reach Set-Content -- see stdout/stderr above"
    with open(out_path, "r", encoding="ascii") as fh:
        child_exit_code = fh.read().strip()
    assert child_exit_code == "0", (
        "the `python -c` child launched by Send-TunnelHealNotice exited %r for an "
        "apostrophe-bearing $root=%r -- the fix regressed" % (child_exit_code, apostrophe_clone)
    )


def test_the_old_raw_string_splice_pattern_did_break(apostrophe_clone: str, tmp_path):
    """Written-down proof that the PRE-FIX pattern really was broken by an apostrophe, so the
    passing test above is not vacuously true. Builds the OLD splice by hand (mirroring exactly
    what git history shows for this line) rather than reverting the real fix -- the mutation
    check covers "does reverting the real fix turn this file red" separately; this is "is the
    thing we are testing for actually a real Python SyntaxError, in isolation".
    """
    py = shutil.which("python") or shutil.which("python.exe") or sys.executable
    old_style_code = "import sys; sys.path.insert(0, r'%s'); print('unreachable')" % apostrophe_clone
    proc = childproc.run(
        [py, "-c", old_style_code],
        timeout=20,
        creationflags=childproc.headless_creationflags(),
    )
    assert proc.returncode != 0, (
        "test setup bug: the pre-fix raw-string splice pattern did NOT fail for this path -- "
        "either the apostrophe is missing from apostrophe_clone or Python's raw-string parsing "
        "changed; this test needs to actually exercise a broken parse to mean anything"
    )
    assert "SyntaxError" in proc.stderr, (
        "expected the pre-fix pattern to fail with SyntaxError, got:\n%s" % proc.stderr
    )

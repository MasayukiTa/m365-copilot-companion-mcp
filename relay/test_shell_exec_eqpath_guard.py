# -*- coding: utf-8 -*-
"""shell_exec must refuse bash-only syntax that cmd.exe mis-executes on Windows.

WHY THIS EXISTS. 2026-11-07: a modal Windows shell dialog -- `'=C:/Users/.../k3cs_pr'`
"file not found" (0x80070002) -- kept popping while fleet jobs ran. Root cause: a worker handed
shell_exec a POSIX one-liner (`WT="C:/path" && git -C "$WT" ...`). shell_exec runs via cmd.exe
(subprocess shell=True) on Windows, which cannot parse a bare `NAME="value"` assignment and
never expands `$VAR`; the dropped assignment leaves a stray `="C:/path"` that, handed to the
Windows shell-open verb, raises 0x80070002 in a modal dialog.

tools.code_exec._cmd_shell_syntax_guard rejects these up front on Windows so the dialog never
fires. The guard is a deterministic string check with no clock and no state, so it is exercised
directly here regardless of host OS.
"""
import os

import pytest

from tools import code_exec as CE


def _guard(cmd):
    return CE._cmd_shell_syntax_guard(cmd)


K3 = 'WT="C:/tmp/m365-companion/agents/k3cs_pr" && git -C "$WT" status'


@pytest.mark.skipif(os.name != "nt", reason="guard only fires under cmd.exe on Windows")
def test_the_exact_oneliner_that_popped_the_dialog_is_refused():
    msg = _guard(K3)
    assert msg is not None
    assert "WT=" in msg
    assert "run_python" in msg or "set " in msg


@pytest.mark.skipif(os.name != "nt", reason="guard only fires under cmd.exe on Windows")
@pytest.mark.parametrize("cmd", [
    'FOO="C:/x" && echo hi',
    'A=1; B=2',
    'X=val | findstr val',
])
def test_bare_bash_assignment_is_refused(cmd):
    assert _guard(cmd) is not None


@pytest.mark.skipif(os.name != "nt", reason="guard only fires under cmd.exe on Windows")
@pytest.mark.parametrize("cmd", [
    'git -C $WT status',
    'echo ${HOME}/x',
    'echo $HOME',
])
def test_bash_variable_expansion_is_refused(cmd):
    assert _guard(cmd) is not None


@pytest.mark.skipif(os.name != "nt", reason="guard only fires under cmd.exe on Windows")
@pytest.mark.parametrize("cmd", [
    'set WT=C:/x && git -C %WT% status',
    'curl "https://h/api?a=b&c=d" -o out.json',
    'git -C C:/repo status',
    'echo a=b',
])
def test_legitimate_commands_are_allowed(cmd):
    assert _guard(cmd) is None


def test_guard_is_a_no_op_off_windows(monkeypatch):
    monkeypatch.setattr(CE.os, "name", "posix")
    assert _guard(K3) is None


@pytest.mark.skipif(os.name != "nt", reason="ShellExecute behaviour is Windows-only")
def test_root_cause_shellopen_of_eq_path_raises_0x80070002():
    with pytest.raises(OSError) as ei:
        os.startfile('=C:/tmp/m365-companion/agents/__guard_probe_nonexistent__')
    assert ei.value.winerror == 2


# A `$` that lives INSIDE a quoted span is not a cmd.exe variable reference and must not be
# rejected: cmd.exe passes the quoted text through verbatim, so these are legitimate Windows
# commands. The guard searches the quote-masked string, so the quoted `$` is invisible to it.
@pytest.mark.skipif(os.name != "nt", reason="guard only fires under cmd.exe on Windows")
@pytest.mark.parametrize("cmd", [
    'powershell -NoProfile -Command "Get-Process | ? { $_.CPU -gt 1 }"',
    'git log --format="%H $x"',
])
def test_dollar_inside_quotes_is_allowed(cmd):
    assert _guard(cmd) is None


# An UNQUOTED `$VAR` and a bash assignment are still bash-only syntax and stay rejected.
@pytest.mark.skipif(os.name != "nt", reason="guard only fires under cmd.exe on Windows")
@pytest.mark.parametrize("cmd", [
    'WT="C:/x" && git -C "$WT" status',
    'echo $HOME',
])
def test_unquoted_dollar_and_assignment_still_refused(cmd):
    assert _guard(cmd) is not None


# A `$` inside a SINGLE-quoted span is masked exactly like a double-quoted one, so these
# legitimate commands must also pass. cmd.exe does not special-case single quotes, but the
# guard masks both `"..."` and `'...'` spans so a literal `$` within either is invisible to it.
@pytest.mark.skipif(os.name != "nt", reason="guard only fires under cmd.exe on Windows")
@pytest.mark.parametrize("cmd", [
    "git log --format='%H $x'",
    "findstr '$PATH' notes.txt",
])
def test_dollar_inside_single_quotes_is_allowed(cmd):
    assert _guard(cmd) is None

"""A bash-style NAME="value" assignment must never reach cmd.exe\'s ShellExecute.

The regression this pins: on 2026-11-07 a fleet worker ran a POSIX one-liner
`WT="C:/...k3cs_pr" && git -C "$WT" ...` through shell_exec. shell_exec runs under
cmd.exe on Windows, which cannot parse a bare NAME="value" assignment; it handed the
quoted right-hand side to ShellExecute, which stripped the quotes to `=C:/...k3cs_pr`
and raised 0x80070002 (file not found) in a modal "起動時にエラー" dialog that kept
popping on the real desktop.

_cmd_shell_syntax_guard rejects that syntax up front (Windows only) so the command is
never spawned. These tests force os.name so the Windows branch runs on the Linux CI
runner too -- the guard is pure string analysis, nothing here is Windows-only.
"""
import os

import pytest

from tools.code_exec import _cmd_shell_syntax_guard


@pytest.fixture
def as_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")


# The exact worktree path from the 2026-11-07 incident.
_K3 = "C:/Users/USER/AppData/Local/Temp/m365-companion/agents/k3cs_pr"


def test_the_exact_failing_command_is_rejected(as_windows):
    cmd = 'WT="%s" && git -C "$WT" status' % _K3
    out = _cmd_shell_syntax_guard(cmd)
    assert out is not None
    assert "assignment" in out
    # The guard must name the offending variable so the caller can fix it.
    assert "WT=" in out


def test_a_leading_assignment_alone_is_rejected(as_windows):
    out = _cmd_shell_syntax_guard('FOO=bar')
    assert out is not None and "assignment" in out


def test_a_dollar_var_is_rejected(as_windows):
    out = _cmd_shell_syntax_guard('git -C "$WT" log')
    assert out is not None and "$VAR" in out


@pytest.mark.parametrize("cmd", [
    'git -C "%s" status' % _K3,            # no assignment, no $var
    'set WT=%s && git -C "%%WT%%" status' % _K3,  # correct cmd.exe form
    'echo "a=b&c=d"',                       # = and & only inside quotes
    'curl "https://x/?a=1&b=2"',            # URL query string in quotes
])
def test_legitimate_commands_pass(as_windows, cmd):
    assert _cmd_shell_syntax_guard(cmd) is None


def test_set_prefixed_assignment_is_allowed(as_windows):
    # `set NAME=value` is the correct cmd.exe form and must not be flagged.
    assert _cmd_shell_syntax_guard('set WT=C:/x') is None


def test_posix_never_fires(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert _cmd_shell_syntax_guard('WT="/tmp/x" && git -C "$WT" status') is None

"""shell_exec must refuse cmd.exe-misparsed syntax before it reaches ShellExecute.

WHY THIS EXISTS. On 2026-11-07 a modal Windows dialog appeared twice while fleet
jobs were running:

    '=C:/Users/.../Temp/m365-companion/agents/k3cs_pr' の起動時にエラー
    2147942402 (0x80070002) が発生しました / 指定されたファイルが見つかりません

The string has a leading '=', forward slashes and no quotes. Its source is
shell_exec: it runs the command through cmd.exe (subprocess shell=True). cmd.exe
cannot parse a bash assignment like `WT="C:/path" && git -C "$WT" ...`; it hands
the right-hand side to ShellExecute as a program to start, and after the quotes
are stripped that surfaces as `=C:/path`, which ShellExecute reports as
0x80070002 in a modal dialog -- on the operator's real desktop, where a fleet
worker cannot dismiss it.

The guard (_cmd_shell_syntax_guard) rejects such commands up front. The critical
case is the one whose left-hand side is EMPTY -- a token that simply starts with
'=' -- because that is the exact '=C:/...' string from the dialog and an
identifier-only assignment check (NAME=value) never sees it.

The guard only fires on Windows; on POSIX /bin/sh these commands are valid, so
the blocking assertions are asserted against the guard function directly (its
Windows branch) rather than by spawning a real shell.
"""
import os

from tools import code_exec


def _guard(command):
    """Call the guard as if on Windows, regardless of the host OS running the test."""
    saved = os.name
    try:
        os.name = "nt"
        return code_exec._cmd_shell_syntax_guard(command)
    finally:
        os.name = saved


# --- the regression: a leading-'=' token (empty left-hand side) ---------------

def test_a_leading_equals_path_is_refused():
    """The exact dialog string shape: `=C:/...` with forward slashes, no quotes."""
    bad = _guard("=C:/Users/x/AppData/Local/Temp/m365-companion/agents/k3cs_pr")
    assert bad is not None
    assert "0x80070002" in bad
    assert "run_python" in bad


def test_a_leading_equals_quoted_path_is_refused():
    assert _guard('="C:/Users/x/agents/k3cs_pr"') is not None


def test_a_leading_equals_after_a_separator_is_refused():
    """`... && =C:/p` -- the '=' token starts right after a command separator."""
    assert _guard("echo hi && =C:/p") is not None


# --- the broader bash-assignment / expansion family ---------------------------

def test_a_bash_assignment_that_opens_a_worktree_is_refused():
    """The command that actually produced the dialog on 2026-11-07."""
    bad = _guard('WT="C:/Users/x/agents/k3cs_pr" && git -C "$WT" status')
    assert bad is not None
    assert "0x80070002" in bad


def test_a_bare_dollar_expansion_is_refused():
    assert _guard("git -C $WT status") is not None
    assert _guard("git -C ${WT} status") is not None


# --- things that must still be allowed -----------------------------------------

def test_cmd_set_assignment_is_allowed():
    """`set NAME=value` is the correct cmd.exe form and must pass."""
    assert _guard("set WT=C:/p && echo %WT%") is None


def test_an_equals_inside_quotes_is_not_an_assignment():
    """A URL query string `?a=b&c=d` carries '=' and '&' inside quotes only."""
    assert _guard('curl "https://example.com/y?a=b&c=d"') is None


def test_a_plain_command_is_allowed():
    assert _guard('git -C "C:/p" status') is None


def test_an_equals_in_a_flag_value_is_allowed():
    """`--format=%H` has '=' but no identifier-start-of-token assignment."""
    assert _guard("git log --format=%H") is None


def test_the_guard_does_not_fire_off_windows():
    """On POSIX the same command is valid /bin/sh, so the guard must stay silent."""
    saved = os.name
    try:
        os.name = "posix"
        assert code_exec._cmd_shell_syntax_guard("=C:/p") is None
        assert code_exec._cmd_shell_syntax_guard('WT="x" && echo $WT') is None
    finally:
        os.name = saved

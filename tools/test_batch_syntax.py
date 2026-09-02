# -*- coding: utf-8 -*-
r"""Unescaped parentheses inside a cmd block, which close it early and abort the script.

THE FAILURE THIS CAME FROM. On a fresh machine quickstart.bat died at STEP 2 with

    : was unexpected at this time.

and the console window closed. It was not the environment, not .env, not encoding: cmd ends a
`for ... do (` block at the FIRST UNESCAPED `)` it meets, so

    if /i "%%A"=="MCP_API_KEY" echo   Bearer token  (MCP_API_KEY)        : %%B

closed the block at `(MCP_API_KEY)`, leaving `        : %%B` to be parsed as its own command.
A line beginning with `:` is not one, and a PARSE error aborts the whole batch immediately --
which is why no `pause` could hold the window: the pauses are all further down a script that
was never reached.

Reproduced on this machine both ways: unescaped, the run prints the error and never reaches
the end of the file; escaped, it prints the line and completes.

WHY IT WENT UNNOTICED. STEP 1 had been failing earlier for an unrelated reason (uv could not
see the machine's root certificates), so nobody had ever got as far as STEP 2. Fixing the
first defect revealed the second -- it had been there the whole time, unreachable.

The neighbouring lines were already escaped as `^(` and `^)`, so this was one line out of step
with the six around it, which is exactly the kind of thing a person reads past and a check does
not.
"""
import io
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BATS = ["quickstart.bat", "setup.bat"]

#: Characters cmd treats specially inside a block. `(` and `)` end it; `<` and `>` redirect.
SPECIAL = "()<>"


def offending_lines(text: str):
    """Echo lines sitting inside a parenthesised block that carry an unescaped special char.

    A HEURISTIC, AND SAID TO BE ONE. cmd's grammar is not recoverable by a regex, so this
    tracks block depth by counting unescaped parens OUTSIDE echo arguments and reports echo
    arguments that contain a bare one while depth > 0. It catches the defect that actually
    happened; it does not claim to model cmd.
    """
    out = []
    depth = 0
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if line.upper().startswith("REM ") or line.startswith("::"):
            continue
        m = re.search(r"\becho\b(.*)$", line, re.I)
        if m and depth > 0:
            arg = m.group(1)
            hit = re.search(r"(?<!\^)[%s]" % re.escape(SPECIAL), arg)
            if hit:
                out.append((n, hit.group(0), line[:110]))
        # Block depth is decided by everything that is NOT an echo argument.
        body = re.sub(r"\becho\b.*$", "", line, flags=re.I)
        depth += len(re.findall(r"(?<!\^)\(", body))
        depth -= len(re.findall(r"(?<!\^)\)", body))
        depth = max(0, depth)
    return out


@pytest.mark.parametrize("name", BATS)
def test_no_unescaped_special_inside_a_block(name):
    text = io.open(os.path.join(REPO, name), encoding="ascii").read()
    bad = offending_lines(text)
    assert not bad, "\n".join(
        "%s:%d has an unescaped %r -- write it as ^%s : %s" % (name, n, ch, ch, s)
        for n, ch, s in bad)


@pytest.mark.parametrize("name", BATS)
def test_the_batch_files_stay_ascii(name):
    """cmd mis-decodes non-ASCII on a cp932 console and corrupts the script."""
    raw = io.open(os.path.join(REPO, name), "rb").read()
    assert not [b for b in raw if b > 127]


# -- the checker itself ------------------------------------------------------------------------

def test_it_catches_the_line_that_actually_broke():
    """The exact line, verbatim from the failure."""
    src = ('if exist ".env" (\n'
           '    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (\n'
           '        if /i "%%A"=="MCP_API_KEY" echo   Bearer token  (MCP_API_KEY)        : %%B\n'
           '    )\n'
           ')\n')
    bad = offending_lines(src)
    assert len(bad) == 1 and bad[0][0] == 3


def test_the_escaped_form_is_accepted():
    src = ('if exist ".env" (\n'
           '    for /f "usebackq" %%A in (".env") do (\n'
           '        echo   Bearer token  ^(MCP_API_KEY^)        : %%B\n'
           '    )\n'
           ')\n')
    assert offending_lines(src) == []


def test_a_paren_outside_any_block_is_fine():
    """Only inside a block does a bare paren end something. Flagging them everywhere would make
    the check noisy, and a noisy check gets switched off."""
    assert offending_lines("echo hello (world)\n") == []


def test_redirection_characters_are_caught_too():
    """`<` and `>` in an echo argument redirect rather than print -- the same class of defect
    with a different symptom: no crash, just a file quietly created and the text missing."""
    src = "if 1==1 (\n    echo   value: <protected>\n)\n"
    bad = offending_lines(src)
    assert bad and bad[0][1] == "<"


def test_comments_are_not_scanned():
    src = "if 1==1 (\n    REM this mentions (parens) in prose\n)\n"
    assert offending_lines(src) == []


# -- process kills, which must never reach beyond this checkout --------------------------------

def test_the_server_kill_is_scoped_to_this_checkout():
    """Matching 'main.py' alone kills ANY process whose command line contains it: another clone
    of this repo on the same machine, an unrelated project's main.py, an editor running one
    under a debugger. supervisor.ps1 already scoped it with the same idiom and start_all.ps1 did
    not -- one file in the repository correct and the other not, which is how a rule that is
    written down still gets broken."""
    for name in ("scripts/start_all.ps1", "scripts/supervisor.ps1"):
        src = io.open(os.path.join(REPO, name), encoding="utf-8").read()
        for m in re.finditer(r"CommandLine -match 'main\\.py'", src):
            window = src[m.start():m.start() + 200]
            assert "$root*" in window or "$Root*" in window, (
                "%s kills main.py processes without scoping them to the repo" % name)


# -- consent for changes outside this folder ---------------------------------------------------

STARTALL = io.open(os.path.join(REPO, "scripts", "start_all.ps1"), encoding="utf-8").read()
QS = io.open(os.path.join(REPO, "quickstart.bat"), encoding="ascii").read()


def test_a_missing_decision_is_not_consent():
    """It used to provision whenever the marker was ABSENT, so a machine that had never been
    asked got a Desktop shortcut and a logon autostart entry anyway. Both change the machine
    outside this folder and both outlive the repo."""
    i = STARTALL.index("function Ensure-ConvenienceProvisioning")
    body = STARTALL[i:i + 2600]
    assert "no consent on record" in body
    assert "if (-not (Test-Path $markerPath))" in body


def test_each_action_is_gated_on_its_own_answer():
    """One question cannot stand in for two: a launcher on the desktop and a program that
    starts itself at logon are different commitments."""
    i = STARTALL.index("function Ensure-ConvenienceProvisioning")
    body = STARTALL[i:i + 3600]
    assert "$wantShortcut -and (Test-Path $shortcutScript)" in body
    assert "$wantAutostart -and (Test-Path $autostartScript)" in body


def test_the_recorded_answer_is_not_overwritten():
    """The file is the record of what the person chose. Stamping it with "provisioned" would
    erase the answer and leave nothing to honour next time."""
    i = STARTALL.index("function Ensure-ConvenienceProvisioning")
    body = STARTALL[i:i + 4200]
    assert '"provisioned" | Out-File' not in body


def test_the_question_comes_before_anything_is_created():
    """THE DEFECT. The prompt sat AFTER start_all had already made the shortcut, so answering
    "n" changed nothing -- the person was asked for permission for something already done."""
    assert QS.index("Create a one-click") < QS.index("start_all.ps1")


def test_autostart_is_asked_about_at_all():
    """It was never mentioned. A program that launches itself at every logon is the larger of
    the two commitments and was the one nobody was told about."""
    assert "automatically when you log on" in QS
    assert "register-supervisor.ps1" in QS


def test_autostart_defaults_to_no_and_the_launcher_to_yes():
    """Defaults carry the weight here, because most people press Enter. A desktop icon is
    trivially reversible; something that runs at every logon is not, so it has to be asked for
    rather than merely not-refused."""
    i = QS.index("automatically when you log on")
    assert "[y/N]" in QS[i:i + 120]
    j = QS.index("Create a one-click")
    assert "[Y/n]" in QS[j:j + 160]


def test_quickstart_no_longer_tells_the_reader_to_run_other_commands():
    """quickstart.bat is the ONLY install method, so delegating is a defect.

    It used to end with "run doctor.bat again" and the doctor's own fix lines sent people to
    start_companion_edge.ps1 and to a log file. That is homework, not an install procedure --
    and it is why a fresh machine finished red while every individual piece worked.
    """
    src = QS
    assert "run  doctor.bat  again" not in src, "still sends the reader to another command"
    assert "run quickstart.bat again" in src, "does not say how to resume"


def test_quickstart_signs_in_rather_than_asking_the_reader_to():
    src = QS
    assert "ensure_m365_signin.ps1" in src, "sign-in is still left to the operator"


def test_quickstart_prints_the_server_error_itself():
    # Naming .setup\logs\server.err.log is the same delegation one layer down. If the only
    # install method cannot say why the server died, nobody can.
    src = QS
    assert "server.err.log" in src
    assert "Get-Content -Tail" in src, "the log is named but never shown"


def test_the_error_block_uses_no_bare_parentheses():
    """THE BUG THAT REAL EXECUTION CAUGHT AND THE LINT DID NOT.

    The first version used `for %%A in (...) do if %%~zA GTR 0 (...)` to test the log's size.
    Inside the enclosing if-block those parentheses had to be escaped, which broke `for` itself:
    cmd reported an invalid use of (...) and the block aborted. The heuristic lint above passed
    it. It is now one quoted powershell call -- parentheses inside a quoted argument are not
    parsed by cmd at all, so the hazard is removed rather than escaped around.
    """
    src = QS
    i = src.index("SHOW THE SERVER")
    block = src[i:src.index("goto :after_banner", i)]
    assert "for %%A" not in block, "the parenthesised for-loop is back"
    assert "^(" not in block, "escaped parentheses are back in this block"

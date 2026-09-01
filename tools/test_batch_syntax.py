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

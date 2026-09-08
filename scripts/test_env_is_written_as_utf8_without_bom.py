# -*- coding: utf-8 -*-
"""Every writer of .env, held to one encoding rule at once.

THIS HAS BEEN FIXED THREE TIMES, ONE FILE AT A TIME. quickstart.bat wrote .env as ASCII and a
Japanese NOTE= line became "NOTE=???????????????" -- measured, not inferred. That was fixed.
scripts/setup_devtunnel.ps1 and scripts/setup.ps1 had the same line and were not looked at,
because the fix was aimed at the file that was reported rather than at the rule. They were
found only when a new-PC setup went wrong for an unrelated reason and the provisioner was
read for the first time since.

The rule, both halves:

  WRITING -- not `-Encoding ASCII`, which replaces every non-ASCII byte with '?'; and not
  PS 5.1's `-Encoding UTF8`, which emits a BOM. The BOM folds into the first key name, so
  MCP_API_KEY becomes "﻿MCP_API_KEY" and every reader misses it. The one correct form is
  [IO.File]::WriteAllLines(path, lines, New-Object System.Text.UTF8Encoding($false)).

  READING -- PS 5.1's Get-Content uses the ANSI codepage unless told otherwise, so a UTF-8
  .env comes back mojibake; a filter-and-rewrite then writes the mojibake back. Read with
  -Encoding UTF8 (which does tolerate a leading BOM on the way in).
"""
import io
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Files that read or write .env. Add a file here when it starts touching .env; a writer that
#: is not listed is not exempt, it is simply not yet caught.
TOUCHERS = [
    "scripts/setup.ps1",
    "scripts/setup_devtunnel.ps1",
    "quickstart.bat",
]


def _src(rel):
    p = ROOT / rel
    if not p.is_file():
        pytest.skip("%s is not present in this checkout" % rel)
    return io.open(p, encoding="utf-8", errors="replace").read()


@pytest.mark.parametrize("rel", TOUCHERS)
def test_no_env_write_uses_ascii(rel):
    """ASCII silently destroys content rather than failing, which is why it survived so long:
    setup reported success and the damage was only visible later, in a file nobody re-read."""
    src = _src(rel)
    for m in re.finditer(r"^.*Encoding ASCII.*$", src, re.M):
        line = m.group(0)
        if line.lstrip().startswith(("#", "REM", "::")):
            continue                      # the prose that explains the ban
        assert "env" not in line.lower(), "writes .env as ASCII: %s" % line.strip()


@pytest.mark.parametrize("rel", TOUCHERS)
def test_no_env_write_uses_powershell_utf8(rel):
    """`Set-Content -Encoding UTF8` looks like the fix and is not: PS 5.1 writes a BOM."""
    src = _src(rel)
    for m in re.finditer(r"^.*Set-Content.*-Encoding UTF8.*$", src, re.M):
        line = m.group(0)
        if line.lstrip().startswith(("#", "REM", "::")):
            continue
        assert "env" not in line.lower(), \
            "writes .env with PS 5.1 -Encoding UTF8, which adds a BOM: %s" % line.strip()


@pytest.mark.parametrize("rel", ["scripts/setup.ps1", "scripts/setup_devtunnel.ps1"])
def test_the_env_write_uses_the_no_bom_encoder(rel):
    """State the positive form too. A test that only forbids leaves the next author guessing,
    and the guess that looks most right is the one that adds a BOM."""
    src = _src(rel)
    assert "UTF8Encoding($false)" in src, "no no-BOM encoder in a file that writes .env"
    assert "WriteAllLines" in src


@pytest.mark.parametrize("rel", ["scripts/setup.ps1", "scripts/setup_devtunnel.ps1"])
def test_every_env_read_declares_its_encoding(rel):
    """The read half. Left off, PS 5.1 decodes with the ANSI codepage; the rewrite that
    follows then persists the mojibake, so a read defect becomes a write defect."""
    src = _src(rel)
    for m in re.finditer(r"^.*Get-Content\s+\$env\w*.*$", src, re.M):
        line = m.group(0)
        if line.lstrip().startswith("#"):
            continue
        assert "-Encoding" in line, "reads .env without an encoding: %s" % line.strip()

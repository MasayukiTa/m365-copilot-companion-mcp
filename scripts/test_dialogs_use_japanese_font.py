# -*- coding: utf-8 -*-
"""Every tracked .ps1 that builds a WinForms Form must use the Japanese-capable font helper
(owner-reported bug, 2026-09-24).

WinForms controls that never set an explicit .Font default to Microsoft Sans Serif (no
Japanese glyphs); Windows font linking then substitutes a Chinese fallback font on a
non-Japanese system locale, so Japanese text renders with Chinese glyph shapes. The fix is
scripts/win/ui_font.ps1's Get-JapaneseUiFont, dot-sourced into every Form-building script.

This is a GUARD test: it does not care what the dot-source line looks like exactly, only that
any tracked .ps1 that creates a System.Windows.Forms.Form also mentions ui_font.ps1 somewhere
in its own text. Enumeration goes through `git ls-files` (repo convention -- a raw filesystem
walk would pick up untracked/ignored files and produce a false pass or miss)."""
from __future__ import annotations

import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from tools import childproc  # noqa: E402  -- see tools/childproc.py: child output must not be

HELPER = os.path.join(REPO, "scripts", "win", "ui_font.ps1")

_BLOCK_COMMENT = re.compile(r"<#.*?#>", re.DOTALL)
# A real dot-source line, not just any mention of the filename -- a comment that merely
# *references* ui_font.ps1 (e.g. "see scripts/win/ui_font.ps1") must NOT satisfy this, or the
# guard is defeated by leaving an explanatory comment behind after deleting the actual
# dot-source line. Confirmed by mutation test: a plain substring check passes on a mutant with
# the dot-source line removed (a neighbouring comment still mentions the filename); this
# anchored-dot-source regex correctly fails on that mutant.
_DOT_SOURCE = re.compile(r"^\s*\.\s*\(.*ui_font\.ps1.*\)", re.MULTILINE)


def _tracked_ps1_files():
    r = childproc.run(["git", "ls-files", "--", "*.ps1"], cwd=REPO, capture_output=True,
                       check=True)
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def _strip_comments(text: str) -> str:
    text = _BLOCK_COMMENT.sub("", text)
    lines = []
    for line in text.splitlines():
        # Simple line-comment strip -- good enough here since we only need to avoid a
        # commented-out "New-Object System.Windows.Forms.Form" false-positiving.
        idx = line.find("#")
        lines.append(line if idx < 0 else line[:idx])
    return "\n".join(lines)


def _creates_a_form(stripped_text: str) -> bool:
    return "System.Windows.Forms.Form" in stripped_text


def test_helper_file_exists_and_defines_the_function():
    assert os.path.isfile(HELPER), "scripts/win/ui_font.ps1 is missing"
    text = open(HELPER, encoding="utf-8").read()
    assert "function Get-JapaneseUiFont" in text


def test_every_form_building_ps1_dot_sources_the_helper():
    files = _tracked_ps1_files()
    assert files, "git ls-files found no tracked .ps1 files -- something is wrong with the probe"

    form_building = []
    missing = []
    for rel in files:
        path = os.path.join(REPO, rel)
        raw = open(path, encoding="utf-8", errors="replace").read()
        stripped = _strip_comments(raw)
        if not _creates_a_form(stripped):
            continue
        form_building.append(rel)
        # Raw text (pre-strip) is fine here: a dot-source line is real code, not a comment.
        if not _DOT_SOURCE.search(raw):
            missing.append(rel)

    # Known scope, per the owner's own repo-wide audit: exactly these two tracked .ps1 files
    # create a Form. If this set ever changes, the new file needs the same treatment.
    form_building_norm = {p.replace("\\", "/") for p in form_building}
    assert form_building_norm == {"scripts/start_all.ps1", "scripts/configure_env.ps1"}, (
        "the set of Form-building .ps1 files changed: %r -- update this test's expected set "
        "and make sure the new file(s) use scripts/win/ui_font.ps1 too" % sorted(form_building_norm)
    )
    assert not missing, "these Form-building .ps1 files do not reference ui_font.ps1: %r" % missing

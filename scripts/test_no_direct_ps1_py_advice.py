# -*- coding: utf-8 -*-
r"""No user-facing text tells a person to run a .ps1 or .py file directly.

A person only ever double-clicks a .bat (quickstart.bat, setup.bat, start_all.bat, doctor.bat,
...); nobody runs `.\scripts\start.ps1` or `python scripts\bootstrap.py` by hand. Two defects
of exactly this shape were found by a Windows Sandbox run (standard user, only quickstart.bat):

  * bootstrap.py's "All steps complete" banner said "Next: start the server with
    .\\scripts\\start.ps1" -- printed mid-quickstart.bat run, where quickstart.bat itself moves
    straight on to its later steps and no person ever types that command.
  * quickstart.bat's own "skipped" lines for the optional Desktop shortcut / logon autostart
    said "...later with scripts\\make_desktop_shortcut.ps1" / "...later with
    scripts\\register-supervisor.ps1" -- no .bat wraps either script, so that was the only
    advice given, and it was wrong for a person to act on directly.

Both were fixed by pointing back at quickstart.bat (or start_all.bat) instead. This guards
against the same mistake creeping back into quickstart.bat, setup.bat, or bootstrap.py's own
console/markdown output.

WHAT COUNTS AS A VIOLATION. An echo line in quickstart.bat/setup.bat, or a string literal
passed to bootstrap.py's log()/write_text() calls, that reads as an instruction to RUN a .ps1
or .py file directly -- "run ... X.ps1", "with ... X.ps1", "start with .\\scripts\\X.ps1", and
so on. A line that only NAMES a script parenthetically to explain what an automatic step does
(e.g. "STEP 4 (setup_devtunnel.ps1) will provision it") is not an instruction to the reader and
is not flagged -- the pattern below requires "run"/"with"/"double-click" directly followed by a
path ending in .ps1/.py within the same short span, which that phrasing does not match.

NOT A SUBSTITUTE FOR READING THE OUTPUT. This is a narrow regex over source text: it cannot
prove every phrasing is fine, only that the two known-bad shapes (and close variants) do not
reappear. New user-facing copy should still be read by a person.
"""
from __future__ import annotations

import re

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent

# "run"/"with"/"double-click"/"start" followed (within a short span, same line) by a path
# ending in .ps1 or .py. Case-insensitive so "Run" and "run" both match.
_DIRECT_RUN_RE = re.compile(
    r"(?i)\b(run|with|double-click|start)\b[^\r\n]{0,60}\.(ps1|py)\b"
)

# Phrasing that mentions a script only to say an automatic step handles it -- not an
# instruction to the reader. Matched against the same line; a line containing one of these is
# exempt even if it also matches _DIRECT_RUN_RE.
_EXEMPT_RE = re.compile(
    r"(?i)"
    r"will (provision|handle|install|sign|run|create)"  # "STEP 4 (X.ps1) will provision it"
    r"|\bruns\b"                                          # "quickstart.bat runs setup.bat"
    r"|\brun for real\b"                                  # test-file self-description
)


def _bat_echo_lines(path):
    """Only the lines a person actually sees: `echo ...`, not REM comments or code."""
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if re.match(r"(?i)^echo(\.| )", s) or s.lower() == "echo.":
            out.append((i, s))
    return out


def _check(lines, label, results):
    for i, s in lines:
        if _EXEMPT_RE.search(s):
            continue
        if _DIRECT_RUN_RE.search(s):
            results.append("%s:%d: %s" % (label, i, s))


def test_quickstart_and_setup_bat_never_tell_the_reader_to_run_a_script_directly():
    results = []
    _check(_bat_echo_lines(ROOT / "quickstart.bat"), "quickstart.bat", results)
    _check(_bat_echo_lines(ROOT / "setup.bat"), "setup.bat", results)
    assert not results, (
        "user-facing echo line(s) tell a person to run a .ps1/.py directly "
        "(nobody does that -- point back at quickstart.bat/setup.bat/start_all.bat instead):\n"
        + "\n".join(results)
    )


def test_bootstrap_py_console_and_markdown_output_never_tells_the_reader_to_run_a_script():
    """Static scan of string literals reaching log()/print()/write_text() in bootstrap.py.
    Running the whole tool to exercise every code path is not needed -- the two known defects
    (the 'Next:' banner and the generated copilot-connector.md) are both string literals in the
    source, and a regression of the same shape would be too."""
    src = (ROOT / "scripts" / "bootstrap.py").read_text(encoding="utf-8")
    results = []
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if s.startswith("#"):
            continue
        # Only lines that look like they carry literal user-facing text (a string literal is
        # present); this intentionally over-matches (e.g. docstring lines) rather than under-
        # match and miss a real instance -- a false positive here is cheap to inspect.
        if '"' not in s and "'" not in s:
            continue
        if _EXEMPT_RE.search(s):
            continue
        if _DIRECT_RUN_RE.search(s):
            results.append("bootstrap.py:%d: %s" % (i, s))
    assert not results, (
        "bootstrap.py line(s) tell a person to run a .ps1/.py directly "
        "(nobody does that -- point back at quickstart.bat/start_all.bat instead):\n"
        + "\n".join(results)
    )

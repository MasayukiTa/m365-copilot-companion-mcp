# -*- coding: utf-8 -*-
"""No tracked Python file may contain an invalid escape sequence.

FOUND BY ACCIDENT, 2026-09-14. A new AST scan printed `<unknown>:50: DeprecationWarning:
invalid escape sequence '\\.'` on every run -- naming no file, because the scan had not passed a
filename. Passing one named `bench/done_vs_correct.py`, and sweeping every tracked file found
**17 of them across 6 files**.

MOST WERE HARMLESS AND TWO WERE NOT, which is the whole reason this is a guard rather than a
cleanup. Python leaves an UNRECOGNISED escape (`\\.`, `\\d`, `\\s`, `\\p`, `\\R`) in the string,
so `"scripts\\doctor.ps1"` really does contain a backslash and the assertion using it passed for
the right reason. But an escape Python DOES recognise is consumed:

    bench/remote/test_broker_client.py   "Z:\\nope\\BROKER_ON"   -> "Z:" NEWLINE "ope\\BROKER_ON"
    bench/test_ui_goal_lines.py          "\\backslashes\\"        -> BACKSPACE + "ackslashes"

The first is a Windows path fixture that was never a path; the test passed anyway, because a
path containing a newline is exactly as absent as one that does not. The second is a fixture
named "backslashes" that contained one backslash rather than two. Neither failed, and neither
tested what it says it tests -- and there is no way to tell the two classes apart by reading,
which is why the rule is "none at all" rather than "none that matter".

AND THE HARMLESS ONES STOP BEING HARMLESS. An unrecognised escape has been a DeprecationWarning
since 3.6 and is a SyntaxWarning now; the change to SyntaxError is announced. When it lands, the
file stops importing -- so the cost of leaving them is paid all at once, on an upgrade, in files
nobody was touching.

THE FIX IS ALWAYS ONE OF TWO THINGS, never a third: make the literal raw (`r"..."`) when every
backslash is meant literally, or double the backslashes when some escapes in it are real. A
string with both, like `bench/test_ui_goal_lines.py`'s -- real `\\n` and `\\t` beside literal
backslashes -- can only take the second.
"""
from __future__ import annotations

import ast
import io
import os
import subprocess
import warnings

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tracked_python():
    out = subprocess.check_output(["git", "ls-files"], cwd=REPO).decode("utf-8", "replace")
    return [r for r in out.splitlines() if r.endswith(".py")]


def _escape_warnings(rel):
    """(lineno, message) for every invalid escape in one file, via the compiler itself.

    NOT A REGEX OVER THE TEXT. Deciding whether a backslash sits inside a string literal, and
    whether the literal is raw, is exactly the job of a parser -- and a regex that got it wrong
    would produce the failure this file is about: a check that looks thorough and is not.
    """
    path = os.path.join(REPO, rel)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            ast.parse(io.open(path, encoding="utf-8").read(), filename=rel)
        except (OSError, UnicodeDecodeError, SyntaxError):
            return []                       # a file that will not parse is another test's job
        return [(getattr(w, "lineno", 0), str(w.message)) for w in caught
                if "escape sequence" in str(w.message)]


def test_no_tracked_file_carries_an_invalid_escape_sequence():
    found = []
    for rel in _tracked_python():
        for lineno, msg in _escape_warnings(rel):
            found.append("%s:%s %s" % (rel, lineno, msg))
    assert not found, (
        "%d invalid escape sequence(s). Each is either a literal backslash -- make the string "
        "raw -- or an escape that was consumed, which means the value is not what it reads as. "
        "A SyntaxError in a future Python either way:\n  %s"
        % (len(found), "\n  ".join(found)))


def test_the_sweep_can_actually_see_one():
    """A sweep that finds nothing everywhere proves nothing anywhere.

    This is the check the ORIGINAL scan lacked: it emitted the warning with no filename, so the
    signal was on screen for days and pointed at nothing. Here the instrument is shown working
    on a known-bad input before its clean result is believed."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ast.parse('x = "C:\\d\\ir"\n', filename="<planted>")
        msgs = [str(w.message) for w in caught if "escape sequence" in str(w.message)]
    assert msgs, "the sweep cannot see an invalid escape even when one is planted in front of it"


def test_a_raw_string_is_not_reported():
    """The other half: a guard that fires on the FIX would be worse than no guard."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ast.parse('x = r"C:\\d\\ir"\n', filename="<planted>")
        assert not [w for w in caught if "escape sequence" in str(w.message)]


def test_the_sweep_reads_the_repository_and_not_an_empty_list():
    """`git ls-files` returning nothing would make the guard vacuously green."""
    files = _tracked_python()
    assert len(files) > 200, "only %d tracked .py files -- the listing is wrong" % len(files)

"""The WPF cockpit's C# source, however many files it is spread over.

ui/FleetCockpit.cs used to be one 17,688-line file. It is now the main part of
`partial class CockpitWindow` plus sibling parts `ui/FleetCockpit.<Theme>.cs` (see the
FleetCockpit.* names on the Build line of ui/rebuild_ui.ps1, which is the one list of what is
compiled). A test that asserts something about the cockpit's source must therefore read ALL the
parts: reading only FleetCockpit.cs would find the constructor and the fields and silently miss
every method that was moved out -- and most of those tests are `assert "..." in src`, which would
fail loudly, but the NEGATIVE ones (`assert "..." not in src`) would pass for the wrong reason.

Use this module instead of opening ui/FleetCockpit.cs by name:

    from tools import cockpit_source
    src = cockpit_source.read()                       # text of all parts, main part first
    src = cockpit_source.read(encoding="utf-8")       # same encodings/errors as open()
    paths = cockpit_source.paths()                    # the files, main part first

The parts are found by globbing `ui/FleetCockpit*.cs` (the `FleetCockpit.exe.config` neighbour is
not .cs), sorted by name with the main part first, so a part added tomorrow is covered without
anyone remembering this file exists.
"""
import glob
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(REPO, "ui")
MAIN = os.path.join(UI, "FleetCockpit.cs")


def paths(ui_dir=None):
    """Every part of the cockpit source, FleetCockpit.cs first, the rest sorted by name."""
    ui_dir = ui_dir or UI
    main = os.path.join(ui_dir, "FleetCockpit.cs")
    rest = sorted(p for p in glob.glob(os.path.join(ui_dir, "FleetCockpit.*.cs")))
    return ([main] if os.path.isfile(main) else []) + rest


def member(src, signature, start=0):
    """The text of the member whose declaration contains `signature`: from the signature to the
    brace that closes its body (strings, verbatim strings, char literals and comments skipped).

    For tests that used to cut `src[a:b]` between two methods: that only worked while the two
    were neighbours in ONE file. After the split neighbours may live in different parts, and the
    concatenation order of `read()` is by file name, not by the original member order."""
    i = src.index(signature, start)
    n = len(src)
    j = i
    depth = 0
    seen_open = False
    while j < n:
        c = src[j]
        d = src[j + 1] if j + 1 < n else ""
        if c == "/" and d == "/":
            j = src.find("\n", j)
            j = n if j < 0 else j
            continue
        if c == "/" and d == "*":
            j = src.find("*/", j + 2)
            j = n if j < 0 else j + 2
            continue
        if c == "@" and d == '"':
            j += 2
            while j < n:
                if src[j] == '"':
                    if src[j + 1:j + 2] == '"':
                        j += 2
                        continue
                    break
                j += 1
            j += 1
            continue
        if c == '"':
            j += 1
            while j < n and src[j] != '"' and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            j += 1
            continue
        if c == "'":
            k = j + 1
            if src[k:k + 1] == "\\":
                k += 2
            else:
                k += 1
            if src[k:k + 1] == "'":
                j = k + 1
                continue
        if c == "{":
            depth += 1
            seen_open = True
        elif c == "}":
            depth -= 1
            if seen_open and depth == 0:
                return src[i:j + 1]
        j += 1
    raise ValueError("no closing brace after %r" % signature)


def read(encoding="utf-8-sig", errors="strict", ui_dir=None):
    """The concatenated text of every part (joined by a newline).

    Each part is decoded on its own. A byte-order mark on a part other than the first is
    dropped (it would otherwise sit in the middle of the text); the first part keeps whatever
    `encoding` gives it, exactly as a direct open() of FleetCockpit.cs did."""
    out = []
    for i, p in enumerate(paths(ui_dir)):
        with open(p, encoding=encoding, errors=errors) as fh:
            t = fh.read()
        if i and t.startswith("﻿"):
            t = t[1:]
        out.append(t)
    return "\n".join(out)

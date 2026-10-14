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

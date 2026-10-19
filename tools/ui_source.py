"""The C# source of a logical UI unit, however many files it is spread over.

ui/CopilotChat.cs (the chat window, `partial class ChatWindow`) and ui/SelfImproveDashboard.cs
(`SelfImproveDashboardWindow`) are being split into partial-class parts, the way ui/FleetCockpit.cs
already was (see tools/cockpit_source.py). A test that asserts something about their source must
read ALL the parts: opening only ui/CopilotChat.cs would find the constructor and the fields and
miss every method that was moved out, and a NEGATIVE assertion (`assert "..." not in src`) would
pass for the wrong reason.

Use this module instead of opening those files by name:

    from tools import ui_source
    src = ui_source.chat_window_source()                     # CopilotChat.cs + CopilotChat.*.cs
    src = ui_source.self_improve_source(encoding="utf-8")    # same encoding/errors as open()
    src = ui_source.read_parts("CopilotChat")                # the generic form
    paths = ui_source.part_paths("CopilotChat")              # the files, main part first

THE PART RULE (one rule for every stem). The parts of stem S are
    ui/S.cs                       the main part, first
    ui/S.<Topic>.cs               every other part, sorted by file name, where <Topic> is ONE
                                  identifier (letters, digits, underscore; starts with a letter)
and nothing else. So `CopilotChat.Store.cs` is a part of CopilotChat, while `ChatSend.cs`,
`StartupGate.cs` (a separate, non-partial class), `CopilotChatX.cs` and `CopilotChat.a.b.cs` are
not. A part added tomorrow is covered without anyone remembering this file exists. The names in
ui/ that start with these stems are pinned by tools/test_ui_source_reads_every_part.py.

The concatenation order is main part first, then by file name. It is NOT the original member
order of the unsplit file: a test that cut `src[a:b]` between two members that used to be
neighbours must cut inside one member instead (tools/cockpit_source.member does that).
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(REPO, "ui")

CHAT_WINDOW = "CopilotChat"
SELF_IMPROVE = "SelfImproveDashboard"

_TOPIC = r"[A-Za-z][A-Za-z0-9_]*"


def is_part(stem, name):
    """True when the bare file `name` is the main part or a topic part of `stem`."""
    return name == stem + ".cs" or re.fullmatch(re.escape(stem) + r"\." + _TOPIC + r"\.cs", name) is not None


def part_paths(stem, ui_dir=None):
    """Every existing part of `stem`: S.cs first (if it exists), then S.<Topic>.cs sorted by name."""
    ui_dir = ui_dir or UI
    try:
        names = os.listdir(ui_dir)
    except OSError:
        return []
    main = stem + ".cs"
    rest = sorted(n for n in names if n != main and is_part(stem, n))
    first = [main] if main in names else []
    return [os.path.join(ui_dir, n) for n in first + rest if os.path.isfile(os.path.join(ui_dir, n))]


def read_parts(stem, encoding="utf-8-sig", errors="strict", ui_dir=None):
    """The concatenated text of every part of `stem`, joined by a newline.

    Each part is decoded on its own with the caller's `encoding`/`errors`, exactly as an open() of
    the single file did. A byte-order mark on a part other than the first is dropped (it would
    otherwise sit in the middle of the text); the first part keeps whatever `encoding` gives it.
    Raises FileNotFoundError when there is no part at all, as open() on a missing file did."""
    found = part_paths(stem, ui_dir)
    if not found:
        raise FileNotFoundError("no ui/%s.cs or ui/%s.<Topic>.cs under %s" % (stem, stem, ui_dir or UI))
    out = []
    for i, p in enumerate(found):
        with open(p, encoding=encoding, errors=errors) as fh:
            t = fh.read()
        if i and t.startswith("\ufeff"):
            t = t[1:]
        out.append(t)
    return "\n".join(out)


def chat_window_paths(ui_dir=None):
    return part_paths(CHAT_WINDOW, ui_dir)


def chat_window_source(encoding="utf-8-sig", errors="strict", ui_dir=None):
    """ui/CopilotChat.cs plus every ui/CopilotChat.<Topic>.cs, in sorted order."""
    return read_parts(CHAT_WINDOW, encoding, errors, ui_dir)


def self_improve_paths(ui_dir=None):
    return part_paths(SELF_IMPROVE, ui_dir)


def self_improve_source(encoding="utf-8-sig", errors="strict", ui_dir=None):
    """ui/SelfImproveDashboard.cs plus every ui/SelfImproveDashboard.<Topic>.cs, in sorted order."""
    return read_parts(SELF_IMPROVE, encoding, errors, ui_dir)


#: The logical units, by the short name the command line takes: (paths function, source function).
UNITS = {
    "chat": (chat_window_paths, chat_window_source),
    "self_improve": (self_improve_paths, self_improve_source),
}


def main(argv=None):
    """`python -m tools.ui_source chat` lists the files a test reading the chat window would get,
    in the order it gets them; `--text` prints their concatenated source instead."""
    import argparse
    ap = argparse.ArgumentParser(prog="python -m tools.ui_source", description=main.__doc__)
    ap.add_argument("unit", choices=sorted(UNITS))
    ap.add_argument("--text", action="store_true", help="print the concatenated source")
    args = ap.parse_args(argv)
    paths_fn, source_fn = UNITS[args.unit]
    if args.text:
        print(source_fn())
    else:
        for p in paths_fn():
            print(os.path.relpath(p, REPO))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

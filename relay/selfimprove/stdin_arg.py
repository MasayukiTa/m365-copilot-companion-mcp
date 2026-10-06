# -*- coding: utf-8 -*-
"""The "-" convention: an argument whose value is read from stdin, unaltered.

WHY IT IS SHARED. `pending.py` implemented this and documented it as "how the dashboard passes
it". `frozen.py` took the same flag name, was called the same way by the same dashboard, and
never implemented it -- so every re-signing made from the dashboard recorded the literal string
"-" as the operator's authorisation. The act happened, the approval was real, and the ledger
kept a placeholder where the words were supposed to be. It was on screen the whole time, as a
small chip reading "-", and nobody read it as the absence it was.

That is not a bug in either file. It is a convention that existed in one place and was assumed
in another, which is what a convention does when it is not somewhere both can reach.

VERBATIM MEANS VERBATIM. The dashboard used to substitute an apostrophe for every double quote
before putting the text on a command line, so a decision containing one was recorded as
something the operator had not written -- while the dialog promised, in as many words, that
nothing would be summarised or reworded. Reading it from stdin removes the quoting problem
rather than escaping around it.
"""
from __future__ import annotations

import sys

#: The value that means "the real one is on stdin".
FROM_STDIN = "-"


def resolve(value):
    """`value` as given, or stdin's whole contents when it is exactly "-".

    Returns "" if stdin cannot be read: a caller that then refuses an empty authorisation is
    doing the right thing, and inventing a value here would be the failure this exists to stop.
    """
    if str(value) != FROM_STDIN:
        return value
    try:
        # The raw buffer, decoded as UTF-8. sys.stdin.read() uses the locale encoding, which on
        # this machine is cp932 -- so reading it that way would corrupt exactly the text this
        # path exists to carry through unaltered.
        return sys.stdin.buffer.read().decode("utf-8", "replace")
    except Exception:
        return ""


def help_suffix() -> str:
    """One sentence for a flag's --help, so the convention is discoverable where it is used."""
    return ('"-" reads the value from stdin, which is how the dashboard passes it: a command '
            "line cannot carry every character a person might type.")

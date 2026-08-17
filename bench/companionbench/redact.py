"""Take the machine out of the results before they are written down.

A results file is the one artefact of this bench that gets committed, and this repository is
public. Everything else in it is chosen -- an episode id, a score, a reason string someone
wrote -- but a traceback is not chosen: it carries whatever absolute paths the interpreter
happened to be running from, and on this machine those paths contain the account name and the
checkout directory. That is how the rule was broken: a grader crashed, the traceback went into
`details.trace`, and the account name went to a public repository inside it.

So the fix is not "stop recording tracebacks" -- the traceback is the only thing that made the
crash diagnosable. The fix is that no local path reaches a file. This runs at the write
boundary AND where tracebacks are captured, because the object is also printed to a terminal
and pasted into commit messages, and one choke point that everything happens to pass through
today is not the same as a rule.

Shape, not literals: the same reason `scripts/check_no_identifying_names.py` matches shapes
rather than a list of forbidden words. A redactor that knows one account name protects one
machine. A redactor that knows what a home directory LOOKS like protects the next one too, and
does not itself have to contain the string it is defending against.
"""
from __future__ import annotations

import os
import re

# `C:\Users\someone`, `/home/someone`, `/Users/someone`, either separator, any case. The name
# is whatever runs up to the next separator; it is the thing being removed, so it is not
# spelled out here.
_HOME = re.compile(
    r"(?i)(?:[A-Za-z]:)?[\\/]{1,2}(?:Users|home)[\\/]{1,2}[^\\/\s\"'<>|]{1,64}"
)

# What is left after the home is gone: the checkout directory name. It is not a path shape --
# it is a word -- so it can only be removed by knowing where the checkout is, which we do.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _root_forms():
    """The checkout path as it can appear: native, posix, and JSON-doubled separators."""
    seen, out = set(), []
    for form in (_ROOT, _ROOT.replace("\\", "/"), _ROOT.replace("\\", "\\\\")):
        if form and form not in seen:
            seen.add(form)
            out.append(form)
    # Longest first, so the doubled form is not half-eaten by the single one.
    return sorted(out, key=len, reverse=True)


def redact(text: str) -> str:
    """Replace anything that identifies this machine with a placeholder.

    The checkout goes first and the home second, because the checkout lives inside the home:
    the other order turns `<home>/checkout/bench/x.py` into something that still names the
    checkout directory, which is the part that carries the organisation's name.
    """
    if not text:
        return text
    for form in _root_forms():
        text = text.replace(form, "<repo>")
    return _HOME.sub("<home>", text)


def redact_deep(value):
    """`redact` over a whole result object -- strings, lists, dicts, and dict KEYS.

    Keys matter: a per-path dict (a workdir listing, a grader's file map) puts the absolute
    path in the key, where a value-only walk would leave it untouched.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact(k) if isinstance(k, str) else k: redact_deep(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact_deep(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_deep(v) for v in value)
    return value

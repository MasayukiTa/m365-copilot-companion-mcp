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

FOUR WAYS THE FIRST VERSION LEAKED, all found by review and all reproduced before fixing:

  * An account name containing a space was cut at the space, so the surname and the whole
    remaining path survived. The account segment now runs to the next SEPARATOR and may
    contain spaces. Where that is ambiguous -- a path at the end of a sentence -- it takes
    too much rather than too little, because over-redaction costs a word of context and
    under-redaction is the bug this module exists for.

  * The checkout was matched case-sensitively, so on Windows, where paths are not, a differing
    case survived intact. And a checkout OUTSIDE any home -- a build directory, CI -- matched
    neither rule, so the entire path survived. Matched case-insensitively now.

  * Only `str` was walked. A `pathlib.Path` or a `bytes` went through untouched, and
    `json.dump(default=str)` then wrote the raw path -- the identical failure to the one being
    fixed, one type away. Path-likes and bytes are redacted as the text they will become.

  * Redacting dict KEYS silently merged records: two different homes became one key and one
    row overwrote the other. A privacy filter that destroys the evidence is not a fix. Colliding
    keys are now kept apart and the collision is recorded in the object.
"""
from __future__ import annotations

import os
import re

# A Windows or POSIX home directory, either separator, any case. The account segment runs to
# the next separator and MAY contain spaces; it stops at quotes, angle brackets, pipes and
# line breaks, which is what keeps `File "<path>", line 3` and `<home>` from being eaten.
# No example of an account name is written here -- `scripts/check_no_identifying_names.py`
# matches the SHAPE, and it is right to flag any file containing it, including this one.
_HOME = re.compile(
    r"(?i)(?:[A-Za-z]:)?[\\/]{1,2}(?:Users|home)[\\/]{1,2}[^\\/\r\n\"'<>|]{1,64}"
)

# What is left after the home is gone: the checkout directory name. It is not a path shape --
# it is a word -- so it can only be removed by knowing where the checkout is, which we do.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _root_pattern():
    """The checkout path, case-insensitively, in every separator form it can appear in.

    Rebuilt per call rather than cached at import: the tests move `_ROOT` to reproduce a
    checkout outside a home, and a cache would make the module answer for the wrong machine.
    """
    root = _ROOT
    if not root:
        return None
    forms = []
    for form in (root, root.replace("\\", "/"), root.replace("\\", "\\\\")):
        if form not in forms:
            forms.append(form)
    # Longest first, so the doubled form is not half-eaten by the single one.
    forms.sort(key=len, reverse=True)
    return re.compile("|".join(re.escape(f) for f in forms), re.I)


def redact(text):
    """Replace anything that identifies this machine with a placeholder.

    The checkout goes first and the home second, because the checkout lives inside the home:
    the other order turns `<home>/checkout/bench/x.py` into something that still names the
    checkout directory, which is the part that carries the organisation's name.
    """
    if isinstance(text, (bytes, bytearray)):
        # Decoded rather than skipped: a bytes traceback is a traceback, and leaving it for
        # `default=str` to stringify later is exactly how a raw path reached a committed file.
        text = bytes(text).decode("utf-8", "replace")
    elif isinstance(text, os.PathLike):
        text = os.fspath(text)
    if not isinstance(text, str) or not text:
        return text
    pattern = _root_pattern()
    if pattern is not None:
        text = pattern.sub("<repo>", text)
    return _HOME.sub("<home>", text)


def redact_deep(value):
    """`redact` over a whole result object -- strings, path-likes, bytes, lists, dicts, KEYS.

    Keys matter: a per-path dict (a workdir listing, a grader's file map) puts the absolute
    path in the key, where a value-only walk would leave it untouched. But redacting keys can
    make two of them equal, and a dict comprehension resolves that by throwing one row away.
    Distinct inputs therefore stay distinct: the second and later collisions are suffixed and
    the fact is recorded under `_redaction_collisions`, so a reader sees that two paths were
    different rather than silently reading one row where there were two.
    """
    if isinstance(value, (str, bytes, bytearray, os.PathLike)):
        return redact(value)
    if isinstance(value, dict):
        out, collisions = {}, 0
        for key, item in value.items():
            new_key = redact(key) if isinstance(key, (str, bytes, bytearray,
                                                      os.PathLike)) else key
            if new_key in out:
                collisions += 1
                base = new_key
                while new_key in out:
                    new_key = ("%s#%d" % (base, collisions) if isinstance(base, str)
                               else (base, collisions))
                    collisions += 1
            out[new_key] = redact_deep(item)
        if collisions:
            out["_redaction_collisions"] = collisions
        return out
    if isinstance(value, list):
        return [redact_deep(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_deep(v) for v in value)
    if isinstance(value, set):
        return {redact_deep(v) for v in value}
    return value

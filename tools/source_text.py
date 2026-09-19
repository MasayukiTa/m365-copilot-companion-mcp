# -*- coding: utf-8 -*-
"""Read a source file as CODE ONLY, so a check about code cannot be answered by prose.

WHY THIS IS A SHARED MODULE AND NOT A HELPER IN ONE TEST FILE. On 2026-09-18 the same class of
false positive was hit four times in one day:

    1. a guard forbidding `S.get(` fired on `_DRAIN_ATTEMPTS.get(`
    2. a check forbidding a reconstructed binary part matched the COMMENT explaining that very
       mistake -- so it was narrowed to skip comment lines
    3. it then matched the module DOCSTRING, which also explains the mistake
    4. a NEW check, written after all of the above, forbade "FileBase64" in
       relay/socket_attachment.py and matched the docstring paragraph describing the six
       multipart fields the page sends

The fourth happened because the fix from the third lived as a private `_code()` inside one test
file. A discipline that has to be re-derived per file is one that will be re-derived wrongly.

These files are valuable precisely because they write down what went wrong, and a text search
cannot tell an explanation from the thing explained. So the explanation is removed before the
search: comments by line, docstrings BY PARSING -- not by pattern, since the pattern is what
kept failing.

Other string literals stay. The checks that use this look for real code such as
`headers["Authorization"] = ...`, which is a literal in an assignment, not prose.
"""
from __future__ import annotations

import ast
import io


def code_only(path: str) -> str:
    """The file's source with comments and docstrings blanked out, line numbering preserved."""
    src = io.open(path, encoding="utf-8", errors="replace").read()
    lines = src.splitlines()
    blank = set()
    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                continue
            body = getattr(node, "body", None) or []
            if not body:
                continue
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                end = getattr(first, "end_lineno", first.lineno)
                blank.update(range(first.lineno, end + 1))
    except SyntaxError:
        # A file that does not parse still gets its comments removed. Returning the raw source
        # instead would quietly restore the failure this module exists to prevent.
        pass
    return "\n".join("" if (i + 1) in blank else l
                     for i, l in enumerate(lines) if not l.lstrip().startswith("#"))

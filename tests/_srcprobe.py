"""Ask what a module or function DOES, not what its prose says about what it used to do.

WHY THIS EXISTS. Four separate tests in this repository have asserted that some pattern is
absent from a source file, and failed on the comment explaining why that pattern was removed:

    assert "in last" not in source          # matched the docstring quoting the old rule
    assert "Fetch.enable" not in source     # matched the note on why CDP was abandoned

A test that scans raw text cannot tell code from the account of the code. The property being
asserted is always about what executes, so the docstrings come out first and the assertion is
made against the rest.

Comments are not in the AST at all, so they disappear for free -- which is the other half of
the same problem, and the reason this does not simply strip triple-quoted strings by hand.
"""
from __future__ import annotations

import ast
import inspect


def _strip_docstrings(node):
    """Every docstring in the tree, removed in place. Returns the node."""
    for sub in ast.walk(node):
        body = getattr(sub, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            del body[0]
    return node


def executable_source(obj) -> str:
    """`obj`'s code with its docstrings gone, as text an `in` test can be run against.

    Takes a module, class or function -- whatever `inspect.getsource` accepts. The result is
    unparsed rather than sliced out of the original, so formatting differences cannot smuggle
    a pattern back in.
    """
    src = inspect.getsource(obj)
    return ast.unparse(_strip_docstrings(ast.parse(inspect.cleandoc(src) if src.startswith(" ")
                                                   else src)))


def executable_source_of_file(path: str) -> str:
    """The same, for a file that is awkward to import."""
    with open(path, encoding="utf-8") as fh:
        return ast.unparse(_strip_docstrings(ast.parse(fh.read())))

# -*- coding: utf-8 -*-
"""The contract reaches the worker whole.

The goal text calls this block "the required interface / behavioral contract (the patch is
judged against THIS)", and the steps under it tell the worker to enumerate EVERY public symbol
the contract names and make each a checklist item the patch must satisfy.

It was being cut at 3000 characters per field, mid-sentence, with no marker. The worker could not
know anything was missing; the checklist it was told to build was short by whatever fell off the
end. Measured on one slice: 4 of 40 contracts exceeded the cap, the longest field ran to 4155
characters, and every graded instance among them failed.

These tests read the AST rather than the text, because the file now explains the cap in a comment
that necessarily contains "3000" -- a substring search matches the explanation and passes while
the slice is still there.
"""
import ast
import io
import os

SRC = os.path.join(os.path.dirname(__file__), "pro_stage_goals.py")


def _slices_in_source():
    """Every subscript that drops the tail of something, e.g. x[:3000]."""
    tree = ast.parse(io.open(SRC, encoding="utf-8").read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        sl = node.slice
        if isinstance(sl, ast.Slice) and sl.upper is not None and sl.lower is None:
            if isinstance(sl.upper, ast.Constant) and isinstance(sl.upper.value, int):
                target = getattr(node.value, "id", "") or getattr(node.value, "attr", "")
                out.append((target, sl.upper.value))
    return out


def test_the_contract_is_not_truncated():
    """The defect. `req[:3000]` and `iface[:3000]` removed the tail of the one thing the goal
    text says the patch is judged against."""
    for name, cap in _slices_in_source():
        assert name not in ("req", "iface"), (
            "%s is being cut to %d characters; the contract must reach the worker whole" %
            (name, cap)
        )


def test_the_explanation_survives_but_is_only_an_explanation():
    """Guards the guard: the comment recording this necessarily contains the old number, which
    is exactly why the test above parses the AST instead of searching the text."""
    text = io.open(SRC, encoding="utf-8").read()
    assert "3000" in text, "the note explaining why there is no cap was removed"
    assert not [n for n, _ in _slices_in_source() if n in ("req", "iface")]

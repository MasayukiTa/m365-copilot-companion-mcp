# -*- coding: utf-8 -*-
"""pptx_export_png must close only the document it opened -- never quit or kill PowerPoint.

WHY THIS EXISTS. PowerPoint is a single shared application the operator may already be
using. The PNG-export tool drives it over COM, and an earlier implementation called
``PowerPoint.Application.Quit()`` in its ``finally`` block. Quitting the application tears
down every open document -- including the operator's live, unsaved work -- not just the
one the tool opened. The ownership rule is narrow: the tool may CLOSE THE ONE DOCUMENT IT
OPENED ITSELF and nothing more. It may NOT call ``Application.Quit``, terminate/kill the
process by name, close all windows, or close any other open presentation -- even on a COM
instance it dispatched itself.

These tests prove that rule from the SOURCE, by parsing it. They start no PowerPoint and
open no document: the prohibition is about what the code is allowed to contain, which is
decided before any COM call, so it is checked statically. That also keeps the suite
hermetic -- it needs no Windows, no pywin32, and no live Office install, so it runs on the
Linux CI runner like every other hermetic test.

SCOPED TO COMMENTS-STRIPPED CODE. The fixed implementation explains in prose comments why
there is no Quit; a naive text scan for the word "Quit" would match that explanation and
fail on the correct code. So the checks below read the function's ast, not its text, and
judge only executable nodes (calls, attributes), never comments or docstrings.
"""
from __future__ import annotations

import ast
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(REPO, "tools", "pptx_ops.py")


def _export_png_function() -> ast.FunctionDef:
    """Return the ast node for pptx_export_png, parsed from the real source file."""
    tree = ast.parse(open(SOURCE, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "pptx_export_png":
            return node
    raise AssertionError("pptx_export_png not found in tools/pptx_ops.py")


def _attribute_names(node: ast.AST) -> list[str]:
    """Every attribute name used anywhere under node, e.g. 'Quit' for `ppt.Quit`."""
    return [n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)]


def _called_attribute_names(node: ast.AST) -> list[str]:
    """Attribute names that are actually CALLED, e.g. 'Quit' for `ppt.Quit()`.

    A reference that is never called cannot quit anything; the prohibition is about the
    call. Reading ``pres.FullName`` (an attribute, not a call) is fine and must not trip
    these checks, which is why close/quit are judged on call sites, not bare attributes.
    """
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            out.append(n.func.attr)
    return out


def test_the_function_exists_and_still_drives_powerpoint():
    fn = _export_png_function()
    # Guard against the test passing vacuously if the function is renamed or gutted: it must
    # still open a presentation and export it, which is the behaviour whose teardown we police.
    attrs = _attribute_names(fn)
    assert "Open" in attrs, "pptx_export_png no longer opens a presentation"
    assert "Export" in attrs, "pptx_export_png no longer exports"


def test_it_never_quits_the_application():
    """No Application.Quit() anywhere in the function -- the core ownership violation."""
    called = _called_attribute_names(_export_png_function())
    assert "Quit" not in called, (
        "pptx_export_png calls .Quit(); quitting PowerPoint closes the operator's own open "
        "documents, which this tool is not allowed to do even on a self-dispatched instance")


def test_it_never_kills_the_process_by_name():
    """No taskkill / TerminateProcess / os.kill / Popen('taskkill') style process teardown."""
    fn = _export_png_function()
    called = _called_attribute_names(fn)
    for banned in ("kill", "terminate", "TerminateProcess", "taskkill"):
        assert banned not in called, (
            "pptx_export_png calls .%s(); killing PowerPoint by process is forbidden -- it "
            "destroys documents the tool never opened" % banned)
    # Also catch a kill issued as a shell/string argument rather than a method call.
    for const in ast.walk(fn):
        if isinstance(const, ast.Constant) and isinstance(const.value, str):
            low = const.value.lower()
            assert "taskkill" not in low, "pptx_export_png shells out to taskkill"
            assert "powerpnt" not in low or "/f" not in low, (
                "pptx_export_png force-kills POWERPNT.EXE")


def test_it_never_closes_all_documents_or_all_windows():
    """Close must target the opened document, not Presentations.Close / a Windows sweep.

    ``Application.Presentations.Close`` or iterating ``Application.Windows`` to close them
    would reach other open documents. The only allowed close is on the single presentation
    object the tool opened (``pres.Close()``).
    """
    fn = _export_png_function()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "Close":
            # The receiver of .Close() must be a plain Name (the opened `pres`), not an
            # attribute chain like Application.Presentations or App.Windows.Item(i).
            recv = n.func.value
            assert isinstance(recv, ast.Name), (
                "Close() is called on %s, not on the single opened presentation object; "
                "closing a collection reaches documents the tool never opened"
                % ast.dump(recv))
    attrs = _attribute_names(fn)
    assert "Windows" not in attrs, (
        "pptx_export_png touches Application.Windows; enumerating/closing windows reaches "
        "other documents")


def test_the_only_close_is_on_the_object_that_was_opened():
    """The document closed is the same object returned by Presentations.Open.

    Find the name bound to Presentations.Open(...) and assert every .Close() call uses that
    exact name. This is what 'close only the document I opened' means in code.
    """
    fn = _export_png_function()
    opened_name = None
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            f = n.value.func
            if isinstance(f, ast.Attribute) and f.attr == "Open":
                if len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                    opened_name = n.targets[0].id
    assert opened_name is not None, "could not find the presentation opened by the tool"
    closed_on = [
        n.func.value.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "Close"
        and isinstance(n.func.value, ast.Name)
    ]
    assert closed_on, "the opened document is never closed; it must be (its own file only)"
    for name in closed_on:
        assert name == opened_name, (
            "Close() is called on %r but the tool opened %r; it may only close the document "
            "it opened itself" % (name, opened_name))

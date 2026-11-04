# -*- coding: utf-8 -*-
"""pptx_export_png may close ONLY a document it opened itself -- proven at runtime.

WHY A RUNTIME TEST, NOT JUST THE AST CHECK. The sibling file
``test_pptx_export_png_does_not_quit_powerpoint.py`` parses the source and proves the code
contains no ``Application.Quit``, no process kill, no ``Presentations.Close`` / ``Windows``
sweep, and that the only ``.Close()`` is on the object returned by ``Presentations.Open``.
That is necessary but NOT sufficient for the ownership rule, because of one COM behaviour a
static read cannot see: ``Presentations.Open`` does not always open a new document. When the
requested file is ALREADY OPEN in the application instance, PowerPoint returns a reference to
the EXISTING open presentation instead of opening a second copy. An unconditional
``pres.Close()`` would then close a document the operator opened -- their live work -- even
though, to the AST, the close is "on the object Open returned".

So the rule "close only the document I opened myself" is a RUNTIME property: it depends on
whether the deck was already open when the tool arrived. These tests drive ``pptx_export_png``
with a FAKE PowerPoint COM layer (no pywin32, no Office, no Windows) and assert the two cases
the operator's contract turns on:

  1. The requested deck was ALREADY OPEN (operator's own document): it must be rendered but
     NEVER closed -- the tool is not its owner.
  2. The requested deck was NOT open: the tool opens it, so it owns it, and MUST close exactly
     that one document (and nothing else) on the way out.

The fake also records every Close() target, so a regression that closes someone else's
document -- or quits, or kills -- is caught as a wrong or extra close, not merely as a
missing comment.

HERMETIC. No real COM: pythoncom and win32com.client are injected into sys.modules as fakes
before the tool imports them. Runs on the Linux CI runner like the other hermetic tests.
"""
from __future__ import annotations

import os
import sys
import types
import importlib

import pytest


# ---------------------------------------------------------------------------
# A minimal fake PowerPoint COM object model.
# ---------------------------------------------------------------------------

class FakePageSetup:
    SlideWidth = 1280.0
    SlideHeight = 720.0


class FakePresentation:
    """One open presentation. Records whether Close() was called on it."""

    def __init__(self, full_name, export_log, close_log):
        self.FullName = full_name
        self.PageSetup = FakePageSetup()
        self._export_log = export_log
        self._close_log = close_log
        self.closed = False

    def Export(self, out_dir, fmt, width, height):
        # Record the export and drop a PNG on disk so the tool's count works.
        self._export_log.append((self.FullName, out_dir, fmt, width, height))
        try:
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "Slide1.PNG"), "wb") as fh:
                fh.write(b"\x89PNG\r\n")
        except Exception:
            pass

    def Close(self):
        self.closed = True
        self._close_log.append(self.FullName)


class FakePresentations:
    """The Application.Presentations collection (1-indexed, COM-style)."""

    def __init__(self, export_log, close_log):
        self._items = []            # list[FakePresentation]
        self._export_log = export_log
        self._close_log = close_log
        self.open_calls = []        # every path passed to Open()

    # -- collection access used by the tool's _already_open() --
    @property
    def Count(self):
        return len(self._items)

    def Item(self, i):              # 1-indexed
        return self._items[i - 1]

    # -- test helper: seed a document as already open --
    def _seed_open(self, full_name):
        pres = FakePresentation(full_name, self._export_log, self._close_log)
        self._items.append(pres)
        return pres

    # -- Application.Presentations.Open(...) --
    def Open(self, path, WithWindow=True):
        self.open_calls.append(path)
        # PowerPoint behaviour: if the file is already open, return the EXISTING object
        # rather than opening a second copy. The tool is expected to never reach this when
        # the deck is already open (it checks first), but model it faithfully anyway.
        want = os.path.normcase(os.path.abspath(str(path)))
        for pres in self._items:
            if os.path.normcase(os.path.abspath(str(pres.FullName))) == want:
                return pres
        pres = FakePresentation(str(path), self._export_log, self._close_log)
        self._items.append(pres)
        return pres


class FakeApplication:
    def __init__(self, presentations):
        self.Presentations = presentations
        self.quit_called = False

    def Quit(self):                 # must never be called
        self.quit_called = True


class _FakeWin32ComClient(types.ModuleType):
    """Stands in for win32com.client; DispatchEx returns our FakeApplication."""

    def __init__(self, app):
        super().__init__("win32com.client")
        self._app = app

    def DispatchEx(self, prog_id):
        return self._app

    def Dispatch(self, prog_id):
        return self._app


class _FakePythoncom(types.ModuleType):
    def __init__(self):
        super().__init__("pythoncom")
        self.init_count = 0
        self.uninit_count = 0

    def CoInitialize(self):
        self.init_count += 1

    def CoUninitialize(self):
        self.uninit_count += 1


# ---------------------------------------------------------------------------
# Harness: install the fakes, run the tool, return what happened.
# ---------------------------------------------------------------------------

def _run_export(tmp_path, monkeypatch, seed_open_paths, request_path):
    """Run pptx_export_png against the fake COM layer.

    seed_open_paths: files to mark as already open before the call.
    request_path: the pptx the tool is asked to export.
    Returns (presentations_collection, app, result_string).
    """
    export_log: list = []
    close_log: list = []
    presentations = FakePresentations(export_log, close_log)
    for sp in seed_open_paths:
        presentations._seed_open(sp)
    app = FakeApplication(presentations)

    fake_client = _FakeWin32ComClient(app)
    fake_win32com = types.ModuleType("win32com")
    # `import win32com.client` resolves `client` as an ATTRIBUTE of the win32com package
    # object after importing the submodule, so the fake parent must carry it explicitly --
    # registering it in sys.modules alone is not enough for the attribute access to succeed.
    fake_win32com.client = fake_client
    monkeypatch.setitem(sys.modules, "pythoncom", _FakePythoncom())
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client)

    # Scope the file tools to tmp_path so _validate_path accepts our paths.
    monkeypatch.setenv("MCP_ALLOWED_BASE", str(tmp_path))
    import tools.file_ops as file_ops
    importlib.reload(file_ops)
    import tools.pptx_ops as pptx_ops
    importlib.reload(pptx_ops)

    result = pptx_ops.pptx_export_png(str(request_path), output_dir=str(tmp_path / "out"))
    return presentations, app, result, close_log, export_log


def _make_pptx(tmp_path, name):
    """Create a file the tool will accept as an existing .pptx."""
    p = tmp_path / name
    p.write_bytes(b"PK\x03\x04 fake pptx")   # is_file() is all the tool checks
    return p


# ---------------------------------------------------------------------------
# Case 1: the operator already had this deck open -> never close it.
# ---------------------------------------------------------------------------

def test_a_document_already_open_is_rendered_but_never_closed(tmp_path, monkeypatch):
    deck = _make_pptx(tmp_path, "operators_live_work.pptx")

    presentations, app, result, close_log, export_log = _run_export(
        tmp_path, monkeypatch,
        seed_open_paths=[str(deck)],
        request_path=deck,
    )

    # It still rendered (the tool's job), so the export happened on the existing object.
    assert export_log, "the already-open deck was not exported: %r" % result
    assert "Exported" in result, result

    # THE CONTRACT: the operator's own open document must NOT have been closed.
    assert close_log == [], (
        "pptx_export_png closed a document that was ALREADY OPEN when it arrived; that is the "
        "operator's live work, which the tool does not own and must never close. Closed: %r"
        % close_log)
    # It must not have opened a second copy either (Open not called for an already-open deck).
    assert presentations.open_calls == [], (
        "the deck was already open, yet Open() was called: %r" % presentations.open_calls)
    # And it certainly must not have quit the application.
    assert app.quit_called is False, "pptx_export_png called Application.Quit()"


# ---------------------------------------------------------------------------
# Case 2: the tool opened the deck itself -> it owns it and must close that one.
# ---------------------------------------------------------------------------

def test_a_document_this_tool_opened_is_closed(tmp_path, monkeypatch):
    deck = _make_pptx(tmp_path, "freshly_rendered.pptx")

    presentations, app, result, close_log, export_log = _run_export(
        tmp_path, monkeypatch,
        seed_open_paths=[],          # nothing open to begin with
        request_path=deck,
    )

    assert "Exported" in result, result
    # It opened exactly the requested deck...
    assert len(presentations.open_calls) == 1, (
        "expected exactly one Open() of the requested deck, got %r" % presentations.open_calls)
    want = os.path.normcase(os.path.abspath(str(deck)))
    # ...and closed exactly that one document, nothing else.
    assert [os.path.normcase(os.path.abspath(c)) for c in close_log] == [want], (
        "the tool opened the deck itself, so it must close exactly that one document; "
        "closed: %r" % close_log)
    assert app.quit_called is False, "pptx_export_png called Application.Quit()"


# ---------------------------------------------------------------------------
# Case 3: another document is open; the tool opens a DIFFERENT one -> close only its own.
# ---------------------------------------------------------------------------

def test_another_open_document_is_left_untouched(tmp_path, monkeypatch):
    other = _make_pptx(tmp_path, "someone_elses_deck.pptx")
    mine = _make_pptx(tmp_path, "the_one_i_export.pptx")

    presentations, app, result, close_log, export_log = _run_export(
        tmp_path, monkeypatch,
        seed_open_paths=[str(other)],      # a different doc is already open
        request_path=mine,                 # we export our own, not that one
    )

    assert "Exported" in result, result
    want = os.path.normcase(os.path.abspath(str(mine)))
    other_norm = os.path.normcase(os.path.abspath(str(other)))

    # Only our own document was closed; the pre-existing other document was not.
    closed_norm = [os.path.normcase(os.path.abspath(c)) for c in close_log]
    assert closed_norm == [want], (
        "expected to close only the document we opened (%r); closed %r" % (want, close_log))
    assert other_norm not in closed_norm, (
        "pptx_export_png closed another already-open document: %r" % other)
    assert app.quit_called is False, "pptx_export_png called Application.Quit()"

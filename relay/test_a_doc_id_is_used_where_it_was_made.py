# -*- coding: utf-8 -*-
"""Close the standing item "docId の寿命" by turning the sentence into a guard that fires.

transport_policy.py says, and has said since 2026-09-18: "nothing caches a docId today --
every attachment uploads immediately before the frame that carries it -- so the answer changes
no decision now. It would matter the moment something reuses one." That sentence is not
re-read by anything. This file is the thing that would notice the moment it stopped being true.

TWO KINDS OF EVIDENCE, BOTH REQUIRED:

  1. BEHAVIOUR, by executing `relay.socket_attachment.annotation_for` with fakes standing in
     for the network (a fake Playwright context/page, a faked `relay.agent_profiles.upload_file`,
     a faked `requests.post`) -- the real seams the module itself uses, read out of
     relay/socket_attachment.py before writing this. This proves:
       (a) one attachment -> exactly one upload (one call to the faked `requests.post`)
       (b) the docId placed in the returned `messageAnnotations` frame is the one THAT upload
           returned, not some other value
       (c) a second `annotation_for(...)` call for the same file uploads again -- the second
           faked upload returns a different docId, and the second frame carries THAT one, not
           the first.

  2. STRUCTURE, by walking the AST of every file under relay/ for a module- or class-level
     container (dict/list/set/OrderedDict/defaultdict/Counter/...) whose name looks like a
     docId cache. This is a narrow, name-based heuristic and it says so out loud:

       WHAT IT CATCHES: `_DOC_ID_CACHE = {}`, `DOCID_SEEN = set()`, `_doc_ids: dict = {}` and
       similar, sitting directly in a module body or a class body (this includes a class's
       `__init__`? NO -- it does not descend into function bodies at all, on purpose: a
       function-LOCAL dict that lives only for the duration of one call is not a cache in the
       sense this guard cares about, and flagging it would make the guard fire on ordinary
       code such as the `headers = dict(...)` local inside `annotation_for` itself).

       WHAT IT CANNOT CATCH, stated so nobody mistakes silence for proof:
         - a cache built through `setattr`, `globals()[...]`, or any other dynamic assignment
         - a cache whose name does not contain some spelling of "doc" + "id" (e.g. a
           generically-named `_state = {}` used to remember a docId)
         - a cache that lives in a class instance attribute assigned inside `__init__`
           (`self._doc_id_cache = {}`), because that is a per-instance runtime value, not
           something `ast.parse` of the source can see as "existing" independent of a call
         - anything outside the `relay/` tree

     It is a tripwire for the straightforward, honestly-named version of the mistake, not a
     proof that no caching of any shape exists anywhere in the process.

IF THIS TEST IS EVER CHANGED TO ALLOW REUSE, MEASURE THE LIFETIME FIRST. It has never been
measured. What to measure: upload a file, wait N minutes (start with something like 5, 30, and
whatever the socket route's own turn/session lifetime is), send the SAME docId as a
messageAnnotations entry on a fresh turn, and check whether the reply can still read the file
back (the same phrase-in-an-image check that first proved the docId path works at all, recorded
in relay/transport_policy.py). Only a docId shown to survive that should ever be reused, and
only after the wait that was actually tested is written down next to the code that relies on it.
"""
from __future__ import annotations

import ast
import glob
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import socket_attachment as SA  # noqa: E402


# ── fakes for the network seams `annotation_for` actually has ──────────────────────────────
#
# Read from relay/socket_attachment.py before writing these: the function takes a live
# Playwright `context`, opens a page, registers a "request" listener, calls
# `relay.agent_profiles.upload_file(page, upload_path)` (imported INSIDE the function, so it
# is patched by name on that module, not on this one), waits for its own listener to have
# captured the page's own upload request, then does `import requests; requests.post(...)`
# (also imported inside the function, so `requests.post` itself is the seam).

class _FakeRequest:
    """Stands in for a Playwright `Request`: `.url` is a property, `.post_data_buffer` is an
    attribute, `.all_headers()` is a method -- matching exactly what `_on_request` reads."""

    def __init__(self, url, body, headers):
        self.url = url
        self.post_data_buffer = body
        self._headers = dict(headers)

    def all_headers(self):
        return dict(self._headers)


class _FakePage:
    """Stands in for a Playwright `Page`. `fire_upload_request` is not part of the real Page
    API -- it is how this fake lets the faked `upload_file` announce "the page just made its
    own UploadFile request", which is what `_on_request` is listening for."""

    def __init__(self, upload_url):
        self._upload_url = upload_url
        self._handler = None
        self.closed = False

    def on(self, event, handler):
        if event == "request":
            self._handler = handler

    def goto(self, url, wait_until=None, timeout=None):
        pass

    def wait_for_timeout(self, ms):
        pass

    def close(self):
        self.closed = True

    def fire_upload_request(self, body, headers):
        assert self._handler is not None, "annotation_for never registered a request listener"
        self._handler(_FakeRequest(self._upload_url, body, headers))


class _FakeContext:
    """Stands in for the Playwright `BrowserContext`; `new_page()` is the only method used."""

    def __init__(self):
        self.pages = []

    def new_page(self):
        p = _FakePage(SA.UPLOAD_URL)
        self.pages.append(p)
        return p


def _fake_upload_file(body=b"\x89PNG\r\n fake bytes", headers=None):
    """A drop-in for `relay.agent_profiles.upload_file`: instead of driving a real
    <input type=file>, it makes the fake page fire the same "request" event the real page's
    own UploadFile POST would raise, then reports success -- exactly the two effects
    `annotation_for` reads (the captured request, and a truthy return)."""
    headers = dict(headers or {"x-anchormailbox": "test@example.com",
                                "content-type": "multipart/form-data; boundary=x"})

    def _upload(page, path):
        page.fire_upload_request(body, headers)
        return True

    return _upload


class _FakeResponse:
    def __init__(self, doc_id, file_name="pic.png"):
        self.status_code = 200
        self._doc_id = doc_id
        self._file_name = file_name

    def json(self):
        return {"docId": self._doc_id, "fileName": self._file_name,
                "fileSanitizer": "ImageSanitizerBingAI",
                "result": {"value": "Success", "message": "Success"}}


def _fake_post(doc_ids):
    """A drop-in for `requests.post`. Each call pops the next docId off `doc_ids` and records
    the call, so a test can assert both HOW MANY uploads happened and WHICH docId each one
    returned -- the two facts (a)/(b)/(c) in the module docstring need."""
    calls = []
    remaining = list(doc_ids)

    def _post(url, headers=None, data=None, timeout=None):
        calls.append({"url": url, "headers": dict(headers or {}), "data": data,
                      "timeout": timeout})
        assert remaining, "requests.post was called more times than the test stocked docIds for"
        return _FakeResponse(remaining.pop(0))

    _post.calls = calls
    return _post


def _sample_file(tmp_path, name="pic.png"):
    f = tmp_path / name
    f.write_bytes(b"\x89PNG\r\n\x1a\n not a real png, just bytes")
    return str(f)


# ── (a) + (b): one attachment, one upload, the returned docId is THAT upload's ─────────────

def test_one_attachment_triggers_exactly_one_upload_and_carries_that_uploads_docid(
        tmp_path, monkeypatch):
    path = _sample_file(tmp_path)
    fake_post = _fake_post(["DOC-FIRST"])
    monkeypatch.setattr("relay.agent_profiles.upload_file", _fake_upload_file())
    monkeypatch.setattr("requests.post", fake_post)

    result = SA.annotation_for(_FakeContext(), "https://example.test/agent", path, "tok-1")

    assert len(fake_post.calls) == 1, "one attachment should cause exactly one upload"
    assert result is not None, "the fakes were wired to succeed; annotation_for returned None"
    assert result[0]["id"] == "DOC-FIRST", (
        "the frame's docId must be the one the upload actually returned")
    assert result[0]["messageAnnotationType"] == "ImageFile"


# ── (c): a second send of the same file uploads again, it does not reuse the first docId ───

def test_a_second_send_of_the_same_file_uploads_again_rather_than_reusing_the_docid(
        tmp_path, monkeypatch):
    path = _sample_file(tmp_path)
    fake_post = _fake_post(["DOC-FIRST", "DOC-SECOND"])
    monkeypatch.setattr("relay.agent_profiles.upload_file", _fake_upload_file())
    monkeypatch.setattr("requests.post", fake_post)
    ctx = _FakeContext()

    first = SA.annotation_for(ctx, "https://example.test/agent", path, "tok-1")
    second = SA.annotation_for(ctx, "https://example.test/agent", path, "tok-1")

    assert len(fake_post.calls) == 2, (
        "a second send of the same file must upload again, not reuse the first response")
    assert first is not None and second is not None
    assert first[0]["id"] == "DOC-FIRST"
    assert second[0]["id"] == "DOC-SECOND", (
        "the second frame reused the first upload's docId instead of using its own")


# ── module-level guard: no docId cache anywhere under relay/ ───────────────────────────────

import re as _re  # noqa: E402

#: Matches some spelling of "doc" immediately followed by "id" (with an optional separator),
#: case-insensitively, anywhere in a name -- "_DOC_ID_CACHE", "docIdSeen", "DOCID_STORE".
_DOC_ID_NAME_RE = _re.compile(r"doc[_\-]?id", _re.IGNORECASE)

#: Container shapes a cache is plausibly built from. A `str`/`int`/`None` literal, or a
#: function call that is not one of these, is not flagged -- e.g. `DOC_ID_FIELD = "docId"`,
#: which names a JSON key rather than storing values, is left alone.
_CONTAINER_CALL_NAMES = {"dict", "list", "set", "OrderedDict", "defaultdict", "Counter", "deque"}


def _is_container_literal_or_call(value_node):
    if isinstance(value_node, (ast.Dict, ast.List, ast.Set)):
        return True
    if isinstance(value_node, ast.Call):
        func = value_node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in _CONTAINER_CALL_NAMES:
            return True
    return False


def _module_and_class_level_assignments(tree):
    """(name, lineno) for every plain/annotated assignment sitting directly in a module body
    or a class body. Deliberately does NOT recurse into function/method bodies -- see the
    module docstring's "WHAT IT CANNOT CATCH" for why that is a stated limit, not an oversight.
    """
    def walk(body):
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    if isinstance(t, ast.Name) and _is_container_literal_or_call(stmt.value):
                        yield t.id, stmt.lineno
            elif isinstance(stmt, ast.AnnAssign):
                if (isinstance(stmt.target, ast.Name) and stmt.value is not None
                        and _is_container_literal_or_call(stmt.value)):
                    yield stmt.target.id, stmt.lineno
            elif isinstance(stmt, ast.ClassDef):
                yield from walk(stmt.body)
    yield from walk(tree.body)


def test_no_module_or_class_level_docid_cache_exists_under_relay():
    """The structural half of the guard. See the module docstring for exactly what this can
    and cannot see -- it is a name-based heuristic over module/class bodies only, not a proof
    that no docId is ever remembered anywhere in the process."""
    offenders = []
    for path in sorted(glob.glob(os.path.join(REPO, "relay", "**", "*.py"), recursive=True)):
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError:
            continue
        for name, lineno in _module_and_class_level_assignments(tree):
            if _DOC_ID_NAME_RE.search(name):
                offenders.append("%s:%d %s" % (os.path.relpath(path, REPO), lineno, name))

    assert offenders == [], (
        "found what looks like a module- or class-level docId cache -- a docId's lifetime on "
        "the server has never been measured (see relay/transport_policy.py's \"STILL OPEN\" "
        "paragraph), so nothing may cache one until that measurement exists:\n  "
        + "\n  ".join(offenders))

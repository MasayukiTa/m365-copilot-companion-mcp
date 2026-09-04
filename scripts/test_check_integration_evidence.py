# -*- coding: utf-8 -*-
"""The gate that polices new capabilities was reporting clean because it read nothing.

`_git` ran with `text=True` and no `encoding=`, so the child was decoded with the LOCALE
codec -- cp932 on a Japanese Windows install. This repository's diffs are full of UTF-8
Japanese, so the decode raised inside subprocess's reader thread: the traceback belonged to
no caller, `r.stdout` came back empty, and `or ""` handed that to a caller that reads an
empty diff as "nothing changed".

Measured on the commit before the fix:

    file list      19 paths      (ASCII -- arrived intact)
    unified diff    0 characters (true size 14,482)  <-- the part that says what is NEW

So no line was ever new, no definition was ever added, and the gate printed
"no new public definitions" -- the same sentence it prints when a change genuinely adds
none. The one case needing attention looked exactly like the common case that does not.

These build a real git repository with Japanese in it and run the checker against it, so
they exercise the decode rather than asserting something about the source text.
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_integration_evidence as G  # noqa: E402

pytestmark = pytest.mark.skipif(
    subprocess.call(["git", "--version"], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL) != 0,
    reason="git is not available")

JAPANESE = "# 日本語のコメント。これが cp932 で読めずゲートが盲目になっていた。\n"


def _repo(tmp_path):
    def git(*a):
        subprocess.run(["git"] + list(a), cwd=str(tmp_path), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    os.makedirs(os.path.join(str(tmp_path), "tools"))
    base = os.path.join(str(tmp_path), "tools", "m.py")
    with open(base, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("x = 1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    return tmp_path, git, base


def _run_against(tmp_path, *args, **kw):
    """Point the checker at `tmp_path` instead of this repository."""
    old = G.ROOT
    G.ROOT = str(tmp_path)
    try:
        return G._git(*args, **kw)
    finally:
        G.ROOT = old


def _added_in(tmp_path, base):
    old = G.ROOT
    G.ROOT = str(tmp_path)
    try:
        return G.added_definitions(base)
    finally:
        G.ROOT = old


# -- the measured failure --------------------------------------------------------------

def test_a_diff_containing_japanese_is_read_not_dropped(tmp_path):
    """THE BUG. Under cp932 this came back as 0 characters and the gate called it clean."""
    tmp_path, git, path = _repo(tmp_path)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(JAPANESE + "def brand_new_capability():\n    return 1\n")
    git("add", "-A")
    git("commit", "-qm", "add")

    diff = _run_against(tmp_path, "diff", "--unified=0", "HEAD~1", "--", "tools/")
    assert diff.strip(), "the diff came back empty; the checker is blind again"
    assert "brand_new_capability" in diff


def test_the_new_definition_is_actually_found_through_that_diff(tmp_path):
    """The end the gate cares about: a capability added alongside Japanese must be seen."""
    tmp_path, git, path = _repo(tmp_path)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(JAPANESE + "def brand_new_capability():\n    return 1\n")
    git("add", "-A")
    git("commit", "-qm", "add")

    assert _added_in(tmp_path, "HEAD~1") == {"brand_new_capability": "tools/m.py"}


def test_a_change_that_adds_nothing_still_reports_nothing(tmp_path):
    """The fix must not turn every commit into a finding."""
    tmp_path, git, path = _repo(tmp_path)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(JAPANESE)
    git("add", "-A")
    git("commit", "-qm", "comment only")
    assert _added_in(tmp_path, "HEAD~1") == {}


# -- unreadable is not clean -------------------------------------------------------------

def test_a_failed_read_raises_instead_of_returning_empty():
    """"" was doing two jobs: "nothing changed" and "could not look". They are not the same
    answer and only one of them should pass."""
    with pytest.raises(G.GitUnreadable):
        G._git("diff", "no-such-ref-exists-anywhere", required=True)


def test_an_unrequired_read_still_degrades_quietly():
    """main() uses a failing rev-parse as its shallow-clone signal, so that path must keep
    returning "" rather than raising."""
    assert G._git("rev-parse", "--verify", "--quiet", "no-such-ref^{commit}") == ""


def test_the_checker_exits_nonzero_when_it_cannot_read(tmp_path):
    tmp_path, git, path = _repo(tmp_path)
    old = G.ROOT
    G.ROOT = str(tmp_path)
    try:
        # A base that resolves but a diff that cannot be produced is the shape being guarded;
        # simulate the unreadable half directly.
        def boom(*a, **k):
            raise G.GitUnreadable("simulated")
        real, G.added_definitions = G.added_definitions, boom
        try:
            assert G.main(["--base", "HEAD"]) == 2
        finally:
            G.added_definitions = real
    finally:
        G.ROOT = old


def test_a_missing_base_is_still_a_skip_not_a_failure(tmp_path):
    """Shallow CI checkouts have no HEAD~1; that was a deliberate pass and must stay one."""
    tmp_path, git, path = _repo(tmp_path)
    old = G.ROOT
    G.ROOT = str(tmp_path)
    try:
        assert G.main(["--base", "HEAD~50"]) == 0
    finally:
        G.ROOT = old


# -- a decorator can BE the wiring ---------------------------------------------------------

FIXTURE_SRC = (
    "import pytest\n"
    "\n"
    "\n"
    "@pytest.fixture(autouse=True)\n"
    "def trusted_skills():\n"
    "    return 1\n"
)

CACHED_SRC = (
    "import functools\n"
    "\n"
    "\n"
    "@functools.lru_cache()\n"
    "def brand_new_capability():\n"
    "    return 1\n"
)


def _commit_appending(tmp_path, source):
    tmp_path, git, path = _repo(tmp_path)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(source)
    git("add", "-A")
    git("commit", "-qm", "add")
    return tmp_path


def test_a_pytest_fixture_is_not_reported_as_dead_code(tmp_path):
    """FOUND BY THE GATE FAILING ON A REAL COMMIT. An autouse fixture is referenced by
    nobody anywhere -- that is what autouse means -- so without this, every fixture ever
    added is reported as a new definition nothing calls. The checker's own rule already
    counts a registration point as evidence; a decorator that hands the function to a
    framework is that, said in one line."""
    assert _added_in(_commit_appending(tmp_path, FIXTURE_SRC), "HEAD~1") == {}


def test_an_ordinary_decorated_function_is_still_checked(tmp_path):
    """The exemption is for decorators that register, not for any decorator at all --
    otherwise @lru_cache would hide a capability nothing calls."""
    assert _added_in(_commit_appending(tmp_path, CACHED_SRC), "HEAD~1") == {
        "brand_new_capability": "tools/m.py"}

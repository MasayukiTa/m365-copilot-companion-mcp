"""What the foundry's loader is allowed to publish as a live tool.

The loader had no tests, which is how it came to publish 55 pytest functions and fixtures as
MCP tools. They were reachable by any connected agent, and they pushed the server to 72 tools
past a client that stops at 70 -- so the cap that keeps unlock and the loop protocol reachable
was being set and then walked past.
"""
from __future__ import annotations

import io
import os
import sys
import textwrap

import pytest

from tools import auto_loader


@pytest.fixture
def auto_pkg(tmp_path, monkeypatch):
    """A package of this test's own, shaped like tools/auto and loaded the same way."""
    pkg = tmp_path / "autotest_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    def write(name, body):
        (pkg / (name + ".py")).write_text(textwrap.dedent(body), encoding="utf-8")

    def load():
        for m in [k for k in list(sys.modules) if k.startswith("autotest_pkg")]:
            del sys.modules[m]
        return auto_loader.load_auto_tools("autotest_pkg")

    return write, load


def _names(pairs):
    return sorted(f.__name__ for _, f in pairs)


def test_a_forged_tool_is_published(auto_pkg):
    write, load = auto_pkg
    write("widget", '''
        def widget_count(path: str = "."):
            """One line."""
            return 1
    ''')
    assert _names(load()) == ["widget_count"]


def test_the_module_of_tests_beside_it_is_not(auto_pkg):
    """The foundry writes a module and its tests together; only one of them is a tool."""
    write, load = auto_pkg
    write("widget", '''
        def widget_count(path: str = "."):
            """One line."""
            return 1
    ''')
    write("test_widget", '''
        def helper():
            return 1

        def test_widget_counts():
            assert helper() == 1
    ''')
    assert _names(load()) == ["widget_count"]


def test_a_test_function_is_skipped_wherever_it_lives(auto_pkg):
    """Not every test sits in a test_-named module, and the module name is not the property
    that matters -- being callable by a remote agent is."""
    write, load = auto_pkg
    write("widget", '''
        def widget_count(path: str = "."):
            """One line."""
            return 1

        def test_widget_count_is_one():
            assert widget_count() == 1
    ''')
    assert _names(load()) == ["widget_count"]


def test_a_fixture_is_skipped_although_its_name_looks_ordinary(auto_pkg):
    """This is the half a name filter misses. fake_cell, always, both, repo and text were all
    published as tools; none of them has a test_ prefix."""
    write, load = auto_pkg
    write("widget", '''
        import pytest

        @pytest.fixture
        def fake_cell():
            return object()

        def widget_count(path: str = "."):
            """One line."""
            return 1
    ''')
    assert _names(load()) == ["widget_count"]


def test_an_empty_package_is_not_an_error(auto_pkg):
    write, load = auto_pkg
    assert load() == []


def test_the_live_package_publishes_no_tests():
    """The real tools/auto, as it stands on disk. This is the assertion that would have caught
    it: it does not care how the filter is written, only what comes out."""
    published = _names(auto_loader.load_auto_tools())
    leaked = [n for n in published if n.startswith("test_")]
    assert not leaked, "pytest functions are registered as live MCP tools: %s" % leaked

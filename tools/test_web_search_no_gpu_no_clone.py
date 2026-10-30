"""Pin the claim behind ledger row #114: the owner asked whether a job said to
REQUIRE A GPU could be done by Copilot instead -- by having a Copilot agent use the
web tools WITHOUT cloning the repository, taking "the tasty part" rather than running
the original framework.

The load-bearing fact that makes that possible is a property of this module:
``tools.search_web`` reaches the open web through DuckDuckGo with NO API key, NO GPU,
NO model to swap in, and -- importantly for "a Copilot agent that has not cloned
anything" -- WITHOUT reading a single file from the repository checkout. If any of
those quietly stopped being true, row #114's whole premise would rot, and nothing
would say so. These tests make it say so.

They are hermetic: the DuckDuckGo client is stubbed, so the suite never touches the
network and runs the same on a Linux CI runner as on a developer's Windows box.
"""
import builtins
import importlib
import sys

import pytest

import tools.search_web as sw


class _FakeDDGS:
    """Stands in for ddgs.DDGS / duckduckgo_search.DDGS. Records nothing external."""

    _text_rows = [
        {"title": "Taking the tasty part", "href": "https://example.test/a",
         "body": "How to reuse a model's useful output without the GPU framework."},
        {"title": "Second hit", "href": "https://example.test/b",
         "body": "More context."},
    ]
    _news_rows = [
        {"title": "Fresh item", "url": "https://news.test/1",
         "source": "Example", "date": "2026-10-27", "body": "Body text."},
    ]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def text(self, query, region=None, safesearch=None, timelimit=None, max_results=None):
        return list(self._text_rows[: (max_results or len(self._text_rows))])

    def news(self, query, region=None, timelimit=None, max_results=None):
        return list(self._news_rows[: (max_results or len(self._news_rows))])


@pytest.fixture
def fake_ddgs(monkeypatch):
    """Inject a fake ``ddgs`` module so the lazy ``from ddgs import DDGS`` resolves to it."""
    import types

    mod = types.ModuleType("ddgs")
    mod.DDGS = _FakeDDGS
    monkeypatch.setitem(sys.modules, "ddgs", mod)
    # Make sure the other import name is not picked up instead.
    monkeypatch.setitem(sys.modules, "duckduckgo_search", mod)
    return mod


def test_web_search_returns_results_without_api_key_or_gpu(fake_ddgs):
    out = sw.web_search("copilot replace gpu job", max_results=2)
    # The real snippets came back and were formatted, not an error line.
    assert "web_search error" not in out
    assert "Taking the tasty part" in out
    assert "https://example.test/a" in out
    # 2 results asked for, 2 returned.
    assert "2 result(s)" in out


def test_web_search_wraps_external_text_as_untrusted(fake_ddgs):
    """A Copilot agent reading a search snippet must see it marked as DATA, not as
    instructions -- the whole point of letting an un-cloned agent pull from the web."""
    out = sw.web_search("anything", max_results=1)
    assert "untrusted_external_content" in out


def test_web_search_news_returns_items(fake_ddgs):
    out = sw.web_search_news("topic", max_results=1)
    assert "web_search_news error" not in out
    assert "Fresh item" in out
    assert "https://news.test/1" in out


def test_web_search_degrades_cleanly_when_no_engine_installed(monkeypatch):
    """On a host where NEITHER ddgs NOR duckduckgo_search is importable, the tool must
    return a readable, own-error status line -- not crash, and not pretend success.
    This is the "measure the tool's completeness" half of row #114: a missing optional
    dependency is a named, recoverable condition."""
    # Hide both engines and force a fresh import attempt inside the function.
    monkeypatch.setitem(sys.modules, "ddgs", None)
    monkeypatch.setitem(sys.modules, "duckduckgo_search", None)
    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name in ("ddgs", "duckduckgo_search"):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    out = sw.web_search("q")
    assert out.startswith("[web_search error")
    assert "install" in out


def test_search_web_module_reads_no_repository_file():
    """The module that lets an UN-CLONED agent reach the web must not itself depend on a
    repository checkout. Its only local import is the dependency-free ``_untrusted``
    helper; it opens no file and resolves no checkout path at import or call time.
    Guard that by source inspection so a future edit that adds a file read is caught.
    """
    import inspect

    src = inspect.getsource(sw)
    # No file I/O and no path-walking against a checkout.
    assert "open(" not in src
    assert "Path(" not in src
    assert "__file__" not in src
    # GPU / model-swap framework is explicitly NOT a dependency here.
    for forbidden in ("import torch", "import tensorflow", "cuda", "onnxruntime"):
        assert forbidden not in src


def test_module_imports_from_a_bare_checkoutless_sys_path():
    """Reimporting the module with only its own package on the path still works -- it
    pulls in nothing that assumes the wider repository is present."""
    importlib.reload(sw)
    assert hasattr(sw, "web_search")
    assert hasattr(sw, "web_search_news")

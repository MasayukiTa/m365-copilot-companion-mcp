# -*- coding: utf-8 -*-
"""Hermetic tests for copilot_image (no Edge, no playwright, no network).

WHAT THESE PROVE. The tool is importable with no optional dependency installed (playwright is
imported INSIDE the function, not at module top), it exposes exactly one public callable so the
auto-loader publishes `copilot_generate_image` and nothing else, it rejects empty inputs before
touching a browser, it degrades to a clear one-line message when playwright is absent, and its
private PNG writer round-trips base64 to bytes on disk. None of this drives a real browser, so
it runs on the Linux CI runner.
"""
from __future__ import annotations

import base64
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.auto import copilot_image as C


def test_module_imports_without_playwright():
    # The module must import even where playwright is not installed: the import is deferred
    # into copilot_generate_image. If this file imported the module and playwright were a
    # top-level import, collection itself would fail on a runner without it.
    assert hasattr(C, "copilot_generate_image")
    assert inspect.isfunction(C.copilot_generate_image)


def test_exactly_one_public_callable_is_published():
    # The auto-loader publishes every public top-level function whose __module__ is this
    # module. Anything else here (helpers) must start with "_" so it is not published as a
    # live MCP tool. Guard against a second public function leaking in later.
    public = [
        name for name, obj in vars(C).items()
        if not name.startswith("_")
        and inspect.isfunction(obj)
        and obj.__module__ == C.__name__
    ]
    assert public == ["copilot_generate_image"], public


def test_signature_is_prompt_then_out_path():
    params = list(inspect.signature(C.copilot_generate_image).parameters)
    assert params[:2] == ["prompt", "out_path"]


def test_empty_prompt_is_rejected_without_a_browser():
    msg = C.copilot_generate_image("", "/tmp/does_not_matter.png")
    assert "prompt is empty" in msg


def test_empty_out_path_is_rejected_without_a_browser():
    msg = C.copilot_generate_image("a mountain", "")
    assert "out_path is empty" in msg


def test_missing_playwright_returns_a_clear_message(monkeypatch):
    # Simulate playwright not being importable and confirm the tool says so in one line
    # rather than raising. We block the import by inserting a meta-path finder that fails
    # for 'playwright' and its submodules.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "playwright" or name.startswith("playwright."):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    msg = C.copilot_generate_image("a mountain", "/tmp/x.png")
    assert "playwright not available" in msg


def test_save_png_round_trips_base64(tmp_path):
    raw = b"\x89PNG\r\n\x1a\n" + b"payload-bytes-123"
    b64 = base64.b64encode(raw).decode("ascii")
    out = tmp_path / "sub" / "img.png"
    n = C._save_png(b64, str(out))
    assert n == len(raw)
    assert out.read_bytes() == raw


def test_this_module_satisfies_the_loader_filters():
    # Prove registration WITHOUT importing the sibling tools/auto modules: load_auto_tools()
    # imports every module in the package, and at least one sibling pulls in fastmcp at import
    # time (~80s), which would make this test slow and flaky in CI for reasons unrelated to
    # this tool. Instead, assert against the loader's own pure filter predicates and this
    # module's real attributes -- the exact conditions load_auto_tools() applies per module.
    from tools.auto_loader import _is_test_module, _is_test_function

    mod_name = "copilot_image"
    assert not mod_name.startswith("_")
    assert not _is_test_module(mod_name)

    # The one public, in-module function the loader would collect from this module.
    collected = [
        name for name, obj in vars(C).items()
        if not name.startswith("_")
        and not _is_test_function(name, obj)
        and inspect.isfunction(obj)
        and obj.__module__ == C.__name__
    ]
    assert collected == ["copilot_generate_image"], collected

    # And a module-level opt-out gate, if ever added, must not be present as a published tool
    # name (it would start with "_"); confirm we are not accidentally exposing one.
    gate = getattr(C, "_auto_tools_enabled", None)
    assert gate is None or callable(gate)

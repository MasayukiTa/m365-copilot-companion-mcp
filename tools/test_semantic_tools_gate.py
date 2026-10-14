# -*- coding: utf-8 -*-
"""The semantic tools return business data, so they are gated like odbc_query, and they are
reachable through the catalogue (gateway) rather than as extra direct tools."""
import inspect
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import tool_catalogue as C  # noqa: E402
from tools.semantic import ops, store  # noqa: E402

NAMES = ("semantic_resolve", "metric_sql", "catalog_search", "catalog_describe",
         "catalog_joins")
CALLS = {
    "semantic_resolve": ("sales",),
    "metric_sql": ("total_sales",),
    "catalog_search": ("orders",),
    "catalog_describe": ("orders",),
    "catalog_joins": ("orders", "customers"),
}


@pytest.fixture()
def folder(tmp_path, monkeypatch):
    d = tmp_path / "sem"
    d.mkdir()
    (d / "glossary.json").write_text(json.dumps({"terms": [{"term": "sales"}]}), encoding="utf-8")
    monkeypatch.setenv(store.ENV_VAR, str(d))
    store._CACHE.clear()
    return d


@pytest.mark.parametrize("name", NAMES)
def test_locked_call_is_refused_with_the_standard_prefix(name, folder):
    out = getattr(ops, name)(*CALLS[name])
    assert isinstance(out, str) and out.startswith("[locked")


@pytest.mark.parametrize("name", NAMES)
def test_every_tool_calls_the_gate_first(name):
    src = inspect.getsource(getattr(ops, name))
    body = src.split('"""')[2]
    assert body.lstrip().startswith("locked = require_unlocked()")


def test_unlocked_calls_return_json(folder, monkeypatch):
    monkeypatch.setattr(ops, "require_unlocked", lambda: None)
    out = json.loads(ops.semantic_resolve("sales"))
    assert out["ok"] is True and out["matches"][0]["name"] == "sales"
    for name in NAMES[1:]:
        assert isinstance(json.loads(getattr(ops, name)(*CALLS[name])), dict)


def test_unconfigured_is_a_clear_json_error(monkeypatch):
    monkeypatch.delenv(store.ENV_VAR, raising=False)
    store._CACHE.clear()
    monkeypatch.setattr(ops, "require_unlocked", lambda: None)
    out = json.loads(ops.catalog_search("x"))
    assert out["ok"] is False and "MCP_SEMANTIC_DIR" in out["error"]


def test_bad_today_is_an_error_not_a_crash(folder, monkeypatch):
    monkeypatch.setattr(ops, "require_unlocked", lambda: None)
    out = json.loads(ops.semantic_resolve("sales", today="not-a-date"))
    assert out["ok"] is False


def test_the_tools_are_in_the_data_category():
    fake = {n: getattr(ops, n) for n in NAMES}
    assert {C.categorise(n) for n in NAMES} == {"data"}
    assert sorted(sum(C.by_category(fake).values(), [])) == sorted(NAMES)


def test_registered_through_the_gateway_and_listed():
    os.environ.setdefault("MCP_API_KEY", "test-only")
    os.environ.setdefault("MCP_TOOL_MAP", "1")
    try:
        import importlib
        main = importlib.import_module("main")
    except Exception as exc:                       # pragma: no cover - env-dependent
        pytest.skip("main.py not importable here: %s" % exc)
    allt = main._ALL_TOOLS
    for n in NAMES:
        assert n in allt
        assert C.categorise(n) == "data"
    listing = C.render_category(allt, "data")
    for n in NAMES:
        assert "  %s -- " % n in listing

"""Hermetic tests for the alias/synonym store (tools/data_aliases.py).

No real business vocabulary: fake Japanese terms only (e.g. "原料"/"投入"/
"配合" as generic material-word synonyms, not any real internal DB term).
The alias store path is monkeypatched to a tmp file for every test, so
nothing ever touches (or creates) the real .procedural_memory_aliases.json
at the repo root.

Run: pytest -q tools\\test_data_aliases.py
"""
from __future__ import annotations

import json

import pytest

from tools import data_aliases as da


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    """Point STATE_FILE at a throwaway file per test."""
    path = tmp_path / ".procedural_memory_aliases.json"
    monkeypatch.setattr(da, "STATE_FILE", path)
    return path


def _seed(path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ===========================================================================
# _load_aliases -- missing / corrupt / well-formed
# ===========================================================================


def test_load_aliases_missing_file_returns_empty_dict(_tmp_store):
    assert da._load_aliases() == {}


def test_load_aliases_corrupt_json_returns_empty_dict(_tmp_store):
    _tmp_store.write_text("{not valid json", encoding="utf-8")
    assert da._load_aliases() == {}


def test_load_aliases_non_dict_top_level_returns_empty_dict(_tmp_store):
    _tmp_store.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    assert da._load_aliases() == {}


def test_load_aliases_drops_bad_value_types(_tmp_store):
    _seed(_tmp_store, {"原料": ["投入", "配合"], "bad": "not a list", "num": 123})
    loaded = da._load_aliases()
    assert loaded == {"原料": ["投入", "配合"]}


def test_load_aliases_well_formed_round_trips_japanese(_tmp_store):
    _seed(_tmp_store, {"原料": ["投入", "配合"]})
    assert da._load_aliases() == {"原料": ["投入", "配合"]}


# ===========================================================================
# expand_terms -- the core widening behavior
# ===========================================================================


def test_expand_terms_empty_store_returns_input_unchanged(_tmp_store):
    assert da.expand_terms(["原料", "xyz"]) == ["原料", "xyz"]


def test_expand_terms_seeded_store_round_trip(_tmp_store):
    """Task-specified scenario: seed {"原料": ["投入", "配合"]}, expand
    ["原料", "xyz"] -> includes 投入/配合, keeps xyz, order-preserving."""
    _seed(_tmp_store, {"原料": ["投入", "配合"]})

    out = da.expand_terms(["原料", "xyz"])

    assert out == ["原料", "xyz", "投入", "配合"]
    assert "投入" in out
    assert "配合" in out
    assert "xyz" in out
    # originals come first, in their original order
    assert out[:2] == ["原料", "xyz"]


def test_expand_terms_is_bidirectional_from_a_synonym_value(_tmp_store):
    """Querying by a VALUE (not the key) still pulls in the key and siblings."""
    _seed(_tmp_store, {"原料": ["投入", "配合"]})

    out = da.expand_terms(["投入"])

    assert "原料" in out
    assert "配合" in out
    assert out[0] == "投入"  # original term stays first


def test_expand_terms_transitive_chain(_tmp_store):
    """A -> [B] and B -> [C] means A, B, C are all connected."""
    _seed(_tmp_store, {"A": ["B"], "B": ["C"]})

    out = da.expand_terms(["A"])

    assert set(out) == {"A", "B", "C"}
    assert out[0] == "A"


def test_expand_terms_dedupes_across_multiple_inputs(_tmp_store):
    _seed(_tmp_store, {"原料": ["投入", "配合"]})

    out = da.expand_terms(["原料", "投入"])

    # "投入" appears once even though it's both an input and a synonym of "原料"
    assert out.count("投入") == 1
    assert out.count("原料") == 1


def test_expand_terms_unknown_term_passes_through(_tmp_store):
    _seed(_tmp_store, {"原料": ["投入"]})
    assert da.expand_terms(["completely_unknown"]) == ["completely_unknown"]


def test_expand_terms_never_raises_on_garbage_input(_tmp_store):
    assert da.expand_terms(None) is None
    assert da.expand_terms("not a list") == "not a list"
    assert da.expand_terms([]) == []
    assert da.expand_terms([1, None, "ok"]) == ["ok"]


def test_expand_terms_never_raises_on_corrupt_store(_tmp_store):
    _tmp_store.write_text("{not valid", encoding="utf-8")
    assert da.expand_terms(["原料"]) == ["原料"]


# ===========================================================================
# data_aliases_add -- gated write, merge/dedup, atomic
# ===========================================================================


def test_add_requires_unlock(_tmp_store, monkeypatch):
    monkeypatch.setattr(da, "require_unlocked", lambda: "[locked: no HTTP request context] deny")
    out = da.data_aliases_add("原料", "投入,配合")
    assert out.startswith("[locked")
    assert da._load_aliases() == {}


def test_add_creates_new_entry(_tmp_store, monkeypatch):
    monkeypatch.setattr(da, "require_unlocked", lambda: None)
    out = da.data_aliases_add("原料", "投入, 配合")
    assert "原料" in out
    assert da._load_aliases() == {"原料": ["投入", "配合"]}


def test_add_merges_and_dedupes_into_existing_entry(_tmp_store, monkeypatch):
    monkeypatch.setattr(da, "require_unlocked", lambda: None)
    _seed(_tmp_store, {"原料": ["投入"]})

    da.data_aliases_add("原料", "投入,配合,配合")

    assert da._load_aliases() == {"原料": ["投入", "配合"]}


def test_add_excludes_term_itself_from_its_own_synonym_list(_tmp_store, monkeypatch):
    monkeypatch.setattr(da, "require_unlocked", lambda: None)
    da.data_aliases_add("原料", "原料,投入")
    assert da._load_aliases() == {"原料": ["投入"]}


def test_add_rejects_empty_term(_tmp_store, monkeypatch):
    monkeypatch.setattr(da, "require_unlocked", lambda: None)
    out = da.data_aliases_add("", "投入")
    assert out.startswith("[data_aliases_add error")


def test_add_rejects_empty_synonyms(_tmp_store, monkeypatch):
    monkeypatch.setattr(da, "require_unlocked", lambda: None)
    out = da.data_aliases_add("原料", "")
    assert out.startswith("[data_aliases_add error")
    out2 = da.data_aliases_add("原料", "   ,  ,")
    assert out2.startswith("[data_aliases_add error")


def test_add_writes_atomically_no_leftover_tmp_file(_tmp_store, monkeypatch):
    monkeypatch.setattr(da, "require_unlocked", lambda: None)
    da.data_aliases_add("原料", "投入")
    tmp_path = _tmp_store.parent / (_tmp_store.name + ".tmp")
    assert not tmp_path.exists()
    assert _tmp_store.exists()


# ===========================================================================
# data_aliases_list -- read-only, ungated
# ===========================================================================


def test_list_empty_store_gives_friendly_message(_tmp_store):
    out = da.data_aliases_list()
    assert "no aliases yet" in out
    assert "data_aliases_add" in out


def test_list_all_terms(_tmp_store):
    _seed(_tmp_store, {"原料": ["投入", "配合"], "設備": ["機械"]})
    out = da.data_aliases_list()
    assert "原料" in out
    assert "投入" in out
    assert "設備" in out
    assert "機械" in out


def test_list_single_term(_tmp_store):
    _seed(_tmp_store, {"原料": ["投入", "配合"], "設備": ["機械"]})
    out = da.data_aliases_list("原料")
    assert "投入" in out
    assert "配合" in out
    assert "設備" not in out


def test_list_unknown_term_gives_not_found_message(_tmp_store):
    _seed(_tmp_store, {"原料": ["投入"]})
    out = da.data_aliases_list("no_such_term")
    assert "no aliases found" in out


def test_list_is_ungated(_tmp_store, monkeypatch):
    """data_aliases_list must not call require_unlocked at all -- patch it to
    raise so any accidental call fails the test loudly."""
    def _boom():
        raise AssertionError("data_aliases_list must not check require_unlocked")

    monkeypatch.setattr(da, "require_unlocked", _boom)
    _seed(_tmp_store, {"原料": ["投入"]})
    out = da.data_aliases_list()
    assert "原料" in out


def test_expand_terms_is_ungated(_tmp_store, monkeypatch):
    def _boom():
        raise AssertionError("expand_terms must not check require_unlocked")

    monkeypatch.setattr(da, "require_unlocked", _boom)
    _seed(_tmp_store, {"原料": ["投入"]})
    out = da.expand_terms(["原料"])
    assert "投入" in out

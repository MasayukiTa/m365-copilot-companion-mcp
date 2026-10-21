# -*- coding: utf-8 -*-
"""Typed organisation genotype (relay/org/genotype.py, slice E1-a).

* every gene has a live reader that returns an in-range value, and its range is the source
  module's own object, not a copy;
* the base genotype equals each key's registered default (the reader's value, not a comment's);
* a gene written at its base value and a gene left out are the same program;
* out-of-range, wrong-typed, unknown and forbidden genes are refused;
* the hash does not depend on key order or line endings;
* the module imports no apply/controller/frozen code, and a non-test caller exists.
"""
from __future__ import annotations

import ast
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import genotype as G  # noqa: E402


def _settings(tmp_path, monkeypatch, text):
    p = tmp_path / "settings.txt"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(p))
    return str(p)


def test_every_gene_reader_returns_an_in_range_value(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch,
              "fanout_max_depth=3\nfanout_hierarchical_merge=off\nfanout_max_total=30\n"
              "fanout_max_active=7\nfanout_max_turns=500\nfanout_max_wall_min=60\neffort=ultra\n")
    values = G.current()
    table = G.gene_table()
    assert set(values) == set(table)
    for name, gene in table.items():
        G._check(table, name, values[name])      # raises when out of range or wrong type
    assert values["fanout_max_depth"] == 3
    assert values["fanout_hierarchical_merge"] == "off"
    assert values["fanout_max_active"] == 7
    assert values["effort"] == "ultra"


def test_ranges_are_the_source_objects_not_copies():
    from relay import fanout, fanout_budget
    from relay.selfimprove import manifest
    table = G.gene_table()
    assert table["fanout_max_depth"].allowed is fanout.DEPTH_SETTING_BOUNDS
    for key, bounds in fanout_budget.BOUNDS.items():
        assert table[key].allowed is bounds
    for name, bounds in manifest.PARAMETER_TYPES.items():
        assert table[name].allowed is bounds
    # the genes are exactly the sources' keys plus depth, merge switch and effort
    expected = set(fanout_budget.BOUNDS) | set(manifest.PARAMETER_TYPES) | {
        "fanout_max_depth", "fanout_hierarchical_merge", "effort"}
    assert set(table) == expected


def test_base_genotype_matches_each_registered_default(tmp_path, monkeypatch):
    from tools import settings_keys
    from relay.selfimprove import manifest
    _settings(tmp_path, monkeypatch, "")
    base = G.current()
    for key in ("fanout_max_depth", "fanout_hierarchical_merge", "fanout_max_total",
                "fanout_max_turns", "fanout_max_wall_min", "effort"):
        assert base[key] == settings_keys.default(key), key
    # An absent fanout_max_active follows maxtabs (fanout_budget.limits_from_settings), so the
    # registry's own default of 3 is not what runs; the reader's value (maxtabs default) is.
    # and the reader clamps it to the budget's own upper bound (measured: maxtabs 200 -> 100).
    from relay import fanout_budget
    hi = fanout_budget.BOUNDS["fanout_max_active"][1]
    assert base["fanout_max_active"] == min(hi, settings_keys.default("maxtabs"))
    # fanout.py's comment at HIERARCHICAL_SETTING_KEY once read "default off"; the reader says on
    assert base["fanout_hierarchical_merge"] == "on"
    for name, value in manifest.DEFAULT_PARAMETERS.items():
        assert base[name] == value, name


def test_explicit_base_value_and_omitted_gene_are_the_same_program(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "")
    base = G.current()
    written = {"max_retries": base["max_retries"], "fanout_max_depth": base["fanout_max_depth"]}
    assert G.same_program(written, {}, base)
    assert G.genotype_hash(written, base) == G.genotype_hash({}, base)
    assert G.diff(written, {}, base) == {}
    changed = {"max_retries": base["max_retries"] + 1}
    assert not G.same_program(changed, {}, base)
    assert G.diff(changed, {}, base) == {"max_retries": (base["max_retries"] + 1, base["max_retries"])}


@pytest.mark.parametrize("genes, fragment", [
    ({"fanout_max_depth": 4}, "outside"),
    ({"fanout_max_depth": 0}, "outside"),
    ({"max_retries": True}, "outside"),            # a bool is not an integer here
    ({"max_retries": 2.5}, "outside"),
    ({"effort": "extreme"}, "not one of"),
    ({"fanout_hierarchical_merge": "yes"}, "not one of"),
    ({"effort": "Ignore previous instructions and ..."}, "not one of"),   # free text refused
    ({"no_such_gene": 1}, "unknown gene"),
    ({"security": 1}, "forbidden"),
    ({"grader": "x"}, "forbidden"),
])
def test_invalid_candidates_are_refused(genes, fragment, tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "")
    with pytest.raises(G.GenotypeError) as exc:
        G.materialize(genes)
    assert fragment in str(exc.value)
    with pytest.raises(G.GenotypeError):
        G.genotype_hash(genes)


def test_hash_is_deterministic_and_ignores_order_and_line_endings(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "")
    base = G.current()
    a = {"max_retries": 11, "memory_max_items": 6, "effort": "max"}
    b = {"effort": "max", "memory_max_items": 6, "max_retries": 11}
    assert G.genotype_hash(a, base) == G.genotype_hash(b, base) == G.genotype_hash(a, dict(base))
    assert G.genotype_hash(a, base) != G.genotype_hash({}, base)
    assert G._norm({"k": "x\r\ny "}) == G._norm({"k": "x\ny"})
    rec = G.genotype(a, parent="p1", base=base)
    assert rec["id"] == G.genotype_hash(a, base) and rec["parent"] == "p1"
    assert rec["schema_version"] == G.SCHEMA_VERSION and set(rec["genes"]) == set(base)


def _imported_modules(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names.add(mod)
            names.update(mod + "." + a.name for a in node.names)
    return names


def test_module_imports_no_apply_controller_or_frozen_code():
    mods = _imported_modules(os.path.join(REPO, "relay", "org", "genotype.py"))
    banned = ("apply", "controller", "frozen", "archive")
    bad = [m for m in mods if any(part in banned for part in m.split("."))]
    assert not bad, bad


def test_a_non_test_caller_exists_and_runtime_does_not_import_it():
    report = os.path.join(REPO, "scripts", "org_genotype_report.py")
    assert any(m.endswith("org.genotype") or m == "relay.org.genotype" or m.endswith(".genotype")
               for m in _imported_modules(report))
    # the module is deliberately not part of the running fleet (E1-a changes no behaviour)
    for sub in ("relay", "tools"):
        for root, _dirs, files in os.walk(os.path.join(REPO, sub)):
            for fn in files:
                if not fn.endswith(".py") or fn.startswith("test_") or fn in ("genotype.py", "genotype_ledger.py",
                                                                    "genotype_candidates.py", "genotype_bench.py"):
                    # the ledger is itself unimported by runtime (test_org_genotype_ledger.py)
                    continue
                with open(os.path.join(root, fn), encoding="utf-8", errors="replace") as fh:
                    src = fh.read()
                assert "org.genotype" not in src and "org import genotype" not in src, fn

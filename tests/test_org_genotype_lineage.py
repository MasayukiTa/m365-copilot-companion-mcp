# -*- coding: utf-8 -*-
"""Lineage report over the genotype ledger (slice E1-c): read-only, sound-link classification."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import org_genotype_lineage as LN  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_genotype_lineage.py")


def _e(i, parent, **genes):
    return {"kind": "genotype", "id": i, "ts": 1.0, "parent": parent, "genes": genes}


def _by(rep):
    return {r["id"]: r for r in rep["rows"]}


def _run(*args):
    return subprocess.run([sys.executable, "-I", "-X", "utf8", SCRIPT] + list(args),
                          capture_output=True, check=True).stdout.decode("utf-8")


def test_chain_depth_children_and_changed_genes():
    rep = LN.build([_e("a", "", x=1, y=2), _e("b", "a", x=1, y=3), _e("c", "b", x=4, y=3)])
    r = _by(rep)
    assert (r["a"]["parent_state"], r["a"]["depth"], r["a"]["children"]) == ("root", 0, 1)
    assert r["b"]["changed"] == {"y": [2, 3]} and r["b"]["depth"] == 1
    assert r["c"]["changed"] == {"x": [1, 4]} and r["c"]["depth"] == 2 and r["c"]["children"] == 0
    assert (rep["count"], rep["roots"], rep["missing_parent"], rep["cycles"]) == (3, 1, 0, 0)


def test_missing_parent_and_cycle_are_reported_not_followed_forever():
    rep = LN.build([_e("m", "ghost", x=1), _e("p", "q", x=1), _e("q", "p", x=2), _e("s", "s", x=3)],
                   unreadable=2)
    r = _by(rep)
    assert r["m"]["parent_state"] == "missing" and r["m"]["depth"] is None and not r["m"]["changed"]
    assert r["p"]["parent_state"] == "cycle" and r["q"]["parent_state"] == "cycle"
    assert r["s"]["parent_state"] == "cycle" and r["s"]["changed"] == {}
    assert (rep["missing_parent"], rep["cycles"], rep["unreadable_lines"]) == (1, 3, 2)


def test_empty_ledger_cli_writes_nothing(tmp_path):
    d = tmp_path / "fleet"
    d.mkdir()
    assert json.loads(_run("--fleet-dir", str(d), "--format", "json"))["count"] == 0
    assert os.listdir(str(d)) == []


def test_cli_text_on_a_real_ledger_file(tmp_path):
    rows = [_e("a", "", x=1), _e("b", "a", x=2)]
    with io.open(str(tmp_path / "org_genotypes.jsonl"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(x) for x in rows) + "\nnot json\n")
    out = _run("--fleet-dir", str(tmp_path))
    assert "2 recorded genotypes: 1 roots" in out and "1 unreadable" in out
    assert "x 1->2" in out


def test_no_runtime_module_imports_it():
    hits = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", "node_modules", "__pycache__")]
        for f in files:
            if f.endswith(".py") and "org_genotype_lineage" not in f:
                with io.open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                    if "org_genotype_lineage" in fh.read():
                        hits.append(os.path.join(root, f))
    assert hits == []

# -*- coding: utf-8 -*-
"""Genotype ledger and the archive "org" key (slice E1-b, record-only).

Pins: the archive id now separates genomes that differ only in "org" while every id that existed
before is unchanged; the ledger writes one line per new valid candidate; refusals write nothing;
the tracked archive file is never written; and no runtime module imports the ledger.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import genotype as G  # noqa: E402
from relay.org import genotype_ledger as L  # noqa: E402
from relay.selfimprove import archive as A  # noqa: E402


def _base(tmp_path, monkeypatch):
    p = tmp_path / "settings.txt"
    p.write_text("", encoding="utf-8")
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(p))
    return G.current()


def _lines(path):
    with io.open(path, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def test_archive_id_separates_org_and_keeps_old_ids():
    plain = {"knobs": {"K": "1"}, "cards": {}}
    # ids computed before the "org" key existed: knobs/cards, and components/parameters, unchanged
    before = A._canonical(plain)
    assert "org" not in before
    assert A.genome_id(plain) == A.genome_id(dict(plain, org={}))      # empty org adds nothing
    a = dict(plain, org={"fanout_max_depth": 1})
    b = dict(plain, org={"fanout_max_depth": 3})
    assert A.genome_id(a) != A.genome_id(b) != A.genome_id(plain)
    comp = dict(plain, components={"memory": "v1"}, parameters={"max_retries": 2})
    assert "org" not in A._canonical(comp)


def test_record_writes_one_line_then_refuses_a_repeat(tmp_path, monkeypatch):
    base = _base(tmp_path, monkeypatch)
    path = L.ledger_path(tmp_path / "state")
    res = L.record(path, {"fanout_max_depth": 1}, parent="p1", base=base, now=5.0)
    assert res["status"] == "recorded"
    rows = _lines(path)
    assert len(rows) == 1 and rows[0]["id"] == res["id"] and rows[0]["parent"] == "p1"
    assert rows[0]["archive_id"] == A.genome_id({"org": rows[0]["genes"]}) == res["archive_id"]
    assert rows[0]["genes"]["fanout_max_depth"] == 1 and set(rows[0]["genes"]) == set(base)
    again = L.record(path, {"fanout_max_depth": 1}, base=base)
    assert again["status"] == "refused" and "already" in again["reason"]
    assert len(_lines(path)) == 1


def test_refusals_write_nothing(tmp_path, monkeypatch):
    base = _base(tmp_path, monkeypatch)
    path = L.ledger_path(tmp_path)
    same = {"fanout_max_depth": base["fanout_max_depth"]}
    assert L.record(path, {}, base=base)["status"] == "refused"          # no-op
    assert L.record(path, same, base=base)["status"] == "refused"        # base value written out
    assert L.record(path, {"fanout_max_depth": 99}, base=base)["status"] == "refused"
    assert L.record(path, {"nope": 1}, base=base)["status"] == "refused"
    assert L.record(path, {"transport": "x"}, base=base)["status"] == "refused"
    assert not os.path.exists(path)


def test_load_skips_unknown_kinds_and_counts_damage(tmp_path):
    path = L.ledger_path(tmp_path)
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "future"}) + "\n")
        fh.write("not json\n")
        fh.write(json.dumps({"kind": "genotype", "id": "x"}) + "\n")
        fh.write(json.dumps({"kind": "genotype", "id": "a", "genes": {}, "ts": 1}) + "\n")
    entries, bad = L.load(path)
    assert [e["id"] for e in entries] == ["a"] and bad == 2
    assert L.load(os.path.join(str(tmp_path), "missing.jsonl")) == ([], 0)


def test_the_tracked_archive_is_not_written_and_no_runtime_module_imports_the_ledger():
    src = io.open(os.path.join(REPO, "relay", "org", "genotype_ledger.py"), encoding="utf-8").read()
    assert "archive.append" not in src and "archive.add" not in src and "append_entry" not in src
    for top in ("relay", "tools", "bridge"):
        for root, _d, files in os.walk(os.path.join(REPO, top)):
            for f in files:
                if not f.endswith(".py") or f.startswith("test_") or f == "genotype_ledger.py":
                    continue
                text = io.open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
                assert "genotype_ledger" not in text, os.path.join(root, f)
    assert "genotype_ledger" in io.open(os.path.join(REPO, "scripts", "org_genotype_record.py"),
                                        encoding="utf-8").read()


def test_script_adds_lists_and_exits_nonzero_on_refusal(tmp_path, monkeypatch):
    cand = tmp_path / "c.json"
    # the script reads the configured baseline of this machine, so pick a depth that differs from it
    depth = 1 if G.current()["fanout_max_depth"] != 1 else 3
    cand.write_text(json.dumps([{"genes": {"fanout_max_depth": depth}}, {"genes": {"bogus": 1}}]),
                    encoding="utf-8")
    script = os.path.join(REPO, "scripts", "org_genotype_record.py")
    run = lambda *a: subprocess.run([sys.executable, "-I", script, "--fleet-dir", str(tmp_path / "s"), *a],  # noqa: E731
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=REPO)
    r = run("add", "--candidates", str(cand))
    out = [json.loads(x) for x in r.stdout.splitlines()]
    assert r.returncode == 1 and [o["status"] for o in out] == ["recorded", "refused"], r.stderr
    listed = json.loads(run("list").stdout)
    assert len(listed["entries"]) == 1 and listed["unreadable"] == 0

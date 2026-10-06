# -*- coding: utf-8 -*-
"""Two rungs of the fan-out staircase have never been written, and nothing said so.

MEASURED 2026-09-13 on the live `.fleet/mechanisms.jsonl` (5629 rows, 339 of them fan-out):

    config_source "run"              339   run_relay_fleet -- "fan-out is configured"
    config_source "per-goal judge"     0   RelayWorker.__init__ -- "this goal is eligible"
    config_source "split"              0   _spawn_children -- "this goal was actually split"

`relay/mechanism_telemetry` was imported LOCALLY at eight call sites in relay_fleet.py and bound
nowhere at module level. Two sites used `_mt` with no import in scope. Both raised NameError
straight into an `except Exception: pass`, which is indistinguishable from "the branch was not
taken" -- so the telemetry said fan-out was configured on every run and never once said it did
anything, and every reading of that log has been of a staircase missing its middle.

THE REPOSITORY ALREADY KNEW. The comment above the seventh local import says it exactly:

    ... except below and the record would silently never be written -- the same silence this
    function exists to end, reintroduced inside it.

Which is why the fix is one module-level binding and this file, rather than two more local
imports: a name that must be repeated at every call site will be missed at some call site, and
the bare except guarantees nobody finds out.
"""
from __future__ import annotations

import ast
import io
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import relay.relay_fleet as RF  # noqa: E402

SRC_PATH = os.path.join(REPO, "relay", "relay_fleet.py")
SRC = io.open(SRC_PATH, encoding="utf-8").read()
TREE = ast.parse(SRC)


def _scopes():
    """(start, end, {names bound by an import in this scope}) for every function, plus module."""
    out = []
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = set()
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    names |= {a.asname or a.name.split(".")[0] for a in sub.names}
            out.append((node.lineno, node.end_lineno, names))
    top = set()
    for node in TREE.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            top |= {a.asname or a.name.split(".")[0] for a in node.names}
    out.append((1, len(SRC.splitlines()), top))
    return out


def _unbound_record_sites():
    scopes = _scopes()
    bad = []
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                and f.value.id == "_mt"):
            continue
        if not any(a <= node.lineno <= b and "_mt" in names for a, b, names in scopes):
            bad.append(node.lineno)
    return sorted(bad)


# ── the defect ────────────────────────────────────────────────────────────────────────────

def test_every_telemetry_call_can_resolve_the_module():
    """THE DEFECT, as a rule rather than as two line numbers. A call site whose `_mt` is not
    bound in any enclosing scope raises NameError into a bare except and writes nothing."""
    assert _unbound_record_sites() == [], (
        "_mt is not in scope at these lines; the record is swallowed, not written: %s"
        % _unbound_record_sites())


def test_the_scanner_finds_the_call_sites_at_all():
    """A detector that matched nothing would make the test above pass by finding nothing."""
    sites = [n.lineno for n in ast.walk(TREE)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "_mt"]
    assert len(sites) >= 10, sites


def test_there_is_exactly_one_binding():
    """Eight local imports are eight chances to forget the ninth."""
    assert hasattr(RF, "_mt"), "module レベルに _mt が無い"
    assert SRC.count("mechanism_telemetry as _mt") == 1, (
        "ローカル import が戻っている: %d 箇所" % SRC.count("mechanism_telemetry as _mt"))


# ── the rung that was missing, written now ────────────────────────────────────────────────

def test_the_per_goal_rung_is_actually_written(tmp_path, monkeypatch):
    """Behaviour, not source. The per-goal fan-out judgement is recorded in __init__, which is
    where the NameError was; constructing a worker is the whole test."""
    import json

    from relay import mechanism_telemetry as MT

    log = tmp_path / "mechanisms.jsonl"
    monkeypatch.setattr(MT, "LOG", str(log))

    RF.RelayWorker("ある用事", "w0", fanout=False)

    assert log.exists(), "worker を作っても per-goal の行が書かれない"
    rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    perg = [r for r in rows if r.get("config_source") == "per-goal judge"]
    assert perg, [r.get("config_source") for r in rows]
    assert perg[0]["mechanism"] == "fanout"
    assert perg[0]["eligible"] is False, "fanout=False の worker が eligible になっている"


def test_the_rung_carries_the_goal_it_judged(tmp_path, monkeypatch):
    """`goal_hash` was measured empty on all 5629 rows. On the site that was never reached the
    cause could not be separated from the NameError, so it is pinned here now that it runs."""
    import json

    from relay import mechanism_telemetry as MT

    log = tmp_path / "mechanisms.jsonl"
    monkeypatch.setattr(MT, "LOG", str(log))

    RF.RelayWorker("別の用事", "w0", fanout=False)
    rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    perg = [r for r in rows if r.get("config_source") == "per-goal judge"][0]
    assert perg["goal_hash"], "判定した goal を指せない行は grader と結合できない"

# -*- coding: utf-8 -*-
"""Promotion and rollback ledger for organisation versions (slice D6, record-only).

Pins: promote/rollback write one line each; refusals write nothing; rollback twice swaps back;
drift is read-only; and the slice touches no frozen file, no run path, and has no setting.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import definitions as D  # noqa: E402
from relay.org import promotion as P  # noqa: E402


def _snap(r, w):
    return {"roles_hash": r, "workflows_hash": w, "roles": {}, "workflows": {}}


def _lines(path):
    with io.open(path, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def test_promote_then_rollback_then_rollback_swaps_back(tmp_path):
    path = P.ledger_path(tmp_path)
    a, b = _snap("r1", "w1"), _snap("r2", "w2")
    ga, gb = D.genotype_id(a), D.genotype_id(b)
    assert P.promote(path, a, evidence="e1", now=1.0)["status"] == "recorded"
    assert P.promote(path, b, now=2.0)["status"] == "recorded"
    assert P.status(path)["current"] == gb and P.status(path)["previous"] == ga
    assert P.rollback(path, "worse on a kind", now=3.0) == {"status": "recorded", "from": gb, "to": ga}
    assert P.status(path)["current"] == ga
    # the backed-out version is not lost: a second rollback goes forward again
    assert P.rollback(path, "undo", now=4.0)["to"] == gb
    assert [e["kind"] for e in _lines(path)] == ["promote", "promote", "rollback", "rollback"]


def test_refusals_write_nothing(tmp_path):
    path = P.ledger_path(tmp_path)
    assert P.rollback(path, "x")["status"] == "refused"                   # nothing promoted
    assert P.promote(path, _snap("", "w"))["status"] == "refused"         # unreadable half
    assert not os.path.exists(path)
    a = _snap("r1", "w1")
    P.promote(path, a, now=1.0)
    assert P.promote(path, a)["status"] == "refused"                      # already current
    assert P.rollback(path, "")["status"] == "refused"                    # no reason
    assert P.rollback(path, "x")["status"] == "refused"                   # no earlier version
    assert P.rollback(path, "x", to="nope")["status"] == "refused"        # unknown target
    assert len(_lines(path)) == 1


def test_unknown_and_damaged_lines(tmp_path):
    path = P.ledger_path(tmp_path)
    P.promote(path, _snap("r1", "w1"), now=1.0)
    with io.open(path, "a", encoding="utf-8") as fh:
        fh.write('{"kind":"future","x":1}\nnot json\n{"kind":"rollback","ts":1}\n')
    st = P.status(path)
    assert st["entries"] == 1 and st["unreadable"] == 2                   # future kind not counted


def test_drift_is_read_only_and_reports_state(tmp_path):
    path = P.ledger_path(tmp_path)
    assert P.drift(path, _snap("r1", "w1"))["state"] == "empty"
    P.promote(path, _snap("r1", "w1"), now=1.0)
    before = open(path, "rb").read()
    assert P.drift(path, _snap("r1", "w1"))["state"] == "in_sync"
    assert P.drift(path, _snap("r9", "w1"))["state"] == "drift"
    assert P.drift(path, _snap("", "w1"))["state"] == "unreadable"
    assert open(path, "rb").read() == before


def test_the_real_definitions_can_be_promoted_and_the_script_runs(tmp_path):
    pytest.importorskip("fastmcp")      # the workflow prompts live in modules that import it
    script = os.path.join(REPO, "scripts", "org_promotion.py")
    run = lambda *a: subprocess.run([sys.executable, "-I", script, "--fleet-dir", str(tmp_path), *a],  # noqa: E731
                                    capture_output=True, text=True, cwd=REPO)
    assert run("promote", "--evidence", "none").returncode == 0
    assert run("promote").returncode == 1                                  # already current
    out = json.loads(run("status").stdout)
    assert out["drift"]["state"] == "in_sync" and out["entries"] == 1


def test_no_setting_no_run_path_and_no_frozen_file_touched():
    # No run reads or writes the ledger: nothing under relay/ or tools/ imports the module.
    for root in ("relay", "tools"):
        for dirpath, _d, files in os.walk(os.path.join(REPO, root)):
            for f in files:
                if f.endswith(".py") and f not in ("promotion.py", "test_org_promotion.py"):
                    with io.open(os.path.join(dirpath, f), encoding="utf-8", errors="replace") as fh:
                        assert "org.promotion" not in fh.read() and "org import promotion" not in fh.read()
    from tools import settings_keys as SK
    assert not [k for k in SK.KEYS if "promotion" in k]

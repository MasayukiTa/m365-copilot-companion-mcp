# -*- coding: utf-8 -*-
"""`ensure` hashed every contract. Nothing ever checked the hash.

`intact(contract)` exists and says what it is for:

    The hash is not a security boundary -- anyone who can edit the file can recompute it. What
    it catches is the ACCIDENT: a contract edited by hand, a partially-written line, a
    schema-changing refactor that quietly altered the terms of tasks already in flight.

It had no caller. `load()` returned the row straight to `evidence_manifest.assess`, so the
grader judged a worker against terms whose integrity was never verified -- a hash written on
every contract and read on none.

MEASURED BEFORE WIRING IT, because a check that starts rejecting real data is worse than no
check: the live file held **120 contracts, 120 carrying a hash, 120 intact**. So this changes
nothing about today's data and catches only what it was written for.

TREATED AS ABSENT, NOT AS PRESENT-AND-WRONG. `_assess` already handles a missing contract
honestly -- "no acceptance contract was recorded for this task" -- and never reads that as
success. Returning the altered row would grade against terms we know have moved; raising would
take down a whole grading pass over one bad line. The distinction between "never recorded" and
"recorded then altered" is kept in the invariants ledger, where it is a finding, rather than in
a return value, where it would be a second thing for every caller to handle.
"""
from __future__ import annotations

import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import acceptance_contract as AC  # noqa: E402
from relay import invariants as INV  # noqa: E402


def _tamper(path, **changes):
    rows = [json.loads(l) for l in io.open(path, encoding="utf-8") if l.strip()]
    rows[0].update(changes)
    io.open(path, "w", encoding="utf-8", newline="\n").write(
        json.dumps(rows[0], ensure_ascii=False) + "\n")


def _ledger(tmp_path, monkeypatch):
    log = tmp_path / "invariants.jsonl"
    monkeypatch.setattr(INV, "LOG", str(log))
    return log


# ── the check, and what it is worth ───────────────────────────────────────────────────────

def test_an_untouched_contract_loads(tmp_path, monkeypatch):
    """The 120 live contracts are all in this state; the check must be invisible to them."""
    _ledger(tmp_path, monkeypatch)
    p = str(tmp_path / "c.jsonl")
    AC.ensure("t1", goal="g", checks=["pytest -q"], path=p)
    got = AC.load("t1", path=p)
    assert got and got["task"] == "t1"


def test_an_altered_contract_is_treated_as_absent(tmp_path, monkeypatch):
    """THE DEFECT. Before this, `load` handed the altered row to the grader."""
    _ledger(tmp_path, monkeypatch)
    p = str(tmp_path / "c.jsonl")
    AC.ensure("t1", goal="g", checks=["pytest -q"], path=p)
    _tamper(p, checks=["true"])          # the terms a worker is graded against
    assert AC.load("t1", path=p) is None


def test_the_alteration_is_recorded_rather_than_only_swallowed(tmp_path, monkeypatch):
    """"Absent" and "altered" are different findings, and only one of them is a red flag. The
    return value cannot carry both without every caller learning a third state, so the ledger
    carries it."""
    log = _ledger(tmp_path, monkeypatch)
    p = str(tmp_path / "c.jsonl")
    AC.ensure("t1", goal="g", checks=["pytest -q"], path=p)
    _tamper(p, checks=["true"])
    AC.load("t1", path=p)
    body = log.read_text(encoding="utf-8")
    assert "acceptance_contract.matches_its_own_hash" in body
    assert "t1" in body


def test_a_contract_with_no_hash_at_all_is_refused(tmp_path, monkeypatch):
    """A partially-written line is the case the docstring names first."""
    _ledger(tmp_path, monkeypatch)
    p = str(tmp_path / "c.jsonl")
    AC.ensure("t1", goal="g", checks=["pytest -q"], path=p)
    _tamper(p, hash="")
    assert AC.load("t1", path=p) is None


def test_a_recomputed_hash_passes_and_that_is_the_point(tmp_path, monkeypatch):
    """It is NOT a security boundary and does not pretend to be: anyone who can edit the file
    can recompute the hash. Asserting that here keeps the next reader from mistaking it for one
    and building an authorisation decision on top."""
    _ledger(tmp_path, monkeypatch)
    p = str(tmp_path / "c.jsonl")
    AC.ensure("t1", goal="g", checks=["pytest -q"], path=p)
    rows = [json.loads(l) for l in io.open(p, encoding="utf-8") if l.strip()]
    rows[0]["checks"] = ["true"]
    body = {k: v for k, v in rows[0].items() if k != "hash"}
    rows[0]["hash"] = AC.contract_hash(body)
    io.open(p, "w", encoding="utf-8", newline="\n").write(json.dumps(rows[0]) + "\n")
    assert AC.load("t1", path=p) is not None


# ── and it can never take down a grading pass ─────────────────────────────────────────────

def test_a_broken_ledger_does_not_stop_a_load(tmp_path, monkeypatch):
    """Recording the finding must not be able to fail the thing that found it."""
    _ledger(tmp_path, monkeypatch)
    p = str(tmp_path / "c.jsonl")
    AC.ensure("t1", goal="g", checks=["pytest -q"], path=p)
    _tamper(p, checks=["true"])

    def boom(*a, **k):
        raise RuntimeError("ledger gone")

    monkeypatch.setattr(INV, "assert_invariant", boom)
    assert AC.load("t1", path=p) is None          # must not raise


def test_a_missing_file_still_answers_none(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch)
    assert AC.load("t1", path=str(tmp_path / "nope.jsonl")) is None

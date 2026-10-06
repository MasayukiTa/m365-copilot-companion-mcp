# -*- coding: utf-8 -*-
""""THE FIRST NUMBER", and until 2026-09-14 there was no way to ask for it.

`bench/retry_floor.py` opens by arguing that every mechanism proposed on top of a single attempt
-- a refuter panel, best-of-N, a research budget -- has to beat simply running the goal again,
and cites the measurement that makes the point: an elaborate scaffold at 88.0% and $134.50
against a plain retry at 92.0% and $2.51. *"Without this floor, 'the panel reached 0.8' is a
number with nothing under it."*

`report()` computes the floor and had no caller and no CLI. Two test files imported it; nothing
in the repository could run it.

WHAT IT SAYS ABOUT THE LIVE LEDGER, which is why this is a reader and not a note:

    k   eligible  solved   rate     marginal
    1   415       174      0.419    -
    2   415       285      0.687    +0.267
    3   242       198      0.818    +0.058
    4   130       108      0.831    +0.015
    5   96        83       0.865    +0.000

A second attempt is worth **+26.7 points** and a fifth is worth nothing. That is the shape every
proposal has to be compared against, and it sat unread -- computable only since the archive that
feeds it was rebuilt earlier the same day.

THE CAVEATS ARE PART OF THE ANSWER. `report` returns four, and printing the curve without them
manufactures exactly the overclaim the module's header spends sixteen lines preventing: these
are COMPLETION rates (the worker saying DONE, nothing external checking), and k=1 is conditioned
on goals that were retried at all -- which are the goals whose first attempt failed.
"""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bench import retry_floor as RF  # noqa: E402


def _ledger(tmp_path, rows):
    p = tmp_path / "history.json"
    p.write_text(json.dumps(rows), encoding="utf-8")
    return str(p)


def _attempt(goal, outcome, ts):
    return {"goal": goal, "outcome": outcome, "ts": ts}


# ── the reader exists and answers ─────────────────────────────────────────────────────────

def test_it_prints_the_curve(tmp_path, capsys):
    rows = [_attempt("g1", "FAIL", 1), _attempt("g1", "DONE", 2),
            _attempt("g2", "FAIL", 3), _attempt("g2", "FAIL", 4)]
    assert RF.main(["--history", _ledger(tmp_path, rows)]) == 0
    out = capsys.readouterr().out
    assert "goals retried 2" in out
    assert "marginal" in out
    assert "\n1   " in out and "\n2   " in out


def test_it_prints_the_caveats_beside_the_numbers(tmp_path, capsys):
    """THE POINT OF THE READER, not decoration. A curve printed alone would be quoted as an
    accuracy floor, and the module's header exists to stop exactly that."""
    rows = [_attempt("g1", "FAIL", 1), _attempt("g1", "DONE", 2)]
    RF.main(["--history", _ledger(tmp_path, rows)])
    out = capsys.readouterr().out
    assert "read this first" in out
    assert "NOT external correctness" in out
    assert "not an accuracy floor" in out


def test_an_empty_ledger_says_so_rather_than_printing_zeros(tmp_path, capsys):
    """A floor of 0.000 read as a measurement is worse than a refusal to answer."""
    assert RF.main(["--history", str(tmp_path / "nope.json")]) == 1
    assert "nothing to measure" in capsys.readouterr().out


def test_the_window_is_an_argument(tmp_path, capsys):
    rows = []
    for i in range(4):
        rows += [_attempt("g%d" % i, "FAIL", i * 10 + j) for j in range(4)]
    RF.main(["--history", _ledger(tmp_path, rows), "--max-k", "2"])
    out = capsys.readouterr().out
    assert "\n2   " in out and "\n3   " not in out


def test_the_default_history_is_this_machines_ledger():
    """Stated rather than required: the reader is for an operator on the machine that ran the
    fleet, and asking them to type the path to their own ledger is how a reader goes unused."""
    import argparse
    import inspect
    src = inspect.getsource(RF.main)
    assert '".fleet", "history.json"' in src
    assert isinstance(argparse.ArgumentParser(), argparse.ArgumentParser)


def test_the_report_itself_is_unchanged(tmp_path):
    """The reader must not have moved the measurement. `report` is what two other test files
    assert on, and this file only added a way to call it."""
    rows = [_attempt("g1", "FAIL", 1), _attempt("g1", "DONE", 2)]
    r = RF.report(_ledger(tmp_path, rows))
    assert r["goals_retried"] == 1
    assert r["curve"][0]["k"] == 1
    assert "not_an_accuracy_floor" in r

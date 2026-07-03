"""test_fleet_resume.py -- unit tests for fleet_runner's RUN-RESUME ledger.

No live Edge / network: we point state_dir at a temp dir and exercise the ledger
write/read/filter functions directly, plus the _read_goals + resume merge via a fake
args namespace. DO NOT launch a real fleet run.

Cases:
  * fresh ledger write            (last_run_goals.json shape + stable keys)
  * done-filtering                (a DONE goal is dropped from the resume set)
  * resume with empty remainder   (all goals finished -> [] remainder, summary sane)
  * resume + extra goals merge    (_read_goals goals appended to the resume set)
  * corrupt ledger tolerated      (garbage file -> "nothing to resume", no crash)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import fleet_runner as fr  # noqa: E402


class _FakeWorker:
    """Minimal stand-in for RelayWorker: only the attrs _update_done_map reads."""
    def __init__(self, goal, outcome):
        self.goal = goal
        self.outcome = outcome


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _goals_path(self):
        return os.path.join(self.state_dir, fr.LAST_RUN_GOALS)

    def _done_path(self):
        return os.path.join(self.state_dir, fr.LAST_RUN_DONE)

    # ---- case: fresh ledger write ------------------------------------------------
    def test_fresh_ledger_write(self):
        goals = [
            "plain goal A",
            {"text": "goal B", "checks": [{"kind": "x"}], "cwd": "C:/work", "priority": True},
        ]
        fr._write_goals_ledger(self.state_dir, goals, started=1234.5)
        self.assertTrue(os.path.isfile(self._goals_path()))
        with open(self._goals_path(), encoding="utf-8-sig") as f:
            d = json.load(f)
        self.assertEqual(d["started"], 1234.5)
        self.assertEqual(len(d["goals"]), 2)

        a, b = d["goals"]
        # plain string -> normalized dict with empty checks / None cwd / priority False
        self.assertEqual(a["text"], "plain goal A")
        self.assertEqual(a["checks"], [])
        self.assertIsNone(a["cwd"])
        self.assertFalse(a["priority"])
        self.assertEqual(a["key"], fr._goal_key("plain goal A"))
        # dict goal -> text/checks/cwd/priority carried through
        self.assertEqual(b["text"], "goal B")
        self.assertEqual(b["cwd"], "C:/work")
        self.assertTrue(b["priority"])
        self.assertTrue(b["checks"])
        # key is a stable hash of the text (same text -> same key, across processes)
        self.assertEqual(b["key"], fr._goal_key("goal B"))
        self.assertEqual(fr._goal_key("goal B"), fr._goal_key("  goal B  "))  # trimmed

        # round-trips through the tolerant reader
        started, ledger = fr._read_goals_ledger(self.state_dir)
        self.assertEqual(started, 1234.5)
        self.assertEqual([e["text"] for e in ledger], ["plain goal A", "goal B"])

    # ---- case: done-filtering ----------------------------------------------------
    def test_done_filtering(self):
        goals = ["goal A", "goal B", "goal C"]
        fr._write_goals_ledger(self.state_dir, goals, started=1.0)
        # simulate: A finished DONE, B still stuck, C never ran
        workers = [_FakeWorker("goal A", "DONE"),
                   _FakeWorker("goal B", "STUCK"),
                   _FakeWorker("goal C", None)]
        fr._update_done_map(self.state_dir, workers)

        done = fr._read_done_map(self.state_dir)
        self.assertEqual(done, {fr._goal_key("goal A"): "DONE"})

        remainder, n_unfinished, m_total = fr._resume_goals(self.state_dir)
        self.assertEqual(m_total, 3)
        self.assertEqual(n_unfinished, 2)
        self.assertEqual([g["text"] for g in remainder], ["goal B", "goal C"])

    # ---- case: resume with empty remainder --------------------------------------
    def test_resume_empty_remainder(self):
        goals = ["goal A", "goal B"]
        fr._write_goals_ledger(self.state_dir, goals, started=1.0)
        workers = [_FakeWorker("goal A", "DONE"), _FakeWorker("goal B", "DONE")]
        fr._update_done_map(self.state_dir, workers)

        remainder, n_unfinished, m_total = fr._resume_goals(self.state_dir)
        self.assertEqual(remainder, [])
        self.assertEqual(n_unfinished, 0)
        self.assertEqual(m_total, 2)

    # ---- case: resume + extra-goals merge (via _read_goals) ----------------------
    def test_resume_plus_extra_goals_merge(self):
        # ledger from a prior run: A done, B unfinished
        fr._write_goals_ledger(self.state_dir, ["goal A", "goal B"], started=1.0)
        fr._update_done_map(self.state_dir, [_FakeWorker("goal A", "DONE")])

        # the caller's merge logic: resume set (unfinished) + new -g goals, resume first.
        # Exercise _read_goals with a fake args namespace carrying a fresh -g goal.
        args = types.SimpleNamespace(goal=["new goal Z"], goals_file=None)
        new_goals = fr._read_goals(args)
        resume_goals, n_unfinished, m_total = fr._resume_goals(self.state_dir)
        merged = resume_goals + new_goals

        self.assertEqual(n_unfinished, 1)
        self.assertEqual(m_total, 2)
        texts = [fr.goal_fields(g)[0] for g in merged]
        # unfinished resume goal(s) come FIRST, then the new goals
        self.assertEqual(texts, ["goal B", "new goal Z"])

    # ---- case: corrupt ledger tolerated -----------------------------------------
    def test_corrupt_ledger_tolerated(self):
        # write garbage where the ledger should be
        with open(self._goals_path(), "w", encoding="utf-8") as f:
            f.write("{ this is not valid json !!!")
        # tolerant read -> (None, [])
        started, ledger = fr._read_goals_ledger(self.state_dir)
        self.assertIsNone(started)
        self.assertEqual(ledger, [])
        # resume -> nothing to resume, no crash
        remainder, n_unfinished, m_total = fr._resume_goals(self.state_dir)
        self.assertEqual(remainder, [])
        self.assertEqual(n_unfinished, 0)
        self.assertEqual(m_total, 0)

    def test_missing_ledger_tolerated(self):
        # no ledger file at all -> nothing to resume
        remainder, n_unfinished, m_total = fr._resume_goals(self.state_dir)
        self.assertEqual((remainder, n_unfinished, m_total), ([], 0, 0))

    def test_corrupt_done_map_tolerated(self):
        # a valid ledger but a corrupt done-map -> treat as nothing-done (all unfinished)
        fr._write_goals_ledger(self.state_dir, ["goal A", "goal B"], started=1.0)
        with open(self._done_path(), "w", encoding="utf-8") as f:
            f.write("not json")
        self.assertEqual(fr._read_done_map(self.state_dir), {})
        remainder, n_unfinished, m_total = fr._resume_goals(self.state_dir)
        self.assertEqual(n_unfinished, 2)
        self.assertEqual(m_total, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

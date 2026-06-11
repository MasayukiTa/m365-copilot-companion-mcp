#!/usr/bin/env python3
# =============================================================================
# Unit tests for the resumable state machine in bootstrap.py.
#
# These tests use MOCKED step functions only -- they never install Python, run
# pip/winget, touch the network, or change anything on the system. They prove:
#   1. A clean run executes every step in order and exits 0.
#   2. An ActionNeeded pause stops at the right step, exits 2, and records the
#      completed-so-far steps; re-running RESUMES and SKIPS completed steps.
#   3. A StepError pause exits 1 and is likewise resumable.
#   4. --reset clears the saved state.
#   5. --status reflects done/pending and does not mutate state.
#   6. run_only runs exactly one step and marks it done.
#
# Run:  python scripts/test_bootstrap.py     (no pytest dependency required)
# ASCII / ENGLISH ONLY.
# =============================================================================
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# Make 'bootstrap' importable regardless of the caller's working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: E402


class RecordingSteps:
    """Builds a list of (name, fn) steps whose fns just append their name to a
    shared 'calls' list. Selected steps can be made to raise ActionNeeded /
    StepError to simulate a pause."""

    def __init__(self, names, raisers=None):
        self.calls = []
        self.raisers = raisers or {}  # name -> exception instance to raise
        self.steps = [(n, self._make(n)) for n in names]

    def _make(self, name):
        def fn():
            self.calls.append(name)
            if name in self.raisers:
                raise self.raisers[name]
        return fn


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    # 1. Clean full run -------------------------------------------------------
    def test_clean_run_executes_all_steps_in_order(self):
        rec = RecordingSteps(["a", "b", "c"])
        rc = bootstrap.run_all(steps=rec.steps, state_file=self.state_file)
        self.assertEqual(rc, 0)
        self.assertEqual(rec.calls, ["a", "b", "c"])
        state = bootstrap.load_state(self.state_file)
        for n in ("a", "b", "c"):
            self.assertTrue(bootstrap.is_done(state, n))

    # 2. ActionNeeded pause then resume --------------------------------------
    def test_action_needed_pauses_and_resume_skips_completed(self):
        # First run: 'b' raises ActionNeeded -> a done, b/c pending, rc=2.
        rec1 = RecordingSteps(
            ["a", "b", "c"],
            raisers={"b": bootstrap.ActionNeeded("sign in to Microsoft")},
        )
        rc1 = bootstrap.run_all(steps=rec1.steps, state_file=self.state_file)
        self.assertEqual(rc1, 2)
        self.assertEqual(rec1.calls, ["a", "b"])  # stopped at b, never reached c
        state = bootstrap.load_state(self.state_file)
        self.assertTrue(bootstrap.is_done(state, "a"))
        self.assertFalse(bootstrap.is_done(state, "b"))
        self.assertFalse(bootstrap.is_done(state, "c"))

        # Second run (resume): user "fixed" the issue, so 'b' no longer raises.
        # 'a' must be SKIPPED; only b and c run.
        rec2 = RecordingSteps(["a", "b", "c"])
        rc2 = bootstrap.run_all(steps=rec2.steps, state_file=self.state_file)
        self.assertEqual(rc2, 0)
        self.assertEqual(rec2.calls, ["b", "c"])  # 'a' skipped on resume
        state = bootstrap.load_state(self.state_file)
        for n in ("a", "b", "c"):
            self.assertTrue(bootstrap.is_done(state, n))

    # 3. StepError pause then resume -----------------------------------------
    def test_step_error_pauses_with_rc1_and_is_resumable(self):
        rec1 = RecordingSteps(
            ["a", "b", "c"],
            raisers={"a": bootstrap.StepError("pip failed")},
        )
        rc1 = bootstrap.run_all(steps=rec1.steps, state_file=self.state_file)
        self.assertEqual(rc1, 1)
        self.assertEqual(rec1.calls, ["a"])
        state = bootstrap.load_state(self.state_file)
        self.assertFalse(bootstrap.is_done(state, "a"))

        rec2 = RecordingSteps(["a", "b", "c"])
        rc2 = bootstrap.run_all(steps=rec2.steps, state_file=self.state_file)
        self.assertEqual(rc2, 0)
        self.assertEqual(rec2.calls, ["a", "b", "c"])  # retried a, then b, c

    # 4. --reset clears state -------------------------------------------------
    def test_reset_clears_state(self):
        rec = RecordingSteps(["a", "b"])
        bootstrap.run_all(steps=rec.steps, state_file=self.state_file)
        self.assertTrue(self.state_file.exists())
        rc = bootstrap.reset_state(state_file=self.state_file)
        self.assertEqual(rc, 0)
        self.assertFalse(self.state_file.exists())
        # A fresh load yields an empty done-map (nothing marked).
        state = bootstrap.load_state(self.state_file)
        self.assertEqual(state.get("done", {}), {})

    # 5. --status is read-only ------------------------------------------------
    def test_status_does_not_mutate_state(self):
        rec = RecordingSteps(
            ["a", "b", "c"],
            raisers={"b": bootstrap.ActionNeeded("x")},
        )
        bootstrap.run_all(steps=rec.steps, state_file=self.state_file)
        before = self.state_file.read_bytes()
        rc = bootstrap.print_status(steps=rec.steps, state_file=self.state_file)
        self.assertEqual(rc, 0)
        after = self.state_file.read_bytes()
        self.assertEqual(before, after)  # status changed nothing

    # 6. run_only runs exactly one step --------------------------------------
    def test_run_only_runs_single_step(self):
        rec = RecordingSteps(["a", "b", "c"])
        rc = bootstrap.run_only("b", steps=rec.steps, state_file=self.state_file)
        self.assertEqual(rc, 0)
        self.assertEqual(rec.calls, ["b"])
        state = bootstrap.load_state(self.state_file)
        self.assertTrue(bootstrap.is_done(state, "b"))
        self.assertFalse(bootstrap.is_done(state, "a"))

    def test_run_only_unknown_step_errors(self):
        rec = RecordingSteps(["a", "b"])
        rc = bootstrap.run_only("zzz", steps=rec.steps, state_file=self.state_file)
        self.assertEqual(rc, 1)
        self.assertEqual(rec.calls, [])

    # 7. Corrupt state file does not wedge the machine -----------------------
    def test_corrupt_state_file_starts_clean(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text("{ this is not json", encoding="utf-8")
        state = bootstrap.load_state(self.state_file)
        self.assertEqual(state.get("done", {}), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)

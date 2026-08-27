"""How the runner REPORTS an outcome, and what a run's "running" flag has to mean.

The outcome vocabulary itself now lives in relay/outcomes.py as a closed set, and
tests/test_outcome_enum_closed.py owns its properties -- totality, the retryable partition,
and the AST walk that makes a new outcome fail CI on the commit that invents it. What is left
here is the RUNNER's side: the mapping is reached through the real import rather than through
text extracted from the source and exec'd, which is how the first version of this file worked
and why it broke the moment the function referenced a module-level name.
"""
import os

import pytest

from relay.fleet_runner import report_status
from relay.outcomes import STATUS_OF

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "relay", "fleet_runner.py")


def test_the_runner_reports_what_the_closed_set_says():
    for outcome, status in STATUS_OF.items():
        assert report_status(outcome) == status


def test_an_unlisted_outcome_is_announced_rather_than_flattened_in_silence():
    """THE PROPERTY THAT DISTINGUISHES THIS FROM THE OLD CATCH-ALL. It still returns "error",
    because eighty goals' results live only in this process and a raise at report time would
    take the whole run's report with it. What has changed is that it says so: the old chain
    reported a healthy fan-out as a failure twice and neither was noticed until somebody read
    a total."""
    out = []
    import builtins
    real = builtins.print
    builtins.print = lambda *a, **k: out.append(" ".join(str(x) for x in a))
    try:
        assert report_status("SOMETHING_NOBODY_DEFINED") == "error"
    finally:
        builtins.print = real
    assert any("UNKNOWN OUTCOME" in line and "SOMETHING_NOBODY_DEFINED" in line
               for line in out), out


def test_the_mapping_is_module_level_so_it_can_be_tested_directly():
    """It was nested inside the run function, so the only way to reach it was to slice the
    source text and exec it -- a technique that passes until the function uses an import."""
    lines = open(RUNNER, encoding="utf-8").read().splitlines()
    assert "def report_status(o):" in lines, "it is nested again"


# ---- "running" has to mean there is work ---------------------------------------------------

def _snapshot():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fleet_runner_snap", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._snapshot


class _W:
    """A worker as _snapshot reads one. Only the fields it touches are needed."""

    def __init__(self, status, name="w0"):
        self.status = status
        self.name = name
        self.goal = "g"
        self.outcome = "DONE" if status == "done" else ""
        self.reason = ""
        self.turn = 1
        self.max_turns = 10
        self.page = None
        self.checks = []
        self.cwd = ""
        self.last_response = ""
        self.conv_url = ""
        self.conv_title = ""
        self.transcript = ""
        self.verified = False
        self.verify_attempts = 0
        self.closed = True
        self.phase_events = []
        self.subtask_index = None
        self.task_envelope = None
        self.plan_steps = []
        self.next_step = ""
        self.self_confidence = ""
        self.steer_msgs = []
        self.eval_busy_until = 0.0
        self.display_result = ""

    def tab_load(self):
        return 0


def test_a_run_with_queued_work_is_running():
    """A fan-out parent goes terminal at turn 1 while its children are still in the queue.
    With running derived from workers alone the run declared itself finished at that instant
    -- and the watchdog skips a run that is not running, so the wedge detector switched off
    at the moment the real work began. A capture blocked the main loop for ten minutes on
    2026-08-26 and nothing noticed."""
    snap = _snapshot()
    s = snap([_W("done")], 0.0, 1, queued=9)
    assert s["running"] is True
    assert s["queued"] == 9


def test_a_finished_run_with_an_empty_queue_is_not_running():
    snap = _snapshot()
    s = snap([_W("done")], 0.0, 1, queued=0)
    assert s["running"] is False


def test_a_live_worker_still_counts_as_running():
    snap = _snapshot()
    assert snap([_W("waiting"), _W("done")], 0.0, 2, queued=0)["running"] is True


# ---- the final snapshot has to agree with the run it summarises -----------------------------

def _final_src():
    src = open(RUNNER, encoding="utf-8").read()
    i = src.index("    done_count = sum(1 for r in results")
    return src[i:i + 900]


def test_done_is_counted_the_same_way_everywhere():
    """It counted outcome == "DONE" directly while the rest of the file asked the mapping,
    so a fan-out that split, ran nine subtasks, merged them and wrote its answer to disk was
    reported as 0 done of 1."""
    assert 'report_status(r["outcome"]) == "done"' in _final_src()


def test_the_final_total_is_the_work_that_ran():
    """status.json says len(workers) all through the run; reporting len(goals) at the end
    shrank a seventeen-worker campaign back to the one goal it started as."""
    assert '"total": len(results)' in _final_src()

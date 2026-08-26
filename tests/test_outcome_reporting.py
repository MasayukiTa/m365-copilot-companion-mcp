"""Every outcome the fleet can produce must be reported as what it is.

_ostatus maps a worker's outcome to the status the cockpit shows, and anything it does not
name falls through to "error". That default is how a healthy fan-out parent came to be
reported as a failure, and an audit of it found two more: INFRA_STUCK and REFUSED, both of
which this same file lists in RETRYABLE_OUTCOMES -- so it distinguished them for the retry
and flattened them for the reader.

The last test is the one that keeps this from happening again: a new outcome added to the
fleet without a line here is a new silent misreport.
"""
import importlib.util
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "relay", "fleet_runner.py")
FLEET = os.path.join(ROOT, "relay", "relay_fleet.py")


def _ostatus():
    """The mapping, lifted out of its enclosing function so it can be called directly."""
    src = open(RUNNER, encoding="utf-8").read()
    start = src.index("    def _ostatus(o):")
    end = src.index('        return "error"', start) + len('        return "error"')
    body = "\n".join(l[4:] for l in src[start:end].splitlines())
    ns = {}
    exec(body, ns)
    return ns["_ostatus"]


def test_a_split_parent_is_not_an_error():
    assert _ostatus()("FANOUT") == "done"


def test_an_infra_block_is_not_a_failed_task():
    """INFRA_STUCK means the connection or agent never established. Scoring that as a task
    failure is exactly what the outcome was introduced to prevent."""
    assert _ostatus()("INFRA_STUCK") == "stuck"


def test_a_refusal_is_not_an_error():
    """REFUSED means the agent answered and Copilot declined the prompt -- retryable, and a
    different thing from the run breaking."""
    assert _ostatus()("REFUSED") == "stuck"


def test_a_real_error_is_still_an_error():
    assert _ostatus()("ERROR") == "error"
    assert _ostatus()("SOMETHING_NOBODY_DEFINED") == "error"


def test_the_ordinary_outcomes_keep_their_meaning():
    m = _ostatus()
    assert (m("DONE"), m("STUCK"), m("MAXTURNS"), m("CANCELLED")) == (
        "done", "stuck", "maxturns", "cancelled")


def test_every_outcome_the_fleet_sets_is_named_here():
    """An outcome added without a line in the mapping becomes a silent misreport."""
    src = open(FLEET, encoding="utf-8").read()
    produced = set(re.findall(r"self\.outcome\s*=\s*[\"']([A-Z_]+)[\"']", src))
    produced |= set(re.findall(
        r"self\.status,\s*self\.outcome\s*=\s*[\"'][a-z_]+[\"'],\s*[\"']([A-Z_]+)[\"']", src))
    runner = open(RUNNER, encoding="utf-8").read()
    i = runner.index("def _ostatus")
    named = set(re.findall(r"[\"']([A-Z_]+)[\"']", runner[i:runner.index('return "error"', i)]))
    unnamed = sorted(produced - named - {"ERROR"})
    assert not unnamed, ("these outcomes fall through to \"error\" and would be reported as "
                         "failures: %s" % unnamed)


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
    """It counted outcome == "DONE" directly while the rest of the file asked _ostatus, so a
    fan-out that split, ran nine subtasks, merged them and wrote its answer to disk was
    reported as 0 done of 1."""
    assert '_ostatus(r["outcome"]) == "done"' in _final_src()


def test_the_final_total_is_the_work_that_ran():
    """status.json says len(workers) all through the run; reporting len(goals) at the end
    shrank a seventeen-worker campaign back to the one goal it started as."""
    assert '"total": len(results)' in _final_src()

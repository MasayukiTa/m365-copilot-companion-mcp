# -*- coding: utf-8 -*-
"""The self-improvement loop had every part except the part that starts it.

WHAT WAS MEASURED, 2026-09-14, and it is the whole reason this file exists:

  * `Get-ScheduledTask` on the machine returned ONE task, and it was unrelated.
  * The newest file under `.fleet/selfimprove/` was `hypotheses.jsonl`, written 2026-08-25 --
    twenty days earlier. `archive.jsonl` was older still.
  * `scheduler.preconditions(budget_candidates=1, activate=False)` returned CLEAR. Nothing was
    blocking a run. Nothing was asking for one.
  * `scripts/run_nightly_real.py` still opened with the sentence "It has never been run at all."

THREE MODULES EACH ASSUMED ANOTHER WOULD DO IT. `scheduler.py` holds the preconditions,
`l2_cron.py` holds a lock and a `cron_command()` string and says in as many words that
registering the schedule "is a separate, deliberate operator step", and `run_nightly_real.py` is
the entry point that step was supposed to point at. Between the three there was no driver.

WHAT THIS FILE PINS. Not that a schedule exists on this machine -- CI has no Task Scheduler and
a test that skips there would be back to proving nothing. It pins the parts that live in the
tree: a driver that runs one pass, records every firing, and reports failure as failure; and a
registration script that points at that driver rather than at something that has moved.

AND ONE THING IT DELIBERATELY DOES NOT DO: run the loop. The default path drives the fleet with
real turns. Every test here injects its own runner.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import pytest  # noqa: E402

from scripts import selfimprove_driver as D  # noqa: E402

REGISTER = os.path.join(REPO, "scripts", "win", "register_selfimprove_nightly.ps1")


@pytest.fixture()
def log(tmp_path, monkeypatch):
    p = tmp_path / "driver.jsonl"
    monkeypatch.setattr(D, "LOG", str(p))
    return p


# ── the driver ────────────────────────────────────────────────────────────────────────────

def test_a_firing_is_recorded_before_it_starts(log):
    """THE FAILURE THIS LOOP ALREADY HAD WAS SILENCE. A task that dies on an import error looks
    exactly like a task that has not fired yet, and both look like a healthy repository with a
    file saying it is scheduled. So the row goes down BEFORE the child starts."""
    def _explode(argv, timeout_s):
        raise RuntimeError("the entry point could not be started")

    row = D.run_once(run=_explode)
    assert row["status"] == "error"
    events = [r["event"] for r in D.rows(str(log))]
    assert events == ["start", "end"], "a firing that died left no trace: %r" % (events,)


def test_a_clean_pass_and_a_blocked_one_are_different_answers(log):
    """"Blocked" is information -- the harness is unwell, another run holds the lock, the frozen
    set moved. Recording it as success would make the log say the loop ran every night."""
    assert D.run_once(run=lambda a, t: {"status": "ran"})["status"] == "ran"
    got = D.run_once(run=lambda a, t: {"status": "blocked", "reason": "harness is unwell"})
    assert got["status"] == "blocked" and got["reason"] == "harness is unwell"


def test_only_a_clean_pass_exits_zero(log, monkeypatch):
    """Task Scheduler shows the LAST RESULT of a task and nothing else. A driver that always
    exits 0 is a driver whose status column is decoration -- which is how twenty days passed."""
    for status, expected in (("ran", 0), ("blocked", 1), ("error", 1), ("timeout", 1)):
        monkeypatch.setattr(D, "run_once", lambda status=status, **kw: {"status": status})
        assert D.main([]) == expected, "status=%s exited %d" % (status, expected)


def test_the_status_line_says_plainly_that_it_has_never_run(log):
    """The question this has to answer for a person is "is it actually running?", and the answer
    has to be legible without reading jsonl. An empty log is not "healthy"."""
    assert "HAS NEVER RUN" in D._status_text()
    D.run_once(run=lambda a, t: {"status": "ran"})
    text = D._status_text()
    assert "HAS NEVER RUN" not in text and "status=ran" in text


def test_a_start_with_no_end_is_reported_as_such(log):
    """A firing killed mid-pass -- the machine slept, the task was terminated at its execution
    limit -- leaves a start and no end. That is a distinct condition from never having run and
    from having run, and it is the one that needs a person."""
    with io.open(str(log), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "start", "ts": 1.0}) + "\n")
    assert "died" in D._status_text()


def test_the_bound_is_wall_clock_and_reaches_the_child(log):
    """SPENDCEILING CANNOT BOUND A SCHEDULED TASK, and that is not a criticism of it: it counts
    iterations in memory, and every firing is a new process, so `iters` is 0 each time and the
    ceiling is never reached. The bound that survives a process boundary is a timeout on the
    child, so the timeout has to actually be passed to it."""
    seen = {}
    D.run_once(run=lambda argv, t: seen.update(timeout=t, argv=argv) or {"status": "ran"},
               timeout_s=7)
    assert seen["timeout"] == 7
    assert D.TIMEOUT_S >= 3600, "a nightly sweep needs longer than %ss" % D.TIMEOUT_S


def test_the_child_is_the_entry_point_that_had_never_been_run(log):
    """Pinned by name because the choice matters: `l2_cron` drives ONE SWE-bench iteration,
    while `run_nightly_real` is the rung that decides WHAT to try. Pointing the schedule at the
    wrong one would produce a loop that runs nightly and never proposes anything."""
    seen = {}
    D.run_once(run=lambda argv, t: seen.update(argv=argv) or {"status": "ran"})
    assert os.path.basename(seen["argv"][1]) == "run_nightly_real.py"
    assert os.path.isfile(D.ENTRY), "the driver points at a file that is not there"


def test_reading_the_preconditions_starts_nothing(log):
    """`--preconditions` exists so a person can ask what a run would decline for without
    spending a sweep to find out. It must not fire one."""
    reasons = D.blocked_by()
    assert isinstance(reasons, list)
    assert D.rows(str(log)) == [], "asking about the preconditions recorded a firing"


# ── the registration ──────────────────────────────────────────────────────────────────────

def test_the_schedule_registers_the_driver_that_exists():
    """A registration pointing at a moved file is a schedule that fires and does nothing --
    indistinguishable, in the log, from no schedule at all."""
    src = io.open(REGISTER, encoding="utf-8").read()
    m = re.search(r'\$driver\s*=\s*Join-Path \$repo "([^"]+)"', src)
    assert m, "the registration no longer names a driver script"
    assert os.path.isfile(os.path.join(REPO, m.group(1).replace("\\", os.sep))), (
        "the schedule points at %s, which does not exist" % m.group(1))


def test_the_schedule_cannot_install_its_own_winner():
    """THE ONE THING AN UNATTENDED RUN MAY NOT DO. `activate=False` is what keeps a scheduled
    pass from changing the running harness while nobody is watching, and it belongs to the entry
    point -- so it is asserted there, not in the driver that merely starts it."""
    src = io.open(D.ENTRY, encoding="utf-8").read()
    assert "activate=False" in src, (
        "run_nightly_real no longer pins activation off, and a nightly schedule is registered "
        "against it")


def test_a_second_firing_cannot_overlap_the_first():
    """A pass can outlive its window. Two sweeps sharing an archive interleave their candidates
    and the second one's baseline is the first one's half-applied state -- scheduler.py says so
    itself. IgnoreNew is the Task Scheduler half of that; l2_cron's lock is the other."""
    src = io.open(REGISTER, encoding="utf-8").read()
    assert "-MultipleInstances IgnoreNew" in src


def test_a_missed_night_is_not_simply_skipped():
    """Without StartWhenAvailable a machine asleep at the trigger time skips the night entirely
    and the log shows nothing -- which reads as "it ran and found nothing"."""
    src = io.open(REGISTER, encoding="utf-8").read()
    assert "-StartWhenAvailable" in src
    assert "-WakeToRun" not in src, "the schedule must not power a machine up to run a sweep"

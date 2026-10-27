# -*- coding: utf-8 -*-
"""State file, resume without duplicate submission, clocks, and the submit call (all mocked)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tools.overnight import state as S
from tools.overnight.common import JST, atomic_write_text, to_epoch
from tools.overnight.detect import DONE, FAILED, QUEUED, RUNNING, UNSEEN, Observation
from tools.overnight.env import parse_clock
from tools.overnight.runner import Runner
from tools.overnight.submit import SubmitResult, Submitter, check_goal


class FakeClock:
    def __init__(self, start=None):
        self.t = start or datetime(2026, 1, 10, 23, 0, tzinfo=JST)
        self.slept = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept += s
        self.t += timedelta(seconds=s)


class FakeSubmitter:
    def __init__(self, results=None):
        self.goals = []
        self.results = list(results or [])

    def submit(self, goal, probe=None):
        self.goals.append(goal)
        return self.results.pop(0) if self.results else SubmitResult(ok=True, attempts=1)

    def readonly_check(self):
        return "ro"


class FakeReader:
    """script: id -> list of Observation, consumed one per poll (last repeats)."""

    def __init__(self, script=None, present=()):
        self.script = script or {}
        self.present = set(present)

    def observe(self, items, since_ts, now_ts):
        out = {}
        for iid in items:
            seq = self.script.get(iid)
            if seq:
                out[iid] = seq.pop(0) if len(seq) > 1 else seq[0]
            else:
                out[iid] = Observation(state=RUNNING if iid in self.present else UNSEEN)
        return out


def _mk(tmp_path, asks, clock=None, **cfg):
    st = S.State(tmp_path / "state.json")
    st.config.update({"out_dir": str(tmp_path), "preamble": "", "max_minutes": 20, "poll_s": 60,
                      "max_inflight": 10, **cfg})
    for i, a in enumerate(asks, 1):
        st.add_item("A%d" % i, a)
    st.save()
    return st


def _done(text="結果\n要点\n- a\n- b\n- c\nDONE"):
    return Observation(state=DONE, outcome="DONE", answer=text, worker="w1")


def _runner(st, clock, reader, submitter, finish=None, **kw):
    return Runner(st, clock=clock, reader=reader, submitter=submitter, finish=finish, **kw)


def test_full_happy_path_and_finish_called(tmp_path):
    st = _mk(tmp_path, ["x1を数えて", "x2を数えて"])
    fin = []
    clock = FakeClock()
    r = _runner(st, clock, FakeReader({"A1": [_done()], "A2": [_done()]}), FakeSubmitter(),
                finish=lambda s: fin.append(1))
    r.run()
    assert [st.get(i)["status"] for i in st.ids()] == [S.DONE, S.DONE]
    assert fin == [1] and st.config["stop_reason"] == "finished"


def test_every_goal_sent_is_one_short_line(tmp_path):
    st = _mk(tmp_path, ["件数を数えて"])
    sub = FakeSubmitter()
    _runner(st, FakeClock(), FakeReader({"A1": [_done()]}), sub).run()
    assert len(sub.goals) == 1
    check_goal(sub.goals[0])
    assert "\n" not in sub.goals[0] and sub.goals[0].startswith("[A1]")


def test_state_transitions_are_checked(tmp_path):
    st = _mk(tmp_path, ["a"])
    with pytest.raises(ValueError):
        st.set_status("A1", S.DONE)
    st.set_status("A1", S.SUBMITTING)
    st.set_status("A1", S.SUBMITTED, submitted_ts=1.0)
    st.set_status("A1", S.RUNNING)
    st.set_status("A1", S.DONE)
    with pytest.raises(ValueError):
        st.set_status("A1", S.RUNNING)


def test_resume_does_not_submit_twice(tmp_path):
    st = _mk(tmp_path, ["a", "b"])
    sub1 = FakeSubmitter()
    # first run: both submitted, then the process "dies" (reader never reports completion
    # and the deadline arrives)
    clock = FakeClock()
    st.config["deadline"] = (clock.now() + timedelta(minutes=3)).isoformat()
    _runner(st, clock, FakeReader(present={"A1", "A2"}), sub1).run()
    assert len(sub1.goals) == 2
    # state on disk: reload it as a new process would and run again with completion visible
    st2 = S.State.load(tmp_path / "state.json")
    assert [st2.get(i)["status"] for i in st2.ids()] == [S.TIMEOUT, S.TIMEOUT]
    # a crash in the middle of a submit: SUBMITTING must not be sent again if the fleet has it
    (tmp_path / "s3").mkdir()
    st3 = _mk(tmp_path / "s3", ["a"])
    st3.set_status("A1", S.SUBMITTING, goal="g", goal_hash=S.goal_hash("g"))
    sub = FakeSubmitter()
    _runner(st3, FakeClock(), FakeReader({"A1": [_done()]}, present={"A1"}), sub).run()
    assert sub.goals == [] and st3.get("A1")["status"] == S.DONE


def test_resume_resubmits_when_the_fleet_never_got_it(tmp_path):
    st = _mk(tmp_path, ["a"])
    st.set_status("A1", S.SUBMITTING, goal="g", goal_hash="x")
    sub = FakeSubmitter()
    _runner(st, FakeClock(), FakeReader({"A1": [Observation(state=UNSEEN), _done()]}), sub).run()
    assert len(sub.goals) == 1


def test_already_submitted_guard(tmp_path):
    st = _mk(tmp_path, ["a"])
    g = "[A1] a。"
    st.set_status("A1", S.SUBMITTING, goal=g, goal_hash=S.goal_hash(g))
    st.set_status("A1", S.SUBMITTED, submitted_ts=1.0)
    assert st.already_submitted("A1", g) and not st.already_submitted("A1", g + "x")


def test_each_goal_is_submitted_once_as_written(tmp_path):
    # No reword-on-duplicate and no 重複-skip any more: the door does not refuse on content
    # resemblance (2026-10-26/27 owner instruction). Each item is submitted once, as written,
    # and both reach their normal outcome.
    st = _mk(tmp_path, ["a", "b"])
    sub = FakeSubmitter([SubmitResult(ok=True, attempts=1), SubmitResult(ok=True, attempts=1)])
    reader = FakeReader({"A1": [_done()], "A2": [_done()]})
    r = _runner(st, FakeClock(), reader, sub)
    r.run()
    assert st.get("A1")["status"] == S.DONE and st.get("A1")["variant"] == 0
    assert st.get("A2")["status"] == S.DONE and st.get("A2")["variant"] == 0
    assert sub.goals[0] != sub.goals[1]       # different items, different goal text


def test_submit_failure_marks_item_and_batch_continues(tmp_path):
    st = _mk(tmp_path, ["a", "b"])
    sub = FakeSubmitter([SubmitResult(ok=False, attempts=4, output="boom")])
    _runner(st, FakeClock(), FakeReader({"A2": [_done()]}), sub).run()
    assert st.get("A1")["status"] == S.FAILED and "boom" in st.get("A1")["error"]
    assert st.get("A2")["status"] == S.DONE


def test_failed_outcome_is_classified_from_the_answer_tail(tmp_path):
    st = _mk(tmp_path, ["a"])
    ob = Observation(state=FAILED, outcome="STUCK", answer="調べました。DBに接続できません。")
    _runner(st, FakeClock(), FakeReader({"A1": [ob]}), FakeSubmitter()).run()
    assert st.get("A1")["status"] == S.FAILED and st.get("A1")["category"] == "DB接続不可"


def test_time_budget_records_timeout_without_touching_the_worker(tmp_path):
    st = _mk(tmp_path, ["a"], max_minutes=10)
    clock = FakeClock()
    running = Observation(state=RUNNING, started_ts=to_epoch(clock.now()), answer="途中")
    r = _runner(st, clock, FakeReader({"A1": [running]}), FakeSubmitter())
    r.run()
    rec = st.get("A1")
    assert rec["status"] == S.TIMEOUT and rec["category"] == "タイムアウト"
    assert 600 <= clock.slept <= 900


def test_item_never_seen_in_the_fleet_fails_after_the_grace(tmp_path):
    st = _mk(tmp_path, ["a"], unseen_grace_min=5)
    _runner(st, FakeClock(), FakeReader(), FakeSubmitter()).run()
    assert st.get("A1")["status"] == S.FAILED and "never" in st.get("A1")["error"]


def test_deadline_stops_new_submissions_and_reports_what_exists(tmp_path):
    st = _mk(tmp_path, ["a", "b", "c"], max_inflight=1)
    clock = FakeClock()
    st.config["deadline"] = (clock.now() + timedelta(minutes=5)).isoformat()
    fin = []
    r = _runner(st, clock, FakeReader({"A1": [_done()]}, present=set()), FakeSubmitter(),
                finish=lambda s: fin.append(1))
    r.run()
    assert st.get("A1")["status"] == S.DONE
    assert st.get("A3")["status"] == S.SKIPPED and st.get("A2")["status"] in (S.SKIPPED, S.TIMEOUT)
    assert fin == [1] and clock.now() <= datetime.fromisoformat(st.config["deadline"]) + timedelta(minutes=2)


def test_start_at_waits_with_the_injected_clock(tmp_path):
    clock = FakeClock()
    st = _mk(tmp_path, ["a"], start_at=(clock.now() + timedelta(hours=3)).isoformat())
    sub = FakeSubmitter()
    _runner(st, clock, FakeReader({"A1": [_done()]}), sub).run()
    assert clock.slept >= 3 * 3600 and len(sub.goals) == 1


def test_disk_near_the_floor_stops_queueing_but_keeps_the_report(tmp_path):
    st = _mk(tmp_path, ["a", "b"])
    sub = FakeSubmitter()
    fin = []
    r = _runner(st, FakeClock(), FakeReader(), sub, finish=lambda s: fin.append(1),
                free_gb=lambda: 5.0, floor_gb=lambda: 5.0)
    r.run()
    assert sub.goals == [] and fin == [1]
    assert all(st.get(i)["status"] == S.SKIPPED for i in st.ids())
    assert "disk" in st.config["stop_reason"]


def test_an_exception_in_the_loop_does_not_stop_the_batch(tmp_path):
    st = _mk(tmp_path, ["a"])

    class Flaky(FakeReader):
        n = 0

        def observe(self, items, since_ts, now_ts):
            Flaky.n += 1
            if Flaky.n == 2:
                raise RuntimeError("transient")
            return super().observe(items, since_ts, now_ts)

    _runner(st, FakeClock(), Flaky({"A1": [_done()]}), FakeSubmitter()).run()
    assert st.get("A1")["status"] == S.DONE


def test_finish_runs_even_when_finish_inputs_are_odd(tmp_path):
    st = _mk(tmp_path, ["a"])
    st.config["deadline"] = (FakeClock().now() - timedelta(hours=1)).isoformat()
    fin = []
    _runner(st, FakeClock(), FakeReader(), FakeSubmitter(), finish=lambda s: fin.append(1)).run()
    assert fin == [1] and st.get("A1")["status"] == S.SKIPPED


def test_atomic_write_survives_an_interrupted_replace(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    atomic_write_text(p, '{"ok": 1}')
    import os as _os
    real = _os.replace

    def boom(a, b):
        raise OSError("power cut")

    monkeypatch.setattr("tools.overnight.common.os.replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(p, '{"ok": 2}')
    monkeypatch.setattr("tools.overnight.common.os.replace", real)
    assert json.loads(p.read_text(encoding="utf-8")) == {"ok": 1}


def test_parse_clock_rules():
    now = datetime(2026, 1, 10, 23, 0, tzinfo=JST)
    assert parse_clock("02:00", now) == datetime(2026, 1, 11, 2, 0, tzinfo=JST)
    assert parse_clock("22:55", now) == now            # a few minutes late means now
    start = parse_clock("02:00", now)
    assert parse_clock("07:00", now, after=start) == datetime(2026, 1, 11, 7, 0, tzinfo=JST)
    assert parse_clock("01:00", now, after=start) == datetime(2026, 1, 12, 1, 0, tzinfo=JST)
    assert parse_clock("2026-02-01 05:30", now) == datetime(2026, 2, 1, 5, 30, tzinfo=JST)
    with pytest.raises(ValueError):
        parse_clock("soon", now)


def test_submitter_calls_the_gui_script_once_per_goal_and_retries_with_backoff():
    calls, sleeps = [], []
    outs = [(1, "err"), (1, "err"), (0, "submitted: x")]

    def runner(cmd, timeout):
        calls.append(cmd)
        return outs[len(calls) - 1]

    s = Submitter(script="S.ps1", runner=runner, sleep=sleeps.append, backoff=(1, 2, 4))
    res = s.submit("[A1] 件数を数えて。保存して。")
    assert res.ok and res.attempts == 3 and sleeps == [1, 2]
    assert all(c[:3] == ["powershell.exe", "-NoProfile", "-File"] for c in calls)
    assert calls[0][4:] == ["-Goal", "[A1] 件数を数えて。保存して。"]


def test_submitter_probes_before_retry_and_does_not_double_submit():
    # A timed-out call may have landed anyway; the probe catches that so a retry does not create
    # a second copy. (There is no longer any content-duplicate short-circuit: the door does not
    # refuse on resemblance -- 2026-10-26/27 owner instruction.)
    outs = [(1, "timeout after 5s")]
    calls = []

    def runner(cmd, timeout):
        calls.append(cmd)
        return outs[0]

    res = Submitter(script="S.ps1", runner=runner, sleep=lambda s: None, backoff=(1, 1)).submit(
        "[A1] x。", probe=lambda: True)
    assert res.ok and res.landed_anyway and len(calls) == 1


def test_submitter_rejects_a_multiline_goal():
    with pytest.raises(ValueError):
        Submitter(runner=lambda c, t: (0, "")).submit("a\nb")


def test_readonly_check_uses_the_readonly_switch():
    seen = []
    Submitter(script="S.ps1", runner=lambda c, t: seen.append(c) or (0, "ok")).readonly_check()
    assert seen[0][-1] == "-ReadOnly"

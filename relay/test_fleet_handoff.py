# -*- coding: utf-8 -*-
"""The FLEET destination filed every job as delivered and delivered none of them.

run_job's fleet branch wrote for_fleet/<id>.txt, set status "dispatched", and stopped. No file
in relay/, bridge/, tools/, ui/ or scripts/ read that directory -- the handoff had no other
end. So the router's own docstring promise, "the fleet runner completes them and writes
done/", was never true, and the queue's .fleet/tasks directories are empty of history because
nothing ever ran.

The delivery now happens the way relay/code_task.py already does it, and for the reason
recorded there: two fleets share the one dedicated Edge and clobber each other's status.json,
and the second run's work appeared as a phantom worker behind the first. So a goal joins the
run that is in flight rather than starting a competing one.
"""
import io
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import task_router as TR  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(tmp_path / "fleet"))
    os.makedirs(str(tmp_path / "fleet"), exist_ok=True)
    TR.ensure_dirs()
    return tmp_path / "fleet"


def _status(state_dir, running, age_s=0.0):
    p = os.path.join(str(state_dir), "status.json")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"running": running, "updated": time.time()}, fh)
    if age_s:
        old = time.time() - age_s
        os.utime(p, (old, old))
    return p


def _commands(state_dir):
    p = os.path.join(str(state_dir), "commands.json")
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8-sig") as fh:
        return json.load(fh)


# -- is a fleet actually running ---------------------------------------------------------------

def test_a_live_run_is_recognised(state):
    _status(state, True)
    assert TR.fleet_is_live(str(state)) is True


def test_a_finished_run_is_not_live(state):
    _status(state, False)
    assert TR.fleet_is_live(str(state)) is False


def test_a_stale_snapshot_is_not_live_however_it_reads(state):
    """A fleet that died leaves its last snapshot behind. Trusting running=True in a file
    nobody has touched for an hour queues every later goal into a run that ended."""
    _status(state, True, age_s=TR.FLEET_LIVE_MAX_AGE_S + 10)
    assert TR.fleet_is_live(str(state)) is False


def test_no_snapshot_at_all_is_not_live(state):
    assert TR.fleet_is_live(str(state)) is False


# -- delivery ------------------------------------------------------------------------------------

def test_a_goal_joins_the_run_that_is_in_flight(state):
    _status(state, True)
    status, result = TR.fleet_handoff("先月のメールを一覧して", "j1", str(state))
    assert status == "dispatched" and result["delivered"] == "add_goal"
    adds = _commands(state)["add_goal"]
    assert len(adds) == 1 and adds[0]["text"] == "先月のメールを一覧して"
    assert adds[0]["priority"] is False, "an inbound goal must not jump the current work"


def test_a_second_goal_is_appended_not_replacing_the_first(state):
    _status(state, True)
    TR.fleet_handoff("first", "j1", str(state))
    TR.fleet_handoff("second", "j2", str(state))
    assert [a["text"] for a in _commands(state)["add_goal"]] == ["first", "second"]


def test_other_commands_in_the_file_survive(state):
    """commands.json is shared with the cockpit's stop/pause. Rewriting it wholesale would
    drop a pause a human had just set."""
    _status(state, True)
    with open(os.path.join(str(state), "commands.json"), "w", encoding="utf-8") as fh:
        json.dump({"pause": True}, fh)
    TR.fleet_handoff("a goal", "j1", str(state))
    cur = _commands(state)
    assert cur["pause"] is True and len(cur["add_goal"]) == 1


def test_the_file_is_written_without_a_bom(state):
    """The fleet reads utf-8-sig, so a BOM parses -- but code_task.py writes none, and two
    writers of one file should write it the same way."""
    _status(state, True)
    TR.fleet_handoff("a goal", "j1", str(state))
    with open(os.path.join(str(state), "commands.json"), "rb") as fh:
        assert not fh.read(3).startswith(b"\xef\xbb\xbf")


# -- what it does NOT do ---------------------------------------------------------------------------

def test_with_no_run_in_flight_the_goal_waits_and_says_so(state):
    """The old branch said "dispatched" for a file nobody read. A status that overstates what
    happened is how a queue goes unnoticed for months."""
    _status(state, False)
    status, result = TR.fleet_handoff("a goal", "j1", str(state))
    assert status == "awaiting_fleet"
    assert "waits" in result["note"]
    assert _commands(state) == {}, "nothing should be queued into a run that is not there"


def test_it_does_not_start_a_fleet_by_itself(state):
    """Spawning starts a browser, a set of workers and spends the tenant's Copilot budget.
    Doing that because a sentence arrived over a tunnel is a bigger act than queueing one."""
    assert TR.AUTOSTART is False, "autostart must be opt-in"
    _status(state, False)
    TR.fleet_handoff("a goal", "j1", str(state))
    assert _commands(state) == {}


def test_an_empty_goal_is_an_error_not_a_delivery(state):
    _status(state, True)
    status, _ = TR.fleet_handoff("   ", "j1", str(state))
    assert status == "error"
    assert _commands(state) == {}


# -- the whole path, through run_job ------------------------------------------------------------

def test_a_fleet_goal_job_reaches_the_running_fleet(state):
    """End to end: the job type an agent submits, through the router, into the run."""
    _status(state, True)
    job = {"id": "j9", "type": "fleet_goal", "payload": {"goal": "do the thing"}}
    rec = TR.run_job(job)
    assert rec["status"] == "dispatched"
    assert [a["text"] for a in _commands(state)["add_goal"]] == ["do the thing"]


def test_a_goal_that_could_not_be_delivered_is_kept_where_it_can_be_retried(state):
    """for_fleet/ MEANS ONE THING NOW, and this test used to assert the other one.

    It read "the record of the goal, even now that delivery is real" -- so the file was
    written whether or not delivery happened, and the directory held both kinds at once. That
    was fine as a record and useless as a queue, which is why a goal arriving with no fleet
    running was filed as awaiting_fleet and then never delivered: nothing could tell which
    files were still owed.

    The record of a delivered goal is its done/ entry, which says dispatched and how. What
    stays here is what is still owed.
    """
    _status(state, False)
    TR.run_job({"id": "j9", "type": "fleet_goal", "payload": {"goal": "do the thing"}})
    with open(os.path.join(TR.TASKS, "for_fleet", "j9.txt"), encoding="utf-8") as fh:
        assert fh.read() == "do the thing"


def test_where_the_instruction_came_from_reaches_the_archive(tmp_path, monkeypatch):
    """It was recorded at the door and dropped at the record. The done/ file for the first
    real submission read origin=None, and by then the pending file that held it was gone --
    so the one place the difference survived had just been deleted."""
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(tmp_path / "state"))
    TR.ensure_dirs()
    job = {"id": "j1", "type": "fleet_goal", "payload": {"goal": "do a thing"},
           "origin": {"via": "mcp", "source": "an agent"}}
    rec = TR.run_job(job, now_ts=1.0)
    assert rec.get("origin") == {"via": "mcp", "source": "an agent"}


def test_a_job_with_no_origin_does_not_grow_an_empty_one(tmp_path, monkeypatch):
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(tmp_path / "state"))
    TR.ensure_dirs()
    rec = TR.run_job({"id": "j2", "type": "fleet_goal", "payload": {"goal": "x"}}, now_ts=1.0)
    assert "origin" not in rec


# -- more than one device, at the same moment -------------------------------------------------

def test_goals_arriving_together_do_not_delete_each_other(tmp_path, monkeypatch):
    """THE ONE THAT WOULD HAVE LOST WORK. The instructions do not all come from one place --
    two phones can submit at the same moment, and code_task.py writes commands.json too.

    The write was atomic; the read-append-write around it was not. Both callers read the same
    list, each appended its own goal, and whichever replaced second silently deleted the
    other. A lost goal is indistinguishable from a goal that was never sent.
    """
    import threading
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(state))

    n = 24
    errors = []

    def submit(i):
        try:
            TR.add_goal_to_live_fleet("goal %02d" % i, str(state))
        except Exception as exc:  # pragma: no cover - a failure here is the finding
            errors.append(exc)

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    import json as _json
    with io.open(str(state / "commands.json"), encoding="utf-8-sig") as fh:
        got = _json.load(fh)
    texts = sorted(g["text"] for g in got["add_goal"])
    assert len(texts) == n, "%d of %d goals survived" % (len(texts), n)
    assert texts == sorted("goal %02d" % i for i in range(n))


def test_the_command_file_is_never_left_half_written(tmp_path, monkeypatch):
    """Every writer used the same `commands.json.tmp`, so two of them clobbered each other's
    partial file before either replace() ran. Each takes a name of its own now."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(state))
    TR.add_goal_to_live_fleet("a", str(state))
    TR.add_goal_to_live_fleet("b", str(state))
    leftovers = [p.name for p in state.iterdir() if p.name.endswith(".tmp")
                 or p.name.endswith(".lock")]
    assert not leftovers, "left behind: %s" % leftovers


def test_a_lock_left_by_a_crashed_writer_does_not_wedge_delivery(tmp_path, monkeypatch):
    """A goal dropped because a previous process died holding a lock would be indistinguishable
    from one never sent. Ten seconds is far longer than a read and a write."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(state))
    stale = state / "commands.json.lock"
    stale.write_text("", encoding="utf-8")
    monkeypatch.setattr(TR.time, "time", _clock_running_fast())
    TR.add_goal_to_live_fleet("after a crash", str(state))
    import json as _json
    with io.open(str(state / "commands.json"), encoding="utf-8-sig") as fh:
        got = _json.load(fh)
    assert [g["text"] for g in got["add_goal"]] == ["after a crash"]


def _clock_running_fast():
    """A clock that jumps a minute per call, so the stale-lock deadline is reached at once
    instead of the test sleeping through it."""
    import itertools
    counter = itertools.count(0, 60.0)
    return lambda: next(counter)


# -- a goal that arrived before the fleet did ---------------------------------------------------

def _tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(exist_ok=True)
    TR.ensure_dirs()
    return tmp_path


def _live(tmp_path, running=True):
    """Write the status file fleet_is_live reads, with a pid that is really alive."""
    import json as _json
    import os as _os
    with io.open(str(tmp_path / "state" / "status.json"), "w", encoding="utf-8") as fh:
        _json.dump({"running": running, "pid": _os.getpid(), "ts": 9e9}, fh)


def test_a_goal_that_arrived_with_no_fleet_is_delivered_when_one_starts(tmp_path, monkeypatch):
    """THE GAP. It was filed in done/ as awaiting_fleet and never looked at again -- the
    status was the only thing waiting. Six such records had built up."""
    _tasks(tmp_path, monkeypatch)
    rec = TR.run_job({"id": "j1", "type": "fleet_goal",
                      "payload": {"goal": "do the thing"}}, now_ts=1.0)
    assert rec["status"] == "awaiting_fleet"
    assert os.path.isfile(os.path.join(TR.TASKS, "for_fleet", "j1.txt"))

    _live(tmp_path)
    out = TR._deliver_waiting_goals(now_ts=2.0)
    assert [r["status"] for r in out] == ["dispatched"]
    assert out[0]["result"]["delivered_late"] is True

    import json as _json
    with io.open(str(tmp_path / "state" / "commands.json"), encoding="utf-8-sig") as fh:
        cmds = _json.load(fh)
    assert [g["text"] for g in cmds["add_goal"]] == ["do the thing"]


def test_a_delivered_goal_leaves_nothing_waiting(tmp_path, monkeypatch):
    """for_fleet/ means one thing now: goals still waiting. A delivered goal's record is its
    done/ entry, which says dispatched and how."""
    _tasks(tmp_path, monkeypatch)
    _live(tmp_path)
    rec = TR.run_job({"id": "j2", "type": "fleet_goal", "payload": {"goal": "x"}}, now_ts=1.0)
    assert rec["status"] == "dispatched"
    assert os.listdir(os.path.join(TR.TASKS, "for_fleet")) == []


def test_it_keeps_waiting_while_no_fleet_runs(tmp_path, monkeypatch):
    """A pass with nothing in flight must not consume the goal."""
    _tasks(tmp_path, monkeypatch)
    TR.run_job({"id": "j3", "type": "fleet_goal", "payload": {"goal": "y"}}, now_ts=1.0)
    assert TR._deliver_waiting_goals(now_ts=2.0) == []
    assert os.path.isfile(os.path.join(TR.TASKS, "for_fleet", "j3.txt"))


def test_the_waiting_file_survives_a_failed_delivery(tmp_path, monkeypatch):
    """Removing it first would lose the goal if the write failed, and a lost goal looks
    exactly like one that was never sent."""
    _tasks(tmp_path, monkeypatch)
    TR.run_job({"id": "j4", "type": "fleet_goal", "payload": {"goal": "z"}}, now_ts=1.0)
    _live(tmp_path)

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(TR, "add_goal_to_live_fleet", boom)
    try:
        TR._deliver_waiting_goals(now_ts=2.0)
    except OSError:
        pass
    assert os.path.isfile(os.path.join(TR.TASKS, "for_fleet", "j4.txt"))


def test_an_empty_waiting_file_is_not_retried_forever(tmp_path, monkeypatch):
    _tasks(tmp_path, monkeypatch)
    TR.ensure_dirs()
    with io.open(os.path.join(TR.TASKS, "for_fleet", "j5.txt"), "w", encoding="utf-8") as fh:
        fh.write("   ")
    _live(tmp_path)
    assert TR._deliver_waiting_goals(now_ts=2.0) == []
    assert not os.path.isfile(os.path.join(TR.TASKS, "for_fleet", "j5.txt"))


def test_both_readers_of_the_fleet_status_agree_on_what_live_means():
    """THE AGREEMENT IS THE REQUIREMENT, and nothing was holding it.

    task_router and code_task both decide "is a fleet running" from the same file, and the
    failure they exist to prevent is on the record: two fleets share the one dedicated Edge
    and clobber each other's status.json, and the second run's work appeared as a phantom
    worker behind the first. If the two readers disagree, one of them starts the second fleet.

    They agree today -- age plus the running flag, thirty seconds, utf-8-sig -- but one holds
    the threshold in a named constant and the other has it inline, so a change to either would
    part them silently. This is the check that would notice.
    """
    import re
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(repo, "relay", "code_task.py"), encoding="utf-8").read()
    i = src.index("def _fleet_is_live")
    body = src[i:i + 700]
    ages = [int(m) for m in re.findall(r"getmtime\(sp\)\)\s*>\s*(\d+)", body)]
    assert ages, "code_task._fleet_is_live no longer compares the file age; re-derive this"
    assert ages[0] == TR.FLEET_LIVE_MAX_AGE_S, (
        "code_task treats a status file as live for %ds, task_router for %ds -- the two "
        "disagree, and the one that says 'not live' will start a second fleet"
        % (ages[0], TR.FLEET_LIVE_MAX_AGE_S))
    assert "running" in body, "code_task no longer reads the running flag"
    assert "utf-8-sig" in body, "code_task no longer tolerates a BOM; the writers must match"


def test_a_goal_in_done_with_no_fleet_is_not_finished(tmp_path, monkeypatch):
    """`done/` holds the ROUTER's record, not a completed job, and the two can disagree.

    A reader auditing the queue found the record in done/, found no matching *.delivered.json,
    and reported a job that had left for_fleet/ by a route they could not determine. Nothing
    had moved: the record and the handoff are two artifacts written in the same pass, under
    different extensions, and only the handoff is the outstanding work. The docstring said
    "finished", which is what made the wrong reading the natural one.
    """
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    TR.ensure_dirs()
    jid = "abc123"
    with open(os.path.join(TR.TASKS, "pending", "%s.json" % jid), "w", encoding="utf-8") as fh:
        json.dump({"id": jid, "type": "fleet_goal", "payload": {"goal": "do a thing"},
                   "created": time.time()}, fh)
    TR.dispatch_once()

    rec_path = os.path.join(TR.TASKS, "done", "%s.json" % jid)
    assert os.path.isfile(rec_path), "the router leaves a record of every job it handled"
    with open(rec_path, encoding="utf-8") as fh:
        rec = json.load(fh)
    assert rec["status"] == "awaiting_fleet"
    assert rec["ts_done"] is None, "a record in done/ can still be unfinished; ts_done says so"

    handoff = os.path.join(TR.TASKS, "for_fleet", "%s.txt" % jid)
    assert os.path.isfile(handoff), "the outstanding work is the handoff, not the record"
    assert not os.path.isfile(os.path.join(TR.TASKS, "done", "%s.delivered.json" % jid)), (
        "the delivery record must appear only after a fleet actually took the goal")


def test_a_lock_in_delete_pending_is_contention_not_a_failure(tmp_path, monkeypatch):
    """Windows raises PermissionError, not FileExistsError, for a lock being released.

    A file whose last handle has just closed with an unlink outstanding sits in delete-pending,
    and O_CREAT|O_EXCL on it fails with ERROR_ACCESS_DENIED. Catching only FileExistsError let
    that out of add_goal_to_live_fleet: with 24 concurrent submitters it raised in 2 runs out
    of 8, most threads at a time. Deterministic here, because a race reproduced two times in
    eight is not a test anyone can act on.
    """
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(state))

    real_open = os.open
    calls = {"n": 0}

    def flaky(path, flags, *a, **k):
        if str(path).endswith("commands.json.lock"):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", flaky)
    TR.add_goal_to_live_fleet("survives a lock in delete-pending", str(state))

    assert calls["n"] >= 3, "the two refusals were not actually exercised"
    with io.open(os.path.join(str(state), "commands.json"), encoding="utf-8-sig") as fh:
        data = json.load(fh)
    assert [g["text"] for g in data["add_goal"]] == ["survives a lock in delete-pending"]


def test_a_reader_holding_commands_json_does_not_lose_the_goal(tmp_path, monkeypatch):
    """A rename onto a path someone else has open fails on Windows, and the fleet reads
    commands.json on its own schedule. Bounded retry, so a goal does not vanish because the
    consumer happened to be reading at that moment."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(state))

    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst, *a, **k):
        if str(dst).endswith("commands.json"):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(13, "Permission denied")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", flaky)
    TR.add_goal_to_live_fleet("survives a reader", str(state))

    assert calls["n"] >= 3, "the two refusals were not actually exercised"
    with io.open(os.path.join(str(state), "commands.json"), encoding="utf-8-sig") as fh:
        data = json.load(fh)
    assert [g["text"] for g in data["add_goal"]] == ["survives a reader"]


def test_every_python_writer_of_commands_json_takes_the_same_lock(tmp_path, monkeypatch):
    """A LOCK ONE WRITER TAKES IS NOT A LOCK.

    The lock went into add_goal_to_live_fleet with a comment naming relay/code_task.py as the
    other writer of this file -- and code_task kept its own unlocked read-append-write, as did
    bench/fleet_ctl.py, whose docstring claims it "merges, so a queued add_goal isn't
    clobbered". Two of the three Python writers could still delete the goals the lock existed
    to protect. This drives the router's writer and fleet_ctl's merge together and requires
    both to survive.
    """
    import threading
    state = tmp_path / "state"
    state.mkdir()
    path = os.path.join(str(state), "commands.json")

    n = 12
    barrier = threading.Barrier(n * 2)
    errors = []

    def as_router(i):
        barrier.wait()
        try:
            TR.add_goal_to_live_fleet("router goal %02d" % i, str(state))
        except Exception as exc:
            errors.append(exc)

    def as_merger(i):
        barrier.wait()
        try:
            TR.write_commands(path, lambda cur: cur.update({"flag%02d" % i: True}))
        except Exception as exc:
            errors.append(exc)

    threads = ([threading.Thread(target=as_router, args=(i,)) for i in range(n)]
               + [threading.Thread(target=as_merger, args=(i,)) for i in range(n)])
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    with io.open(path, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    assert len(data.get("add_goal") or []) == n, (
        "goals were lost: %d of %d survived" % (len(data.get("add_goal") or []), n))
    assert sum(1 for k in data if k.startswith("flag")) == n, (
        "merges were lost: %d of %d survived" % (sum(1 for k in data if k.startswith("flag")), n))


def test_code_task_no_longer_writes_the_file_itself():
    """The other writer named in the router's own comment. It is a separate PROCESS, so no
    runtime test can hold the two together -- what is checkable is that it stopped keeping a
    private copy of the read-append-write."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(repo, "relay", "code_task.py"), encoding="utf-8").read()
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "add_goal_to_live_fleet" in body, "code_task must go through the shared writer"
    assert 'cmds_path + ".tmp"' not in body, (
        "code_task is writing commands.json itself again, outside the lock")

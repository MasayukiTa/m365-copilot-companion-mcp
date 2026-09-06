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
    """What the fleet would see, through the fleet's OWN reader.

    THE REAL CONSUMER, NOT A COPY OF IT. These assertions used to open commands.json and parse
    it here, which tested the writer against this file's idea of the format rather than against
    relay/fleet_runner. Commands are one file each now, so the reader also decides the order
    they arrive in; going through it means a change to either side shows up here.

    read_commands CONSUMES what it returns, exactly as the running fleet does, so a test that
    calls this twice sees the second call empty -- which is the truth about the channel.
    Merged into one dict the way the runner applies them: add_goal entries accumulate in order,
    every other key is last-one-wins.
    """
    from relay import fleet_runner as FR
    merged = {}
    for cmd in FR.read_commands(str(state_dir)):
        for k, v in (cmd or {}).items():
            if k == "add_goal":
                items = v if isinstance(v, list) else [v]
                merged.setdefault("add_goal", []).extend(items)
            else:
                merged[k] = v
    return merged


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
    writers of one channel should write it the same way."""
    _status(state, True)
    TR.fleet_handoff("a goal", "j1", str(state))
    files = sorted(os.listdir(os.path.join(str(state), TR.COMMANDS_DIR)))
    assert len(files) == 1, files
    with open(os.path.join(str(state), TR.COMMANDS_DIR, files[0]), "rb") as fh:
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
    two phones can submit at the same moment, and code_task.py and the cockpit write here too.

    The write was always atomic; the read-append-write AROUND it was not. Both callers read the
    same list, each appended its own goal, and whichever replaced second silently deleted the
    other. A lost goal is indistinguishable from a goal that was never sent. A lock fixed that
    for the Python writers and could not fix it for the cockpit; one file per command removes
    the read-modify-write that made a lock necessary at all. This is the test that would have
    caught the original defect, and it still has to pass with no lock anywhere.
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
    texts = sorted(g["text"] for g in _commands(state)["add_goal"])
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


def test_a_writer_that_died_midwrite_costs_nothing(tmp_path, monkeypatch):
    """What replaced the stale-lock case, because there is no lock to go stale any more.

    A writer that dies between opening its temp file and renaming it leaves a .tmp behind. That
    file must not be delivered (it may be half a command) and must not stop anything else from
    being delivered. The old design's equivalent hazard was a lock file left by a crashed
    process, which wedged every later goal until it was broken by timeout -- a goal dropped for
    that reason is indistinguishable from one never sent.
    """
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(state))
    d = os.path.join(str(state), TR.COMMANDS_DIR)
    os.makedirs(d, exist_ok=True)
    with io.open(os.path.join(d, "0000000000000000000-dead.tmp"), "w", encoding="utf-8") as fh:
        fh.write('{"add_goal": [{"text": "half a comm')

    TR.add_goal_to_live_fleet("after a crash", str(state))
    assert [g["text"] for g in _commands(state)["add_goal"]] == ["after a crash"]


def test_a_command_that_will_not_parse_is_set_aside_not_retried_forever(tmp_path, monkeypatch):
    """Deleting it would destroy the instruction; leaving it would block the reader on every
    sweep for the life of the run. It is renamed .bad, which does neither."""
    from relay import fleet_runner as FR
    state = tmp_path / "state"
    state.mkdir()
    d = os.path.join(str(state), TR.COMMANDS_DIR)
    os.makedirs(d, exist_ok=True)
    bad = os.path.join(d, "0000000000000000001-junk.json")
    with io.open(bad, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    TR.write_command(str(state), {"add_goal": [{"text": "the good one"}]})

    got = FR.read_commands(str(state))
    assert [g["text"] for c in got for g in FR.goals_from_command(c)] == ["the good one"]
    assert not os.path.isfile(bad), "the unparsable command was left to be retried forever"
    assert os.path.isfile(bad + ".bad"), "the instruction was destroyed instead of set aside"
    assert FR.read_commands(str(state)) == [], "a .bad file came back on the next sweep"
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
    assert [g["text"] for g in _commands(tmp_path / "state")["add_goal"]] == ["do the thing"]
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


def test_two_writers_never_choose_the_same_filename(tmp_path, monkeypatch):
    """What makes the lock unnecessary, so it is worth pinning directly.

    The name is time_ns plus a random tail. The tail is not decoration: a clock is not a
    counter, and this repo has already shipped an id that assumed otherwise -- Windows advances
    time.time in ~15.6 ms steps, so a name derived from the clock alone repeats, and here a
    repeat means one command silently overwrites another.
    """
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(TR.time, "time_ns", lambda: 1788600000000000000)
    paths = {TR.write_command(str(state), {"n": i}) for i in range(200)}
    assert len(paths) == 200, "%d of 200 names were distinct under one clock reading" % len(paths)
    assert len(os.listdir(os.path.join(str(state), TR.COMMANDS_DIR))) == 200
def test_a_reader_holding_the_file_does_not_lose_the_goal(tmp_path, monkeypatch):
    """A rename onto a path someone else has open fails on Windows, and the fleet reads on its
    own schedule. Bounded retry, so a goal does not vanish because the consumer happened to be
    reading at that moment."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(state))

    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst, *a, **k):
        if str(dst).endswith(".json"):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(13, "Permission denied")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", flaky)
    TR.add_goal_to_live_fleet("survives a reader", str(state))

    assert calls["n"] >= 3, "the two refusals were not actually exercised"
    assert [g["text"] for g in _commands(state)["add_goal"]] == ["survives a reader"]
def test_every_writer_drops_its_own_file_and_none_are_lost(tmp_path, monkeypatch):
    """A LOCK ONE WRITER TAKES IS NOT A LOCK -- so this channel no longer needs one.

    The lock went into add_goal_to_live_fleet under a comment naming relay/code_task.py as the
    other writer, and code_task kept its own unlocked read-append-write, as did
    bench/fleet_ctl.py, whose docstring claimed it "merges, so a queued add_goal isn't
    clobbered". ui/CopilotChat.cs writes here too, from a separately built binary that could
    never have taken a Python lock. One file per command makes the question moot: this drives
    goal writers and patch writers into the channel at the same moment and requires every
    single edit to arrive.
    """
    import threading
    state = tmp_path / "state"
    state.mkdir()

    n = 12
    barrier = threading.Barrier(n * 2)
    errors = []

    def as_router(i):
        barrier.wait()
        try:
            TR.add_goal_to_live_fleet("router goal %02d" % i, str(state))
        except Exception as exc:
            errors.append(exc)

    def as_patcher(i):
        barrier.wait()
        try:
            TR.write_command(str(state), {"flag%02d" % i: True})
        except Exception as exc:
            errors.append(exc)

    threads = ([threading.Thread(target=as_router, args=(i,)) for i in range(n)]
               + [threading.Thread(target=as_patcher, args=(i,)) for i in range(n)])
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    merged = _commands(state)
    assert len(merged.get("add_goal") or []) == n, (
        "goals were lost: %d of %d survived" % (len(merged.get("add_goal") or []), n))
    assert sum(1 for k in merged if k.startswith("flag")) == n, (
        "patches were lost: %d of %d survived" % (sum(1 for k in merged if k.startswith("flag")), n))
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


# -- the seam: what the router writes is what the runner reads ---------------------------------

def test_the_runner_reads_back_exactly_the_goal_the_router_delivered(tmp_path, monkeypatch):
    """THE JOIN NOBODY TESTED. Both halves had tests; the seam between them had none.

    relay/task_router writes this channel; relay/fleet_runner reads it. Each side was covered
    against its own fixture, so a key renamed on one side would pass everything and deliver
    nothing. That is not hypothetical here: the archive wrote `gate_verdict` while the
    scheduler read `verdict`, and an adapter wrote `keep` while the policy read `kept` -- both
    sides green throughout, both found by a person reading code.

    This drives the REAL writer and the REAL reader, with no browser and no Copilot turn:
    fleet_is_live only asks for a fresh status.json saying running.
    """
    state = tmp_path / "state"
    state.mkdir()
    with io.open(os.path.join(str(state), "status.json"), "w", encoding="utf-8") as fh:
        json.dump({"running": True}, fh)

    TR.add_goal_to_live_fleet("summarise last month's mail", str(state))
    goals = _commands(state)["add_goal"]

    assert [g["text"] for g in goals] == ["summarise last month's mail"]
    assert goals[0].get("priority") is False
def test_the_richer_entry_code_task_sends_survives_the_round_trip(tmp_path, monkeypatch):
    """code_task adds cwd and checks so a retry re-runs WITH its acceptance gate. If those are
    dropped in transit the goal still runs, and silently runs ungated -- which looks like a
    pass."""
    from relay import fleet_runner as FR

    state = tmp_path / "state"
    state.mkdir()
    with io.open(os.path.join(str(state), "status.json"), "w", encoding="utf-8") as fh:
        json.dump({"running": True}, fh)

    entry = {"text": "fix the failing test", "priority": True,
             "cwd": r"C:\work\proj", "checks": ["pytest -q"]}
    TR.add_goal_to_live_fleet(entry["text"], str(state), entry=entry)

    goals = [g for c in FR.read_commands(str(state)) for g in FR.goals_from_command(c)]
    assert goals == [entry], "a field was lost between the writer and the reader: %r" % (goals,)
def test_several_goals_arrive_in_the_order_they_were_sent(tmp_path, monkeypatch):
    """Two phones, one run. The reader must see both, in the order they were sent -- which is
    now decided by the filename, so the ordering is the reader's to get right."""
    from relay import fleet_runner as FR

    state = tmp_path / "state"
    state.mkdir()
    with io.open(os.path.join(str(state), "status.json"), "w", encoding="utf-8") as fh:
        json.dump({"running": True}, fh)

    for i in range(3):
        TR.add_goal_to_live_fleet("goal %d" % i, str(state))

    goals = [g for c in FR.read_commands(str(state)) for g in FR.goals_from_command(c)]
    assert [g["text"] for g in goals] == ["goal 0", "goal 1", "goal 2"]
def test_a_full_job_from_the_intake_door_reaches_the_runners_reader(tmp_path, monkeypatch):
    """END TO END, WITHOUT A BROWSER OR A COPILOT TURN: the door, the router, the channel, the
    reader. The only thing simulated is that a fleet is running, which fleet_is_live decides
    from a fresh status.json -- so every line of the delivery path is the real one."""
    from tools import fleet_intake as FI

    state = tmp_path / "state"
    state.mkdir()
    with io.open(os.path.join(str(state), "status.json"), "w", encoding="utf-8") as fh:
        json.dump({"running": True}, fh)
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(state))
    TR.ensure_dirs()

    out = FI.fleet_submit("check the coating DB for last week", source="phone (verification)")
    jid = out.split()[1]
    TR.dispatch_once()

    with io.open(os.path.join(TR.TASKS, "done", "%s.json" % jid), encoding="utf-8") as fh:
        rec = json.load(fh)
    assert rec["status"] == "dispatched", "the router did not deliver it: %r" % (rec,)
    assert rec["origin"]["source"] == "phone (verification)", "provenance was lost"
    assert not os.path.isfile(os.path.join(TR.TASKS, "for_fleet", "%s.txt" % jid)), (
        "a delivered goal must not also be left waiting")

    assert [g["text"] for g in _commands(state)["add_goal"]] == [
        "check the coating DB for last week"]


def test_order_survives_a_clock_that_does_not_move(tmp_path, monkeypatch):
    """The ordering bug, made deterministic.

    Windows advances time.time_ns in ~15.6 ms steps, so goals sent in quick succession share a
    reading and the random tail decided their order. Measured before the per-process counter
    went in: 0, 1, 2 were sent and 0, 2, 1 came out. Freezing the clock reproduces that
    condition on any platform, so this fails on Linux CI too rather than only here.
    """
    from relay import fleet_runner as FR
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(TR.time, "time_ns", lambda: 1788600000000000000)

    for i in range(25):
        TR.add_goal_to_live_fleet("goal %02d" % i, str(state))

    goals = [g for c in FR.read_commands(str(state)) for g in FR.goals_from_command(c)]
    assert [g["text"] for g in goals] == ["goal %02d" % i for i in range(25)], (
        "commands written inside one clock tick did not keep their order")

# -*- coding: utf-8 -*-
"""A task submitted to the fleet is at the TOP of the cockpit's list at once -- EXECUTED.

## The requirement

"フリートでタスクを追加したら、キューに放り込み完了まで新規タスクとして上部に表示されない。
これを放り込んだらキューへ追加されているかはさておき、とりあえず上に表示されるようにして。must"

## What was wrong

`ui/FleetCockpit.cs` built the list from `.fleet/status.json` (workers) and drew the queue only
inside `EmptyState()` -- only when there was no run and no history. With a run live, or anything
in history, a submission was invisible until a worker existed; and a new worker in status
"pending" then sorted BELOW every active card.

## What this runs

`ui/SubmittedTasks.cs` -- the shipped file that decides which "submitted, not picked up yet"
rows exist, their labels, and the order of the whole list -- compiled with the real csc beside
the shipped command writer `ui/FleetCommands.cs` and the test-only driver
`ui/harness/SubmittedTasksHarness.cs`, then run against REAL files in a tmp dir laid out like
`.fleet/`:

* commands.d files written by the shipped C# writer (`FleetCommands.Write`, both exes' only
  writer) and by the Python one (`relay.task_router.write_command`), and consumed by the
  runner's own reader (`relay.fleet_runner.read_commands`);
* tasks/pending files written by the real `tools.fleet_intake.fleet_submit`, pointed at a tmp
  queue by patching `relay.task_router.TASKS` (the same isolation tools/test_fleet_intake.py
  and tools/test_a_locked_out_worker_should_not_clone_its_own_work.py use);
* tasks/for_fleet files written by `relay.task_router._write_for_fleet`, both shapes;
* status.json written by `relay.fleet_runner._snapshot` + `_write_atomic`.

## What is NOT executed here

The WPF side: that `FleetCockpit.BuildRows` / the idle branch of `OnTick` emit the submitted
rows first (they emit them in `SubmittedTasks.Compose` order, but the emission itself is WPF
code), that `SpawnFleet` / `RetryGoal` / bulk retry call `NoteSubmitted`, and what the row
looks like. `ui/test_both_windows_can_be_constructed.py` proves the cockpit still constructs
with this file in its Build line; the rest is a live GUI check.

## Skips

Only on a non-Windows host. On Windows a missing csc skips unless `REQUIRE_CSC=1` (CI), which
fails instead. A compile failure, a harness failure, or zero cases always fail.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from types import SimpleNamespace

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

UI = os.path.join(REPO, "ui")
FW = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
CSC = os.path.join(FW, "csc.exe")

#: Compiled together and nothing else: the shipped file under test, the shipped command
#: writer, and the test-only driver.
SOURCES = [
    os.path.join(UI, "SubmittedTasks.cs"),
    os.path.join(UI, "FleetCommands.cs"),
    os.path.join(UI, "harness", "SubmittedTasksHarness.cs"),
]

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="the code under test is C# compiled by the .NET Framework csc, which exists only "
           "on Windows")

UNCONFIRMED_AFTER_S = 600
FRESH_PENDING_S = 180


# ── build ─────────────────────────────────────────────────────────────────────────────────

def _require_csc():
    return os.environ.get("REQUIRE_CSC", "").strip() == "1"


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    if not os.path.isfile(CSC):
        msg = "csc.exe is not at %s" % CSC
        if _require_csc():
            pytest.fail(msg + " and REQUIRE_CSC=1")
        pytest.skip(msg)
    d = tmp_path_factory.mktemp("submitted_build")
    out = str(d / "SubmittedTasksHarness.exe")
    r = childproc.run([CSC, "/nologo", "/target:exe", "/out:" + out,
                       "/r:" + os.path.join(FW, "System.Web.Extensions.dll")] + SOURCES,
                      timeout=300)
    assert r.returncode == 0 and os.path.isfile(out), (
        "csc could not build SubmittedTasks.cs with its harness (rc=%s):\n%s\n%s"
        % (r.returncode, r.stdout, r.stderr))
    return out


def _csharp_write_commands(exe, state_dir, texts):
    """add_goal commands through the SHIPPED writer, FleetCommands.Write."""
    os.makedirs(state_dir, exist_ok=True)
    p = os.path.join(state_dir, "_texts.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(texts, fh, ensure_ascii=False)
    r = childproc.run([exe, "write", state_dir, p], timeout=60)
    os.remove(p)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)


# ── status.json through the fleet's own writer ────────────────────────────────────────────

class _Worker(SimpleNamespace):
    """What `fleet_runner._snapshot` reads off a worker."""

    def tab_load(self):
        return 1


def _w(name, goal, status, born=None):
    pe = [] if born is None else [{"ts": born, "event": "pending", "label": "Queued"}]
    if born is not None and status != "pending":
        pe.append({"ts": born + 1, "event": status, "label": status})
    return _Worker(name=name, goal=goal, status=status, outcome="", turn=1, max_turns=10,
                   reason="", last_response="", tabs=1, phase_events=pe,
                   transcript=r"C:\repo\.fleet\transcripts\1726000000_%s.jsonl" % name)


def _status(dirpath, workers, started=1726000000.0):
    from relay import fleet_runner as FR

    os.makedirs(dirpath, exist_ok=True)
    p = os.path.join(dirpath, "status.json")
    FR._write_atomic(p, FR._snapshot(workers, started, len(workers), max_concurrent=6))
    return p


def _runner_goals(FR, cmds):
    """The goals a live run takes from `cmds`, the way fleet_runner's _apply_command does: one
    command's failure (a JSON array root has no .get) costs that command and nothing else."""
    out = []
    for c in cmds:
        try:
            out.extend(FR.goals_from_command(c))
        except Exception:
            pass
    return out


# ── the scenario, laid out on disk, then run once ─────────────────────────────────────────

G_LOCAL = "ローカルで投入したタスク: レポートを要約する"
G_STALE = "stale optimistic entry that nothing ever confirmed"
G_CMD = "goal sent through the command channel by the C# writer"
G_PYCMD = "goal sent through the command channel by the Python writer"
G_QUEUE = "a job queued through fleet_submit waiting for a coordinator"
G_FF_JSON = "a goal parked for a fleet with a priority flag"
G_FF_TEXT = "a goal parked for a fleet as plain text"
G_RETRY = "retry this goal that got stuck"
G_M_OK = "the one well-formed command in a directory of junk"
G_M_BOM = "a command written with a byte order mark"
G_M_DICT = "a command whose add_goal is a single object"
G_DEDUP = "one goal submitted three ways at once"


@pytest.fixture(scope="module")
def world(exe, tmp_path_factory):
    from relay import fleet_runner as FR
    from relay import task_router as TR
    from tools import fleet_intake as FI

    root = str(tmp_path_factory.mktemp("submitted_world"))
    T = time.time()
    none = os.path.join(root, "empty")          # a .fleet with nothing in it
    os.makedirs(none)
    w = {"T": T, "root": root, "none": none}

    # --- commands.d lifecycle: written (C#) -> consumed by the runner -> worker exists ---
    s1 = os.path.join(root, "cmd_written")
    _csharp_write_commands(exe, s1, [G_CMD])
    s2 = os.path.join(root, "cmd_consumed")
    shutil.copytree(s1, s2)
    consumed = FR.read_commands(s2)
    w["consumed"] = [g["text"] for c in consumed for g in FR.goals_from_command(c)]
    s3 = os.path.join(root, "cmd_worker")
    shutil.copytree(s2, s3)
    w["cmd_status"] = _status(s3, [_w("w0", G_CMD, "running", born=T)])
    w.update(s1=s1, s2=s2, s3=s3)

    # --- both writers, one directory: newest first ---
    s4 = os.path.join(root, "two_writers")
    _csharp_write_commands(exe, s4, [G_CMD])
    time.sleep(0.05)
    TR.write_command(s4, {"add_goal": [{"text": G_PYCMD, "priority": False}]})
    w["s4"] = s4

    # --- the queue, through the real fleet_submit, pointed at a tmp tasks dir ---
    tasks = os.path.join(root, "tasks")
    fstate = os.path.join(root, "fleet_state_for_intake")
    os.makedirs(fstate)
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(TR, "TASKS", tasks)
        mp.setattr(TR, "FLEET_STATE_DIR", fstate)
        TR.ensure_dirs()
        said = FI.fleet_submit(G_QUEUE, source="test_a_submitted_task_is_on_top_at_once")
        assert said.startswith("queued "), said
        w["jid"] = said.split()[1]
        said = FI.fleet_submit(G_DEDUP, source="test_a_submitted_task_is_on_top_at_once")
        assert said.startswith("queued "), said
        TR._write_for_fleet("ff_json_0001", G_FF_JSON, priority=True)
        TR._write_for_fleet("ff_text_0001", G_FF_TEXT)
        with open(os.path.join(tasks, "for_fleet", "ff_json_0001.txt"), encoding="utf-8") as fh:
            w["ff_json_body"] = fh.read()
        # junk in the queue: ignored, never thrown
        with open(os.path.join(tasks, "pending", "torn.json"), "w", encoding="utf-8") as fh:
            fh.write('{"id": "torn", "payload": {"goal": "half')
        with open(os.path.join(tasks, "pending", "nogoal.json"), "w", encoding="utf-8") as fh:
            json.dump({"id": "nogoal", "payload": {"note": "x"}}, fh)
    finally:
        mp.undo()
    w["tasks"] = tasks

    # the same goal also as a command, for the three-way de-dup
    s5 = os.path.join(root, "dedup_state")
    _csharp_write_commands(exe, s5, [G_DEDUP])
    w["s5"] = s5

    # --- malformed / half-written commands ---
    sm = os.path.join(root, "malformed")
    _csharp_write_commands(exe, sm, [G_M_OK])
    cd = os.path.join(sm, "commands.d")

    def put(name, data):
        with open(os.path.join(cd, name), "wb") as fh:
            fh.write(data)

    put("0000000000000000001-000000000-aaaaaaaa.json", b"{ not json")
    put("0000000000000000002-000000000-bbbbbbbb.json",
        b'{"add_goal":[{"text":"half-written goal that sto')
    put("0000000000000000003-000000000-cccccccc.tmp",
        json.dumps({"add_goal": [{"text": "a writer mid-flight"}]}).encode("utf-8"))
    put("0000000000000000004-000000000-dddddddd.json.bad",
        json.dumps({"add_goal": [{"text": "one the reader gave up on"}]}).encode("utf-8"))
    put("0000000000000000005-000000000-eeeeeeee.json",
        b"\xef\xbb\xbf" + json.dumps({"add_goal": [{"text": G_M_BOM}]}).encode("utf-8"))
    put("0000000000000000006-000000000-ffffffff.json",
        json.dumps({"add_goal": ["a bare string item"]}).encode("utf-8"))
    put("0000000000000000007-000000000-gggggggg.json",
        json.dumps({"steer": {"worker": "w0", "text": "not a goal"}}).encode("utf-8"))
    put("0000000000000000008-000000000-hhhhhhhh.json", b"")
    put("0000000000000000009-000000000-iiiiiiii.json",
        json.dumps({"add_goal": [{"text": "   "}]}).encode("utf-8"))
    put("0000000000000000010-000000000-jjjjjjjj.json",
        json.dumps({"add_goal": {"text": G_M_DICT}}).encode("utf-8"))
    put("0000000000000000011-000000000-kkkkkkkk.json", b"[1, 2, 3]")
    w["malformed"] = sm
    w["malformed_names"] = sorted(os.listdir(cd))
    # What the RUNNER's own reader makes of the same directory (run on a copy: it consumes).
    copy = os.path.join(root, "malformed_copy")
    shutil.copytree(sm, copy)
    w["runner_goals"] = sorted(g["text"] for g in _runner_goals(FR, FR.read_commands(copy)))

    # --- ordering: every bucket at once ---
    so = os.path.join(root, "ordering")
    w["order_status"] = _status(so, [
        _w("wActive", "an active worker", "running", born=T - 500),
        _w("wOldPending", "held at the gate a long time", "pending", born=T - 600),
        _w("wDone", "a finished worker", "done", born=T - 900),
        _w("wFresh", "a worker created a minute ago", "pending", born=T - 60),
        _w("wEdge", "a worker exactly at the freshness line", "pending", born=T - FRESH_PENDING_S),
        _w("wFresh2", "the newest worker", "pending", born=T - 10),
        _w("wNoEvents", "a pending worker with no phase events", "pending", born=None),
        _w("wActive2", "a second active worker", "verifying", born=T - 400),
        _w("wFreed", "a freed worker", "freed", born=T - 800),
    ])

    # --- retry: the old worker already carries the goal text ---
    sr1 = os.path.join(root, "retry_before")
    w["retry_before"] = _status(sr1, [_w("w3", G_RETRY, "stuck", born=T - 300)])
    sr2 = os.path.join(root, "retry_after")
    w["retry_after"] = _status(sr2, [_w("w3", G_RETRY, "stuck", born=T - 300),
                                     _w("w9", G_RETRY, "pending", born=T + 1)])
    return w


def _cases(w):
    T, none, tasks = w["T"], w["none"], w["tasks"]

    def view(now, state=none, status=None, tk=None):
        return {"op": "view", "now": now, "state": state, "tasks": tk or none, "status": status}

    return [
        {"id": "local_only", "steps": [
            view(T),
            {"op": "local", "goal": G_LOCAL, "now": T, "status": None},
            view(T + 5)]},
        {"id": "stale_local", "steps": [
            {"op": "local", "goal": G_STALE, "now": T, "status": None},
            view(T + 30),
            {"op": "dismiss", "key": G_STALE, "now": T + 30},
            view(T + UNCONFIRMED_AFTER_S + 65),
            view(T + 2 * 3600),
            {"op": "dismiss", "key": G_STALE, "now": T + 2 * 3600},
            view(T + 2 * 3600 + 1)]},
        {"id": "command_lifecycle", "steps": [
            view(T + 2, state=w["s1"]),
            view(T + 3, state=w["s2"]),
            view(T + 4, state=w["s3"], status=w["cmd_status"])]},
        {"id": "two_writers", "steps": [view(T + 2, state=w["s4"])]},
        {"id": "queue", "steps": [view(T + 2, tk=tasks)]},
        {"id": "queue_dedup_with_local", "steps": [
            {"op": "local", "goal": "  " + G_QUEUE.replace(" ", "   ") + "\n", "now": T + 1,
             "status": None},
            view(T + 2, tk=tasks)]},
        {"id": "three_way_dedup", "steps": [
            {"op": "local", "goal": G_DEDUP, "now": T + 1, "status": None},
            view(T + 2, state=w["s5"], tk=tasks)]},
        {"id": "ordering", "steps": [
            {"op": "local", "goal": "older submission", "now": T - 100, "status": w["order_status"]},
            {"op": "local", "goal": "newest submission", "now": T - 5, "status": w["order_status"]},
            view(T, status=w["order_status"])]},
        {"id": "retry_same_goal", "steps": [
            {"op": "local", "goal": G_RETRY, "now": T, "status": w["retry_before"]},
            view(T + 1, status=w["retry_before"]),
            view(T + 2, status=w["retry_after"])]},
        {"id": "malformed", "steps": [view(T + 2, state=w["malformed"])]},
    ]


@pytest.fixture(scope="module")
def results(exe, world):
    cases = _cases(world)
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    d = os.path.join(world["root"], "run")
    os.makedirs(d)
    cpath, rpath = os.path.join(d, "cases.json"), os.path.join(d, "results.json")
    with open(cpath, "w", encoding="utf-8") as fh:
        json.dump(cases, fh, ensure_ascii=False)
    r = childproc.run([exe, "run", cpath, rpath], timeout=120)
    assert r.returncode == 0, "the harness failed (rc=%s):\n%s\n%s" % (
        r.returncode, r.stdout, r.stderr)
    with open(rpath, encoding="utf-8") as fh:
        got = json.load(fh)
    return {"cases": {c["id"]: c for c in cases}, "got": got}


def _steps(results, cid):
    r = results["got"][cid]
    assert r["error"] is None, "%s crashed: %s" % (cid, r["error"])
    return r["steps"]


def _views(results, cid):
    """Only the `view` records of a case, in order."""
    return [s for s in _steps(results, cid) if s["op"] == "view"]


def _keys(step):
    return [s["key"] for s in step["submitted"]]


# ── the run itself ────────────────────────────────────────────────────────────────────────

def test_every_case_ran_and_is_matched_by_id(results):
    got, cases = results["got"], results["cases"]
    assert cases and got, "zero cases ran"
    assert set(got) == set(cases), (sorted(set(cases) - set(got)), sorted(set(got) - set(cases)))
    for cid in cases:
        assert len(_steps(results, cid)) == len(cases[cid]["steps"]), cid


# ── (a) this window's own submission ──────────────────────────────────────────────────────

def test_a_local_submission_is_on_top_before_any_file_exists(results):
    empty, shown = _views(results, "local_only")
    assert empty["submitted"] == [] and empty["order"] == [] and empty["signature_ja"] == ""
    assert shown["order"][0] == "S:" + G_LOCAL, shown["order"]
    s = shown["submitted"][0]
    assert (s["source"], s["backed"], s["unconfirmed"], s["stale"], s["dismissable"]) == \
        ("local", False, True, False, False), s
    # labelled: submitted, not picked up, its age, and that the fleet has not confirmed it
    assert s["label_ja"] == "投入済み・未着手 1分未満 · フリート未確認", s["label_ja"]
    assert s["label_en"] == "submitted, not picked up yet (<1m) · not confirmed by the fleet"
    assert shown["signature_ja"], "a submission that does not change the signature is not re-rendered"


def test_a_stale_local_submission_is_kept_marked_and_only_the_person_removes_it(results):
    _, fresh, refused, stale, later, dismissed, gone = _steps(results, "stale_local")
    assert _keys(fresh) == [G_STALE]
    assert refused["ok"] is False, "a fresh entry the fleet may still pick up was dismissable"
    for step, ja_age in ((stale, "11分"), (later, "2時間0分")):
        assert _keys(step) == [G_STALE], "a stale entry was dropped: %r" % step["submitted"]
        s = step["submitted"][0]
        assert s["unconfirmed"] and s["stale"] and s["dismissable"], s
        assert s["label_ja"] == "投入済み・未着手 %s · フリート未確認" % ja_age, s["label_ja"]
    assert later["submitted"][0]["label_en"] == \
        "submitted, not picked up yet (2h0m) · not confirmed by the fleet"
    assert dismissed["ok"] is True
    assert gone["submitted"] == []


# ── (b) commands.d ────────────────────────────────────────────────────────────────────────

def test_a_command_is_on_top_until_its_worker_exists(results, world):
    written, consumed, worker = _views(results, "command_lifecycle")
    assert written["order"] == ["S:" + G_CMD], written["order"]
    s = written["submitted"][0]
    assert (s["source"], s["backed"], s["unconfirmed"]) == ("command", True, False), s
    assert s["label_ja"].endswith("実行中のランの読込待ち"), s["label_ja"]

    # the runner's own reader took it (and deleted the file) -- the row stays, relabelled
    assert world["consumed"] == [G_CMD]
    assert os.listdir(os.path.join(world["s2"], "commands.d")) == []
    s = consumed["submitted"][0]
    assert (s["key"], s["source"], s["backed"], s["unconfirmed"]) == (G_CMD, "taken", False, False)

    # a worker with the same goal now exists: the row is gone, the worker's card is there
    assert worker["submitted"] == [], worker["submitted"]
    assert worker["order"] == ["W:w0"]


def test_both_command_writers_are_read_and_the_newest_is_first(results):
    step, = _steps(results, "two_writers")
    assert _keys(step) == [G_PYCMD, G_CMD], _keys(step)
    assert all(s["source"] == "command" for s in step["submitted"])


def test_the_command_reader_takes_exactly_what_the_runner_would(results, world):
    """Differential against relay.fleet_runner.read_commands + goals_from_command on a copy of
    the same directory: a torn file, a .tmp, a .bad, a BOM, a bare string, a steer, an empty
    file, a blank text and a single-object add_goal are each read -- or not -- the same way."""
    step, = _steps(results, "malformed")
    ours = sorted(s["goal"] for s in step["submitted"] if s["source"] == "command")
    # THE ONE DECLARED DIFFERENCE: a whitespace-only text is a goal to the runner (its check is
    # truthiness) and no row here -- there is nothing to draw and no key to de-duplicate on.
    runner = [t for t in world["runner_goals"] if t.strip()]
    assert len(runner) == len(world["runner_goals"]) - 1, "the blank-text case was not exercised"
    assert ours == runner, (ours, world["runner_goals"])
    assert ours == sorted([G_M_OK, G_M_BOM, G_M_DICT, "a bare string item"]), ours
    # reading is read-only: every file, junk included, is still there
    assert sorted(os.listdir(os.path.join(world["malformed"], "commands.d"))) == \
        world["malformed_names"]


# ── (c) the queue ─────────────────────────────────────────────────────────────────────────

def test_a_queued_job_is_on_top(results, world):
    step, = _steps(results, "queue")
    by = {s["key"]: s for s in step["submitted"]}
    assert set(by) == {G_QUEUE, G_DEDUP, G_FF_JSON, G_FF_TEXT}, sorted(by)
    assert by[G_QUEUE]["source"] == "pending" and by[G_QUEUE]["id"] == world["jid"], by[G_QUEUE]
    assert by[G_QUEUE]["label_en"].endswith("in the queue, unclaimed")
    # for_fleet: the JSON shape is read as its text, the plain shape as itself
    assert world["ff_json_body"].lstrip().startswith("{"), "the JSON shape was not exercised"
    assert by[G_FF_JSON]["source"] == by[G_FF_TEXT]["source"] == "for_fleet"
    assert by[G_FF_JSON]["goal"] == G_FF_JSON
    # the torn and goal-less pending files were skipped, not thrown
    assert all(s["id"] not in ("torn", "nogoal") for s in step["submitted"])
    assert all(o.startswith("S:") for o in step["order"]) and len(step["order"]) == 4


def test_a_queued_job_and_the_same_local_submission_are_one_row(results, world):
    step, = _views(results, "queue_dedup_with_local")
    hits = [s for s in step["submitted"] if s["key"] == G_QUEUE]
    assert len(hits) == 1, step["submitted"]
    # the file now stands behind it: backed, labelled by where it is, confirmed
    assert (hits[0]["source"], hits[0]["backed"], hits[0]["unconfirmed"]) == \
        ("pending", True, False), hits[0]
    assert hits[0]["id"] == world["jid"]


def test_three_sources_of_one_goal_are_one_row_at_the_furthest_stage(results):
    step, = _views(results, "three_way_dedup")
    hits = [s for s in step["submitted"] if s["key"] == G_DEDUP]
    assert len(hits) == 1, step["submitted"]
    assert hits[0]["source"] == "command", hits[0]


# ── order ─────────────────────────────────────────────────────────────────────────────────

def test_the_order_is_submitted_then_fresh_pending_then_active_then_older_pending_then_done(results):
    step, = _views(results, "ordering")
    assert step["order"] == [
        "S:newest submission", "S:older submission",        # submitted, newest first
        "W:wFresh2", "W:wFresh",                            # fresh pending, newest first
        "W:wActive", "W:wActive2",                          # active, status.json order
        "W:wOldPending", "W:wEdge", "W:wNoEvents",          # older pending (180 s is not fresh)
        "W:wDone", "W:wFreed",                              # terminal
    ], step["order"]


# ── a retry re-submits a goal that is already on the board ────────────────────────────────

def test_a_retry_is_not_swallowed_by_the_worker_it_retries(results):
    before, after = _views(results, "retry_same_goal")
    assert before["order"][0] == "S:" + G_RETRY, before["order"]
    assert before["order"] == ["S:" + G_RETRY, "W:w3"]
    # the NEW worker for it appeared: the row is gone and that worker is on top
    assert after["submitted"] == [] and after["order"] == ["W:w9", "W:w3"], after["order"]

# -*- coding: utf-8 -*-
"""A goal submitted as a JSON wrapper is the same goal as the worker the runner makes of it.

MEASURED 2026-10-18. The cockpit showed "投入済み・未着手 8時間50分 · フリート未確認" for a job that
had RUN (21:48-21:54, ended STUCK, history.json goal_summary "Implement the task in the file ...").
The submitted text was {"cwd": "...", "goal": "Implement the task ..."}. relay/fleet_runner
_read_goals_file turns a goals-file line like that into the goal `d.get("text") or d.get("goal")`
(and the cwd), so the worker's goal was the unwrapped sentence; ui/SubmittedTasks.cs matched
entries to workers by the wrapped text, which no worker ever carries, and GoalTextOf read only
the "text" key. The entry was never confirmed, stayed "unstarted" forever and raised the
stale-queue alarm for work that had finished.

This compiles the shipped ui/SubmittedTasks.cs with the shipped writer and the test harness and
checks, both ways round, that the wrapper and the goal it wraps are ONE entry key; and pins the
cockpit's unwrap rule to the runner's own parser (`_read_goals_file` + `goal_fields`) on every
shape the runner accepts, so the two cannot drift apart again.
"""
from __future__ import annotations

import json
import os
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
SOURCES = [
    os.path.join(UI, "SubmittedTasks.cs"),
    os.path.join(UI, "FleetCommands.cs"),
    os.path.join(UI, "harness", "SubmittedTasksHarness.cs"),
]

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="the code under test is C# compiled by the .NET Framework csc")

GOAL = "Implement the task in the file TASK.md in the git worktree and report DONE"
CWD = r"C:\work\some-repo"
WRAPPER = json.dumps({"cwd": CWD, "goal": GOAL}, ensure_ascii=False)


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    if not os.path.isfile(CSC):
        if os.environ.get("REQUIRE_CSC", "").strip() == "1":
            pytest.fail("csc.exe is not at %s and REQUIRE_CSC=1" % CSC)
        pytest.skip("csc.exe is not at %s" % CSC)
    out = str(tmp_path_factory.mktemp("wrapped_build") / "SubmittedTasksHarness.exe")
    r = childproc.run([CSC, "/nologo", "/target:exe", "/out:" + out,
                       "/r:" + os.path.join(FW, "System.Web.Extensions.dll")] + SOURCES,
                      timeout=300)
    assert r.returncode == 0 and os.path.isfile(out), (r.returncode, r.stdout, r.stderr)
    return out


class _Worker(SimpleNamespace):
    def tab_load(self):
        return 1


def _status(dirpath, goal, started=1726000000.0, born=None):
    from relay import fleet_runner as FR

    os.makedirs(dirpath, exist_ok=True)
    pe = [{"ts": born, "event": "pending", "label": "Queued"}] if born else []
    w = _Worker(name="w0", goal=goal, status="done", outcome="", turn=1, max_turns=10,
                reason="", last_response="", tabs=1, phase_events=pe,
                transcript=r"C:\repo\.fleet\transcripts\1726000000_w0.jsonl")
    p = os.path.join(dirpath, "status.json")
    FR._write_atomic(p, FR._snapshot([w], started, 1, max_concurrent=6))
    return p


def _run(exe, tmp_path, cases):
    cpath, rpath = str(tmp_path / "cases.json"), str(tmp_path / "results.json")
    with open(cpath, "w", encoding="utf-8") as fh:
        json.dump(cases, fh, ensure_ascii=False)
    r = childproc.run([exe, "run", cpath, rpath], timeout=120)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    with open(rpath, encoding="utf-8") as fh:
        got = json.load(fh)
    for cid, v in got.items():
        assert v["error"] is None, "%s crashed: %s" % (cid, v["error"])
    return got


def test_a_wrapped_submission_is_confirmed_by_the_worker_of_the_unwrapped_goal(exe, tmp_path):
    T = time.time()
    st = _status(str(tmp_path / "s1"), GOAL, born=T + 1)
    empty = str(tmp_path / "empty")
    os.makedirs(empty)
    got = _run(exe, tmp_path, [{"id": "wrapped_then_worker", "steps": [
        {"op": "local", "goal": WRAPPER, "now": T, "status": None},
        {"op": "view", "now": T + 5, "state": empty, "tasks": empty, "status": None},
        {"op": "view", "now": T + 8 * 3600, "state": empty, "tasks": empty, "status": st},
    ]}])["wrapped_then_worker"]["steps"]
    before, after = got[1], got[2]
    assert len(before["submitted"]) == 1, before
    assert after["submitted"] == [], (
        "the runner made a worker of this goal and the cockpit still shows it unstarted: %r"
        % after["submitted"])


def test_an_unwrapped_submission_is_confirmed_by_a_worker_that_carries_the_wrapper(exe, tmp_path):
    T = time.time()
    st = _status(str(tmp_path / "s2"), WRAPPER, born=T + 1)
    empty = str(tmp_path / "empty")
    os.makedirs(empty)
    got = _run(exe, tmp_path, [{"id": "plain_then_wrapped_worker", "steps": [
        {"op": "local", "goal": GOAL, "now": T, "status": None},
        {"op": "view", "now": T + 8 * 3600, "state": empty, "tasks": empty, "status": st},
    ]}])["plain_then_wrapped_worker"]["steps"]
    assert got[1]["submitted"] == [], got[1]["submitted"]


def test_the_wrapper_and_the_goal_are_one_row(exe, tmp_path):
    T = time.time()
    empty = str(tmp_path / "empty")
    os.makedirs(empty)
    got = _run(exe, tmp_path, [{"id": "one_row", "steps": [
        {"op": "local", "goal": WRAPPER, "now": T, "status": None},
        {"op": "local", "goal": GOAL, "now": T, "status": None},
        {"op": "view", "now": T + 1, "state": empty, "tasks": empty, "status": None},
    ]}])["one_row"]["steps"]
    v = got[2]["submitted"]
    assert len(v) == 1 and v[0]["goal"] == GOAL, v


# Every shape of line the runner accepts in a goals file, and what the cockpit must read from it.
_SHAPES = [
    ("goal_key", json.dumps({"cwd": CWD, "goal": GOAL})),
    ("text_key", json.dumps({"cwd": CWD, "text": GOAL})),
    ("both_keys_text_wins", json.dumps({"text": GOAL, "goal": "something else"})),
    ("empty_text_goal_used", json.dumps({"text": "", "goal": GOAL})),
    ("plain_text", GOAL),
    ("japanese_wrapper", json.dumps({"goal": "日本語のタスク: 要約する", "cwd": CWD},
                                    ensure_ascii=False)),
    ("with_checks", json.dumps({"goal": GOAL, "checks": [{"type": "shell", "cmd": "x"}]})),
]


def test_the_cockpit_reads_the_goal_the_runner_reads(exe, tmp_path):
    from relay import fleet_runner as FR
    from relay.relay_fleet import goal_fields

    expected = {}
    for name, line in _SHAPES:
        p = str(tmp_path / (name + ".jsonl"))
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
        (goal,) = FR._read_goals_file(p)
        expected[name] = goal_fields(goal)[0]
    cases = [{"id": "pin", "steps": [{"op": "goaltext", "raw": line} for _, line in _SHAPES]}]
    steps = _run(exe, tmp_path, cases)["pin"]["steps"]
    for (name, _line), step in zip(_SHAPES, steps):
        assert step["text"] == expected[name], (
            "%s: the runner reads %r, the cockpit reads %r" % (name, expected[name], step["text"]))

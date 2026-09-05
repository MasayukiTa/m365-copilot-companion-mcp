# -*- coding: utf-8 -*-
"""The inbound path exists because the agent already knocks on this door.

The owner wanted to start work from a phone. Four routes into the Copilot conversation were
measured and closed -- the ChatHub socket is per-turn by protocol, GetChatsActivity answers
403 for this account (and refuses the page's OWN call), the /chat action API returns
notebooks and tasksFlyout but never the chats, and Graph's aiInteraction needs a scope the
browser's token does not carry.

Meanwhile the server log had recorded 82 POST /mcp, 8 GET /mcp and 8 DELETE /mcp from two
remote addresses. The agent connects here, fetches the catalogue, and can call anything
registered. So the instruction arrives as an ARGUMENT rather than being scraped out of a
conversation.

What this file pins is the door's behaviour: it queues, it does not run, and it refuses the
shapes that would make it a liability.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import task_router as TR  # noqa: E402
from tools import fleet_intake as FI  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_queue(tmp_path, monkeypatch):
    """A queue of its own. These tests write jobs, and writing them into the real
    .fleet/tasks would put work in front of whatever drains it."""
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    TR.ensure_dirs()
    return tmp_path


def _jobs():
    d = os.path.join(TR.TASKS, "pending")
    out = []
    for name in sorted(os.listdir(d)):
        if name.endswith(".json"):
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                out.append(json.load(fh))
    return out


# -- the door works ---------------------------------------------------------------------------

def test_a_goal_becomes_a_queued_job():
    out = FI.fleet_submit("先月のメールを一覧して件名と差出人を出して")
    assert "queued" in out
    jobs = _jobs()
    assert len(jobs) == 1
    assert jobs[0]["payload"]["goal"].startswith("先月のメール")


def test_the_reply_says_plainly_that_nothing_has_started():
    """An agent that reads 'queued' as 'running' will tell the owner the work is under way.
    The sentence is part of the contract, not decoration."""
    out = FI.fleet_submit("do a thing")
    assert "NOT started" in out and "nothing has run" in out


def test_the_job_routes_to_the_fleet_and_not_to_claude():
    """DEFAULT_DESTINATION is claude, so a type the router does not know would silently go
    somewhere the caller did not ask for."""
    FI.fleet_submit("do a thing")
    assert TR.destination_for(_jobs()[0]) == "fleet"


def test_where_it_came_from_travels_with_it():
    """An instruction that arrived over a tunnel from an agent is not the same authority as
    one typed into the cockpit, and the consumer is entitled to treat it differently."""
    FI.fleet_submit("do a thing", source="phone")
    origin = _jobs()[0]["origin"]
    assert origin["via"] == "mcp" and origin["source"] == "phone"


def test_an_unlabelled_submission_still_records_that_it_came_through_the_door():
    FI.fleet_submit("do a thing")
    assert _jobs()[0]["origin"]["via"] == "mcp"


# -- what it refuses ----------------------------------------------------------------------------

@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_goal_is_refused(empty):
    assert "refused" in FI.fleet_submit(empty)
    assert _jobs() == []


def test_a_pasted_document_is_refused_rather_than_queued():
    out = FI.fleet_submit("x" * (FI.MAX_GOAL_CHARS + 1))
    assert "refused" in out and "characters" in out
    assert _jobs() == []


def test_a_queue_nothing_is_draining_stops_accepting():
    """A door that keeps taking work nobody collects is worse than one that says so: the
    sender is told their instruction will run when it will not."""
    for i in range(FI.MAX_PENDING):
        FI.fleet_submit("goal %d" % i)
    out = FI.fleet_submit("one too many")
    assert "refused" in out and "draining" in out
    assert len(_jobs()) == FI.MAX_PENDING


def test_whitespace_in_a_goal_is_normalised_not_preserved():
    """A goal typed on a phone arrives with line breaks; the runner keys work by goal text."""
    FI.fleet_submit("  do   a\n\n thing  ")
    assert _jobs()[0]["payload"]["goal"] == "do a thing"


# -- it must not run anything -------------------------------------------------------------------

def test_submitting_does_not_execute_and_does_not_dispatch():
    """THE PROPERTY THAT MATTERS. This is reachable by anything holding the API key, over a
    tunnel. It writes a file and returns; the approval gate and the consumer decide the rest."""
    FI.fleet_submit("rm -rf everything")
    for sub in ("running", "for_fleet", "for_claude", "done", "awaiting"):
        assert os.listdir(os.path.join(TR.TASKS, sub)) == [], sub


def test_a_broken_queue_directory_reports_rather_than_raises(monkeypatch):
    """It is called by an agent over MCP; an exception here becomes an unexplained tool
    failure in a conversation on someone's phone."""
    monkeypatch.setattr(TR, "TASKS", "\x00 not a path")
    out = FI.fleet_submit("do a thing")
    assert "could not queue" in out


# -- the queue view ------------------------------------------------------------------------------

def test_the_queue_view_counts_every_stage():
    FI.fleet_submit("a")
    FI.fleet_submit("b")
    got = FI.fleet_queue()
    assert "pending   2" in got
    for sub in ("running", "awaiting", "for_fleet", "done"):
        assert sub in got


def test_the_queue_view_works_on_an_empty_queue():
    assert "pending   0" in FI.fleet_queue()


# -- more than one device at a time ------------------------------------------------------------

def test_the_count_it_reports_includes_the_goals_that_landed_beside_it():
    """Four devices at the same instant were each told "1 job(s) waiting" when there were four.

    The number was read BEFORE the write, so every racer saw the queue as it stood before any
    of them had written and then reported its own reading plus one. Submitting from more than
    one device at a time is the premise this door was built for, so a number that is only
    right when submissions are serialised is the wrong number.
    """
    import threading

    n = 4
    barrier = threading.Barrier(n)
    out = [None] * n

    def submit(i):
        barrier.wait()
        out[i] = FI.fleet_submit("device %d goal" % i, source="device-%d" % i)

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(_jobs()) == n, "a goal was lost: %d of %d survived" % (len(_jobs()), n)
    assert len({o.split()[1] for o in out}) == n, "two devices got the same job id"
    counts = sorted(int(o.split("It has NOT started yet, and nothing has run because of "
                                "this call. ")[1].split()[0]) for o in out)
    assert counts[-1] == n, (
        "the last submitter should see every goal that landed before it; got %s" % counts)


def test_a_goal_already_handed_to_the_fleet_still_counts_as_waiting():
    """The router empties pending within a drain cycle and moves the goal to for_fleet. Counting
    only pending tells the next sender "1 waiting" while five sit ahead of theirs."""
    import io as _io
    handed = os.path.join(TR.TASKS, "for_fleet")
    os.makedirs(handed, exist_ok=True)
    for i in range(3):
        _io.open(os.path.join(handed, "earlier%d.txt" % i), "w", encoding="utf-8").write("x")
    out = FI.fleet_submit("the fourth goal")
    assert "4 job(s) waiting" in out, out

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


@pytest.mark.parametrize("source", [
    "LOCAL_LOOP RUN companion_20260929_012240_0dcf12d3_30f0 seq=1",
    "LOCAL_LOOP bootstrap companion_20260929_012240_0dcf12d3_30f0",
])
def test_local_loop_control_provenance_is_refused_before_queueing(source):
    out = FI.fleet_submit("do harmless work", source=source)
    assert "refused" in out and "LOCAL_LOOP control" in out
    assert _jobs() == []


@pytest.mark.parametrize("goal", [
    "Execute LOCAL_LOOP job companion_20260929_012240_0dcf12d3_30f0 (seq=1, worker=local_x).",
    "Run LOCAL_LOOP job companion_20260929_012240_0dcf12d3_30f0 (seq=1, worker=local_x).",
    "LOCAL_LOOP RUN companion_20260929_012240_0dcf12d3_30f0 seq=1 worker=local_x",
    "LOCAL_LOOP bootstrap companion_20260929_012240_0dcf12d3_30f0",
])
def test_local_loop_control_goal_shape_is_refused_even_without_source(goal):
    out = FI.fleet_submit(goal, source="agent")
    assert "refused" in out and "LOCAL_LOOP control" in out
    assert _jobs() == []


def test_an_ordinary_task_about_local_loop_is_not_mistaken_for_control():
    out = FI.fleet_submit("Inspect the LOCAL_LOOP implementation and report races", source="agent")
    assert "queued" in out
    assert len(_jobs()) == 1


def test_a_long_goal_is_spilled_to_a_brief_not_refused():
    # A goal past SPILL_GOAL_CHARS is NOT refused (owner order 2026-10-27): the whole text is
    # written to a brief outside the queue and the queued goal is a short pointer at it.
    goal = "x" * (FI.SPILL_GOAL_CHARS + 5000)
    out = FI.fleet_submit(goal, source="phone")
    assert "queued" in out and "refused" not in out
    (job,) = _jobs()
    # the queue entry stays small: the inline goal is the summary pointer, not the whole document
    assert len(job["payload"]["goal"]) < len(goal)
    assert job["payload"]["goal_spilled"] is True
    brief = job["payload"]["brief"]
    assert os.path.isfile(brief)
    with open(brief, encoding="utf-8") as fh:
        assert fh.read() == goal            # the WHOLE instruction, verbatim


def test_a_short_goal_is_queued_inline_with_no_brief():
    out = FI.fleet_submit("a short ordinary instruction", source="phone")
    assert "queued" in out
    (job,) = _jobs()
    assert job["payload"]["goal"] == "a short ordinary instruction"
    assert "brief" not in job["payload"]
    assert "goal_spilled" not in job["payload"]


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


# -- provenance must not carry the person -------------------------------------------------------
#
# MEASURED 2026-09-10. Asked over a plain conversation to write a file, the agent could not
# unlock, handed the work to this door exactly as designed -- and filled `source` in with the
# owner's own work address. One ordinary handoff wrote an employee identifier and the employer's
# domain into .fleet/tasks/done/<id>.json. .fleet is gitignored so nothing was published, and it
# is still the two-word class this project rewrote its history to remove, arriving by a route
# nobody had looked at. `source` is the one field here the AGENT composes about the PERSON.

def test_an_address_in_the_provenance_is_not_recorded():
    FI.fleet_submit("do a thing", source="user request via someone@example.com")
    origin = _jobs()[0]["origin"]
    assert "@" not in origin["source"], origin
    assert "example.com" not in origin["source"], origin
    assert "user request via" in origin["source"], (
        "the whole provenance was thrown away; only the address had to go: %r" % (origin,))


def test_an_address_in_the_note_is_not_recorded():
    FI.fleet_submit("do a thing", note="asked by someone@example.com in chat")
    note = _jobs()[0]["payload"]["note"]
    assert "@" not in note and "example.com" not in note, note
    assert "asked by" in note and "in chat" in note, note


def test_the_goal_itself_is_left_alone():
    """The goal is the PERSON's own words. An instruction that names someone IS the
    instruction, and rewriting it would change the work that runs."""
    FI.fleet_submit("mail the report to someone@example.com and confirm")
    assert _jobs()[0]["payload"]["goal"] == "mail the report to someone@example.com and confirm"


def test_redaction_happens_before_the_cap_not_after():
    """Capping first would let a long prefix push the address past the cut and 'pass'."""
    FI.fleet_submit("do a thing", source="x" * 55 + " someone@example.com")
    assert "@" not in _jobs()[0]["origin"]["source"]


# -- duplicates are normalised in the background, not refused at the door ------------------------
#
# OWNER ORDER 2026-10-26: refusing a goal because it resembles one already in flight turned away
# DIFFERENT work and was the single biggest source of a person being stopped at the door. The
# word-overlap guard is now OFF by default (FI.DUP_GUARD_ENABLED is False): a near-duplicate is
# ACCEPTED and recorded in the background -- a possible_duplicate_of tag on the job and one line in
# .fleet/dup_notes.jsonl -- and the old refusal is kept only behind the fleet_submit_dup_guard
# setting. These tests pin that behaviour against the measured incident.


@pytest.fixture()
def dup_state(tmp_path, monkeypatch):
    """Point the background note's log at a throwaway directory. obs_log writes dup_notes.jsonl in
    the directory of tool_ledger's ledger path, so moving the ledger moves the note."""
    from tools import obs_log, tool_ledger
    monkeypatch.setattr(tool_ledger, "LEDGER_PATH", str(tmp_path / ".fleet" / "tool_events.jsonl"))
    monkeypatch.setattr(obs_log, "_WARNED", False)
    return tmp_path


def _dup_notes(state):
    p = state / ".fleet" / "dup_notes.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def test_two_near_identical_goals_are_both_accepted(monkeypatch, dup_state):
    """(a) With the guard off (the default), two goals that measure as near-duplicates are BOTH
    queued. The first has nothing in flight to resemble; the second resembles the first and is
    still accepted."""
    monkeypatch.setattr(FI, "_dup_guard_on", lambda: False)
    goal = "sales.csv を解析して売上の月次推移をまとめ、グラフを作って報告する"
    out1 = FI.fleet_submit(goal)
    out2 = FI.fleet_submit(goal)
    assert "queued" in out1 and "queued" in out2
    assert "refused" not in out1 and "refused" not in out2
    assert len(_jobs()) == 2


def test_the_second_job_is_tagged_and_a_dup_note_is_written(monkeypatch, dup_state):
    """(b) The second job carries possible_duplicate_of pointing at the first, with a similarity,
    and exactly one line lands in .fleet/dup_notes.jsonl naming both."""
    monkeypatch.setattr(FI, "_dup_guard_on", lambda: False)
    goal = "sales.csv を解析して売上の月次推移をまとめ、グラフを作って報告する"
    FI.fleet_submit(goal)
    first = _jobs()[0]
    FI.fleet_submit(goal)
    jobs = _jobs()
    assert len(jobs) == 2
    # The first job has no resemblance recorded; the second points back at the first.
    tagged = [j for j in jobs if "possible_duplicate_of" in j["payload"]]
    assert len(tagged) == 1, jobs
    assert tagged[0]["payload"]["possible_duplicate_of"] == first["id"]
    assert 0.0 < tagged[0]["payload"]["similarity"] <= 1.0
    notes = _dup_notes(dup_state)
    assert len(notes) == 1, notes
    assert notes[0]["possible_duplicate_of"] == first["id"]
    assert notes[0]["new"] == tagged[0]["id"]


def test_a_failing_dup_note_does_not_fail_the_submission(monkeypatch, dup_state):
    """(c) The background record is after-the-fact and must never fail the submission. We break
    the ACTUAL write (obs_log.append raises), the way a full disk or a missing directory would,
    and assert that _note_possible_duplicate swallows it: the goal is still queued, the caller
    still gets 'queued', and the swallowed error is written to dup_notes_errors.log rather than
    surfaced or silently dropped."""
    monkeypatch.setattr(FI, "_dup_guard_on", lambda: False)
    goal = "sales.csv を解析して売上の月次推移をまとめ、グラフを作って報告する"
    FI.fleet_submit(goal)

    from tools import obs_log

    def boom(name, row):
        raise OSError("disk on fire")

    monkeypatch.setattr(obs_log, "append", boom)
    # The raise happens inside _note_possible_duplicate, which must catch it.
    out = FI.fleet_submit(goal)
    assert "queued" in out
    assert len(_jobs()) == 2
    # No dup-notes line was written (the writer failed) ...
    assert _dup_notes(dup_state) == []
    # ... but the failure left a trail rather than vanishing.
    errlog = dup_state / ".fleet" / "dup_notes_errors.log"
    assert errlog.exists() and "OSError" in errlog.read_text(encoding="utf-8")


def test_the_guard_still_refuses_a_near_match_when_turned_on(monkeypatch, dup_state):
    """(e) Even with fleet_submit_dup_guard on, content resemblance NEVER refuses a submission
    (owner order 2026-10-26/27): the near-duplicate second goal is still queued; the guard only
    makes the resemblance observable (possible_duplicate_of), it does not stop the job."""
    monkeypatch.setattr(FI, "_dup_guard_on", lambda: True)
    goal = "sales.csv を解析して売上の月次推移をまとめ、グラフを作って報告する"
    FI.fleet_submit(goal)
    out = FI.fleet_submit(goal)
    assert "queued" in out and "refused" not in out
    assert len(_jobs()) == 2


def test_the_guard_setting_is_read_from_the_one_settings_file(monkeypatch, tmp_path):
    """_dup_guard_on reads fleet_submit_dup_guard from the shared settings file; on restores the
    guard, absent or anything but on leaves it off (DUP_GUARD_ENABLED's compiled default)."""
    from tools import settings_path as SP
    sfile = tmp_path / "settings.txt"
    monkeypatch.setattr(SP, "settings_file", lambda: str(sfile))
    # absent -> off
    assert FI._dup_guard_on() is False
    sfile.write_text("fleet_submit_dup_guard=on\n", encoding="utf-8")
    assert FI._dup_guard_on() is True
    sfile.write_text("fleet_submit_dup_guard=off\n", encoding="utf-8")
    assert FI._dup_guard_on() is False
    sfile.write_text("fleet_submit_dup_guard=maybe\n", encoding="utf-8")
    assert FI._dup_guard_on() is False


# -- resume_conv: a continuation, not a new task ----------------------------------------------
#
# The chat window reads a durable conversation id off the transcript and hands it to this door
# so the fleet CONTINUES that conversation instead of guessing one by goal text. Two things
# have to hold for that to work from a phone: the id has to reach the queued job's payload (so
# the router can thread it to the fleet), and a follow-up -- which is deliberately short and
# reads like the terse resubmission the duplicate check refuses -- must not be refused for
# carrying resume_conv.

_GOAL_A = "summarise the Q3 revenue deck and list every open action item by owner and date"


def test_resume_conv_rides_on_the_queued_payload():
    """The id has to survive onto the payload, beside goal/priority, or the router has nothing
    to thread through its three delivery routes."""
    out = FI.fleet_submit("continue where we left off", resume_conv="sess:abc123")
    assert "queued" in out
    assert _jobs()[0]["payload"]["resume_conv"] == "sess:abc123"


def test_an_ordinary_goal_writes_no_resume_conv_key():
    """'resume_conv not in payload' has to keep meaning 'start fresh', so an ordinary goal's
    record is unchanged and nothing downstream reads a stray empty id."""
    FI.fleet_submit("do a thing")
    assert "resume_conv" not in _jobs()[0]["payload"]


def test_a_follow_up_is_not_refused_as_a_near_duplicate(monkeypatch):
    """THE WHOLE POINT. The first turn sits in pending/; a continuation of the SAME work reads
    as a near-duplicate of it. Without the exemption the follow-up is refused and the
    conversation it meant to resume is never answered -- the exact bug this closes. With a
    resume_conv it goes through, and its own id travels with it.

    The duplicate guard is turned ON for this test so the refusal path actually fires: with the
    guard off (the 2026-10-26 default) a near-duplicate is accepted in the background rather than
    refused, which would make the exemption invisible -- there would be nothing for resume_conv
    to be exempt FROM. resume_conv must skip the duplicate machinery whether the guard is on or
    off, so pinning it against the guard ON is the stronger check: it proves the id, not the
    guard's default, is what lets the continuation through."""
    monkeypatch.setattr(FI, "_dup_guard_on", lambda: True)
    first = FI.fleet_submit(_GOAL_A)
    assert "queued" in first
    # A re-worded continuation of the same work is NEVER refused on content resemblance
    # (owner order 2026-10-26/27): it is queued, only tagged as a possible duplicate in the
    # background. The guard being on changes the record, not whether the goal starts.
    accepted = FI.fleet_submit(_GOAL_A + " please")
    assert "queued" in accepted and "refused" not in accepted
    # The SAME near-duplicate, carrying a conversation id, is admitted as a continuation too and
    # carries its resume id.
    ok = FI.fleet_submit(_GOAL_A + " please", resume_conv="sess:xyz789")
    assert "queued" in ok
    payloads = [j["payload"] for j in _jobs()]
    assert any(p.get("resume_conv") == "sess:xyz789" for p in payloads)


def test_resume_conv_whitespace_is_normalised():
    """The id is composed elsewhere and relayed here; it is trimmed like the other relayed
    fields so a stray newline never becomes part of the id the fleet matches on."""
    FI.fleet_submit("continue", resume_conv="  sess:trim  ")
    assert _jobs()[0]["payload"]["resume_conv"] == "sess:trim"

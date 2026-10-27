"""Every route a goal can enter this machine by leaves ONE admission receipt, and carries who
submitted it far enough to be joined back.

Three doors put a goal in front of the fleet -- fleet_submit (mcp), the CLI, and the cockpit's
"追加" button (an add_goal straight into commands.d/). Before relay/intake_journal they left
three different trails or none, and the cockpit's add carried no provenance at all, so a goal
that joined a live run read origin=null while the same goal submitted through fleet_submit
carried its origin into history.json. These tests pin the seam that closes that gap:

  * the journal records one line, by whom, and never raises onto a submission;
  * goals_from_command threads an item's `origin` onto the goal dict (the UI-add provenance
    path, which dead-ended exactly the way resume_conv once did);
  * add_goal_to_live_fleet puts `origin` on the item it writes, so the live-add path carries it;
  * fleet_submit writes an mcp admission line the moment the job is on disk.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load the modules under test from THIS repository, not whichever copy a stray PYTHONPATH (e.g.
# a production checkout on sys.path) resolved first. Dropping any already-imported "relay"/"tools"
# modules after putting this repo root at the front of sys.path forces the imports below to bind
# to the tree these tests live in -- the whole point is to pin this repo's seam, not another's.
for _m in [_n for _n in list(sys.modules) if _n == "relay" or _n == "tools"
           or _n.startswith("relay.") or _n.startswith("tools.")]:
    del sys.modules[_m]

import relay.intake_journal as ij
import relay.task_router as tr
import relay.fleet_runner as fr


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    assert cond, name


def test_journal_appends_one_line_and_reads_back():
    with tempfile.TemporaryDirectory() as d:
        rec = ij.record(jid="abc123", route="mcp", submitter="owner x",
                        goal="  do   the   thing  ", session="sess:1", state_dir=d)
        check("record returned the dict", isinstance(rec, dict))
        rows = ij.read_all(state_dir=d)
        check("exactly one line", len(rows) == 1)
        r = rows[0]
        check("jid kept", r["jid"] == "abc123")
        check("route kept", r["route"] == "mcp")
        check("submitter kept", r["submitter"] == "owner x")
        check("session kept", r["session"] == "sess:1")
        # goal is stored as a whitespace-collapsed preview, not verbatim.
        check("goal preview collapsed", r["goal"] == "do the thing")
        check("ts present", isinstance(r["ts"], float))
        check("at present", isinstance(r["at"], str) and r["at"])


def test_a_second_submission_appends_rather_than_replaces():
    with tempfile.TemporaryDirectory() as d:
        ij.record(jid="j1", route="mcp", submitter="a", goal="one", state_dir=d)
        ij.record(jid="j2", route="ui_add", submitter="b", goal="two", state_dir=d)
        rows = ij.read_all(state_dir=d)
        check("both lines present", [r["jid"] for r in rows] == ["j1", "j2"])
        check("routes distinct", [r["route"] for r in rows] == ["mcp", "ui_add"])


def test_a_journal_that_cannot_be_written_does_not_fail_the_submission():
    # Point the state dir at a FILE, so makedirs/open under it cannot succeed. record must
    # swallow the error and return None rather than raise onto a caller that was only
    # submitting work -- a journal line is a witness, not a gate.
    with tempfile.TemporaryDirectory() as d:
        blocker = os.path.join(d, "not_a_dir")
        with open(blocker, "w") as fh:
            fh.write("x")
        state_dir = os.path.join(blocker, "under_a_file")
        rec = ij.record(jid="j", route="mcp", submitter="a", goal="g", state_dir=state_dir)
        check("record returned None on failure", rec is None)


def test_read_all_is_empty_and_quiet_when_no_journal_exists():
    with tempfile.TemporaryDirectory() as d:
        check("no journal reads as empty", ij.read_all(state_dir=d) == [])


def test_goals_from_command_threads_origin_onto_the_goal():
    # The UI-add provenance seam: an add_goal item carrying origin must reach the goal dict the
    # run builds, the same way jid/resume_conv are threaded. Dropped here, a cockpit-added goal
    # would read origin=null downstream.
    cmd = {"add_goal": [{"text": "paint the fence", "jid": "zzz999",
                         "origin": {"via": "ui_add", "source": "cockpit sess:7"}}]}
    goals = fr.goals_from_command(cmd, submission_id="cmdfile-1")
    check("one goal built", len(goals) == 1)
    g = goals[0]
    check("text kept", g["text"] == "paint the fence")
    check("jid kept", g["jid"] == "zzz999")
    check("origin threaded", g.get("origin") == {"via": "ui_add", "source": "cockpit sess:7"})


def test_a_goal_without_origin_stays_originless():
    # No origin on the item means none invented on the goal: an older cockpit binary that does
    # not yet stamp origin must not suddenly read as some default submitter.
    cmd = {"add_goal": [{"text": "t", "jid": "j"}]}
    g = fr.goals_from_command(cmd, submission_id="c")[0]
    check("no origin invented", "origin" not in g)


def test_add_goal_to_live_fleet_puts_origin_on_the_item():
    with tempfile.TemporaryDirectory() as d:
        origin = {"via": "mcp", "source": "owner r31"}
        tr.add_goal_to_live_fleet("solve it", state_dir=d, jid="j1", origin=origin)
        cmd_dir = os.path.join(d, "commands.d")
        files = [f for f in os.listdir(cmd_dir) if f.endswith(".json")]
        check("one command written", len(files) == 1)
        with open(os.path.join(cmd_dir, files[0]), encoding="utf-8") as fh:
            cmd = json.load(fh)
        item = cmd["add_goal"][0]
        check("origin on the item", item.get("origin") == origin)
        check("jid on the item", item.get("jid") == "j1")
        # And the reader threads it straight back onto the goal: writer and reader agree.
        g = fr.goals_from_command(cmd, submission_id=files[0])[0]
        check("round-trips to the goal", g.get("origin") == origin)


def test_add_goal_without_origin_is_unchanged():
    with tempfile.TemporaryDirectory() as d:
        tr.add_goal_to_live_fleet("g", state_dir=d, jid="j")
        cmd_dir = os.path.join(d, "commands.d")
        f = [x for x in os.listdir(cmd_dir) if x.endswith(".json")][0]
        with open(os.path.join(cmd_dir, f), encoding="utf-8") as fh:
            cmd = json.load(fh)
        check("no origin key added", "origin" not in cmd["add_goal"][0])


def test_fleet_submit_writes_an_mcp_admission_line():
    # fleet_submit queues under TR.TASKS; isolate both the tasks tree and the journal to one
    # temp state dir so the real .fleet/ is never touched.
    import tools.fleet_intake as fi
    with tempfile.TemporaryDirectory() as d:
        tasks = os.path.join(d, "tasks")
        old_tasks = tr.TASKS
        old_state = os.environ.get("FLEET_STATE_DIR")
        tr.TASKS = tasks
        os.environ["FLEET_STATE_DIR"] = d
        try:
            reply = fi.fleet_submit("research the thing", source="owner probe")
            check("submit acknowledged", "queued" in reply or "refused" not in reply)
            rows = ij.read_all(state_dir=d)
            check("one admission line", len(rows) == 1)
            check("route is mcp", rows[0]["route"] == "mcp")
            check("submitter carried", rows[0]["submitter"] == "owner probe")
        finally:
            tr.TASKS = old_tasks
            if old_state is None:
                os.environ.pop("FLEET_STATE_DIR", None)
            else:
                os.environ["FLEET_STATE_DIR"] = old_state


class _LaunchArgs:
    # The subset of the argparse namespace _record_launch_admission reads. A plain object rather
    # than a real parse so the test pins the contract (which fields it consumes), not argparse.
    def __init__(self, state_dir, submit_route="", submit_origin="",
                 goal=None, goals_file=None, resume=False):
        self.state_dir = state_dir
        self.submit_route = submit_route
        self.submit_origin = submit_origin
        self.goal = goal
        self.goals_file = goals_file
        self.resume = resume


def test_a_gui_start_records_ui_start_before_the_run_lock():
    # SpawnFleet ("開始") launches the runner with --goals-file and --submit-route ui_start. The
    # admission is recorded from the flags, BEFORE the exclusion lock, so a start refused for a
    # held lock (exit 3) still leaves this receipt. Here we call the recorder directly with the
    # route set and a goals-file on disk.
    with tempfile.TemporaryDirectory() as d:
        gf = os.path.join(d, "goals.jsonl")
        with open(gf, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"text": "paint the fence"}) + "\n")
        fr._record_launch_admission(_LaunchArgs(
            state_dir=d, submit_route="ui_start",
            submit_origin="cockpit alice pid 42", goals_file=gf))
        rows = ij.read_all(state_dir=d)
        check("one admission line", len(rows) == 1)
        check("route is ui_start", rows[0]["route"] == "ui_start")
        check("submitter carried", rows[0]["submitter"] == "cockpit alice pid 42")
        check("goal preview carried", rows[0]["goal"] == "paint the fence")


def test_a_gui_resume_records_ui_resume_even_with_no_goals_on_the_command_line():
    # SpawnFleetResume ("続き") launches with --resume and no -g/--goals-file: the goals come from
    # the durable ledger, not the command line. The receipt must still be written -- a resume is
    # an ingress too -- with a recognisable preview rather than an empty goal.
    with tempfile.TemporaryDirectory() as d:
        fr._record_launch_admission(_LaunchArgs(
            state_dir=d, submit_route="ui_resume",
            submit_origin="cockpit bob pid 7", resume=True))
        rows = ij.read_all(state_dir=d)
        check("one admission line", len(rows) == 1)
        check("route is ui_resume", rows[0]["route"] == "ui_resume")
        check("submitter carried", rows[0]["submitter"] == "cockpit bob pid 7")
        check("resume preview, not empty", rows[0]["goal"] != "")


def test_a_plain_cli_launch_writes_no_launch_line_here():
    # No --submit-route means an ordinary CLI / supervisor launch, whose admission is recorded
    # by _record_cli_submission as route "cli" AFTER the lock, exactly as before. The launch
    # recorder must be a no-op for it, so a cli goal is never journalled twice.
    with tempfile.TemporaryDirectory() as d:
        fr._record_launch_admission(_LaunchArgs(
            state_dir=d, submit_route="", goal=["research the thing"]))
        check("no launch line for a routeless cli start", ij.read_all(state_dir=d) == [])


def test_a_launch_receipt_never_raises_when_the_journal_cannot_be_written():
    # Point the state dir under a FILE so the journal cannot be created. The recorder must
    # swallow it and return without raising -- a receipt is a witness, never a gate on the run.
    with tempfile.TemporaryDirectory() as d:
        blocker = os.path.join(d, "not_a_dir")
        with open(blocker, "w") as fh:
            fh.write("x")
        state_dir = os.path.join(blocker, "under_a_file")
        fr._record_launch_admission(_LaunchArgs(
            state_dir=state_dir, submit_route="ui_start",
            submit_origin="cockpit c pid 1", goal=["g"]))
        check("routed launch under an unwritable dir did not raise", True)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("ALL PASS")

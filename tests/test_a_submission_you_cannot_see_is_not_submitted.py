# -*- coding: utf-8 -*-
"""A job on the queue must be on the screen, and the contract that makes that a defect.

THE FAILURE. A job submitted with fleet_submit lands in .fleet/tasks/pending/<id>.json and has
no worker until a coordinator starts. The cockpit reads .fleet/status.json -- the runner's
snapshot of WORKERS -- and nothing else, so for the whole gap between "on disk" and "a worker
exists" the queue was invisible and EmptyState() rendered "タスクはまだありません". That is not
a slow refresh. It is the panel asserting there are no tasks while the queue holds work.

WHY IT MATTERED MORE THAN A COSMETIC GAP. The standing rule here is that work which cannot be
confirmed in the GUI does not count as working. With the queue invisible, a submission could
not be confirmed at all -- so on 2026-09-18 a turn was verified by reading sessions.sqlite3 and
reported as end-to-end proof. It was proof that a row exists, of a different path than the one
asked about. The display defect produced the verification defect.

WHAT IS PINNED HERE:
  * the cockpit reads the task queue, not only status.json;
  * the empty state stops claiming there are no tasks when there are;
  * the panel PUBLISHES the queue it is showing, so the claim is checkable without asking
    anyone to describe their screen -- and so display and truth can be compared;
  * status.py prints that report;
  * docs/agent_contract.md states the contract, for agent-facing paths too.

MEASURED after the change: submitted at 11:55:17, the panel reported it at 11:55:19 with its
id and an age of 2 s, and the fleet then ran the job and its goal file appeared on disk.
"""
from __future__ import annotations

import io
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COCKPIT = os.path.join(REPO, "ui", "FleetCockpit.cs")
STATUS = os.path.join(REPO, "scripts", "status.py")
CONTRACT = os.path.join(REPO, "docs", "agent_contract.md")


def _read(path):
    return io.open(path, encoding="utf-8", errors="replace").read()


def _cs_code():
    """The cockpit's source with // comment lines dropped.

    Asserted on the source because building and driving a WPF window is not something this
    suite does, and stated as such rather than dressed up. The BEHAVIOUR is pinned by the
    published strip below, which is the panel's own report of what it rendered.

    Comments are stripped because the same substring-versus-prose false positive happened three
    times in one day elsewhere: a comment explaining a mistake must not be indistinguishable
    from the mistake."""
    return "\n".join(l for l in _read(COCKPIT).splitlines()
                     if not l.lstrip().startswith("//"))


def test_the_cockpit_reads_the_task_queue_and_not_only_the_worker_snapshot():
    code = _cs_code()
    assert "ReadQueuedJobs" in code, "the cockpit is back to reading only status.json"
    assert '"pending"' in code and '"for_fleet"' in code, \
        "both queue states must be read: unclaimed, and handed off but unconfirmed"


def test_the_empty_state_no_longer_claims_there_are_no_tasks_when_there_are():
    """THE FALSE STATEMENT, and it must be unreachable while the queue holds work.

    Checked inside EmptyState() only: `ReadQueuedJobs` also appears in PublishHealthStrip, so a
    window measured in characters around the message can be satisfied by the wrong caller. The
    structure is what matters -- the message sits in the else of a branch on the queue."""
    code = _cs_code()
    body = code[code.index("UIElement EmptyState()"):]
    body = body[:body.index("\n    UIElement ", 10)] if "\n    UIElement " in body[10:] else body
    assert "ReadQueuedJobs()" in body, \
        "EmptyState renders without consulting the queue"
    i = body.index("タスクはまだありません")
    guard = body[:i]
    assert "queued.Count > 0" in guard, "the message is not behind a queue-is-empty branch"
    assert guard.rstrip().endswith("{") or "else" in guard[-200:], \
        "the no-tasks message is not in the else of that branch"
    # And the true statement must be there for the other case.
    assert "投入済み" in body, "a queued job is not announced at all"


def test_the_panel_publishes_the_queue_it_is_showing():
    """So the claim is checkable. The dots are published for the same reason, added the day
    before for the same complaint -- that a person had to read a tooltip aloud."""
    code = _cs_code()
    assert "queued_count" in code
    assert "goal_head" in code, "an id alone does not let a reader recognise the job"
    # It must publish what IT rendered, from the same call the display uses.
    pub = code[code.index("void PublishHealthStrip"):]
    assert "ReadQueuedJobs()" in pub[:4000], \
        "the strip is reporting something other than what the panel read"


def test_status_py_prints_the_panels_queue_report():
    """An instrument without a reader is half an instrument -- the rule this repository applied
    to the rejection record the day before."""
    src = _read(STATUS)
    assert "queued_count" in src
    assert "not published -- rebuild ui/ to get it" in src, \
        "an older cockpit binary must be reported as silent, not as zero"


def test_the_contract_is_written_down_and_says_it_covers_agents():
    """The rule existed as a habit and was violated by an agent path the same day it was
    invoked. A contract nobody can cite is not a contract."""
    doc = _read(CONTRACT)
    assert "cannot be confirmed in the GUI" in doc
    assert re.search(r"agent-facing paths exactly as it applies", doc), \
        "the contract does not say it binds agent paths too"
    # The three consequences that were each violated.
    assert "not a verification" in doc, "CLI verification must be named as insufficient"
    assert "is not an answer" in doc, "'it will appear shortly' must be named as insufficient"


def test_the_contract_names_both_routes_to_the_fleet():
    """THE OTHER CONFIRMED DEFECT. The instructions said fleet_submit needs no gateway while
    RULE 4 said every tool is behind one, so an agent that could not see the tool concluded
    there was no route."""
    doc = _read(CONTRACT)
    assert "fleet_submit(goal=" in doc
    assert 'call_tool(name="fleet_submit"' in doc, "the gateway route is missing"
    assert "how to tell which" in doc.lower(), \
        "an agent still has to guess which route it has"


def test_the_server_instructions_no_longer_say_no_gateway():
    """The contradiction, in the text the agent ACTUALLY RECEIVES.

    Checked on the composed string rather than on main.py's source, because the source splits
    it across adjacent literals -- "TWO " + "ROUTES REACH IT: ..." -- so a search of the file
    fails on text that is present in the instructions. What matters is what reaches the agent,
    and the existing budget test reads it the same way."""
    import ast

    tree = ast.parse(_read(os.path.join(REPO, "main.py")))
    text = None
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "instructions":
            text = ast.literal_eval(node.value)
            break
    assert text, "main.py no longer passes instructions= to the server"

    assert "it needs no unlock and no gateway" not in text, \
        "the instructions still tell an agent fleet_submit bypasses the gateway"
    assert "TWO ROUTES REACH IT" in text
    assert "call_tool(name='fleet_submit'" in text, "the gateway route is not spelled out"
    assert "docs/agent_contract.md" in text, "the instructions do not point at the contract"
    assert "QUEUED, not started" in text, "a job id must not read as a landing"


# ---- the recorded CLI route ---------------------------------------------------------------
#
# Launching relay/fleet_runner.py -g "..." is not submitting: the runner writes nothing until
# the run is under way, so a CLI submission was invisible and an early death left no trace at
# all. scripts/submit_goal.py goes through fleet_submit, which writes the queue entry before
# anything can refuse the job -- measured on screen two seconds after the command returned.


def test_the_cli_front_door_goes_through_fleet_submit():
    """THE POINT OF IT. Anything that builds its own queue entry, or starts a runner, is back
    to the route that could not be seen."""
    src = _read(os.path.join(REPO, "scripts", "submit_goal.py"))
    assert "from tools.fleet_intake import fleet_submit" in src
    assert "fleet_runner" not in src.split('"""', 2)[-1], \
        "the front door is starting a runner instead of queueing"


def test_the_front_door_does_not_split_an_instruction_into_words():
    """A shell that splits an unquoted instruction would otherwise queue one job per word, and
    fleet_submit's duplicate guard cannot catch it because the fragments differ."""
    import scripts.submit_goal as SG

    calls = []
    real = None
    try:
        import tools.fleet_intake as FI
        real = FI.fleet_submit

        def _fake(goal="", note="", source=""):
            calls.append(goal)
            return "queued deadbeef -- fake"

        FI.fleet_submit = _fake
        rc = SG.main(["one", "two", "three"])
    finally:
        if real is not None:
            import tools.fleet_intake as FI
            FI.fleet_submit = real
    assert rc == 0, rc
    assert calls == ["one two three"], calls


def test_the_front_door_reports_a_refusal_as_a_failure():
    """fleet_submit refuses by RETURNING a string, not by raising. Exiting 0 on a refusal is
    how a caller concludes the job is queued when the queue said no."""
    import scripts.submit_goal as SG

    real = None
    try:
        import tools.fleet_intake as FI
        real = FI.fleet_submit
        FI.fleet_submit = lambda goal="", note="", source="": \
            "[fleet_submit: refused -- this reads as the goal already queued]"
        rc = SG.main(["do the thing"])
    finally:
        if real is not None:
            import tools.fleet_intake as FI
            FI.fleet_submit = real
    assert rc == 1, "a refusal exited %r" % rc


def test_the_front_door_fills_in_a_source_when_none_is_given():
    """origin.source is the only record of where an instruction came from, and an empty one is
    the answer nobody can act on."""
    import scripts.submit_goal as SG

    seen = {}
    real = None
    try:
        import tools.fleet_intake as FI
        real = FI.fleet_submit

        def _fake(goal="", note="", source=""):
            seen["source"] = source
            return "queued deadbeef -- fake"

        FI.fleet_submit = _fake
        SG.main(["do the thing"])
    finally:
        if real is not None:
            import tools.fleet_intake as FI
            FI.fleet_submit = real
    assert seen.get("source"), "no source was passed"
    assert seen["source"].startswith("cli"), seen["source"]


def test_the_contract_names_the_front_door_and_warns_off_the_runner():
    doc = _read(CONTRACT)
    assert "scripts/submit_goal.py" in doc
    assert "is not submitting" in doc, "the contract does not say what the runner is not"
    # And the freshness caveat, so an absence in the published strip is not read as an absence
    # on screen.
    assert "fresher than the published report" in doc

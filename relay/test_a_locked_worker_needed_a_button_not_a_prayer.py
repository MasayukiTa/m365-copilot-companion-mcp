# -*- coding: utf-8 -*-
"""Twice in one day, a worker was refused for lock, no recovery fired, and the run carried on
regardless -- once producing a deliverable that claimed to have verified content it had never
been able to read.

AUTOMATIC RECOVERY ALREADY EXISTS. `relay.relay_fleet._initial_job_with_unlock` injects
`UNLOCK_PREFIX % password` into a worker's very first turn whenever a local password is found,
and a separate heuristic elsewhere retries it when a reply LOOKS like a lock refusal. Both can
miss: the heuristic is deliberately loose and gated on a matching record, and neither runs at
all for a refusal that arrives later and is never recognised as one. When that happens the
operator could watch a run die on authorisation with no button to press.

THIS IS THAT BUTTON. `{"reunlock": "w0"}` (a worker name, or "" / "*" for every live worker)
written to the cockpit's command file re-delivers the unlock turn ON DEMAND, and
`relay.fleet_runner.apply_reunlock` is the function that does it. It is deliberately built on
`deliver_steers` rather than a hand-rolled dispatch: that function already carries the rule
that every rejection must be NAMED, not swallowed (see tests/test_steer_delivery.py and its own
docstring, which records a steer that was silently dropped and reported as delivered anyway).

THE PASSWORD MUST NEVER TRAVEL THROUGH THE COMMAND FILE. `.fleet/commands.d/*.json` (and the
legacy `commands.json` the cockpit still writes) is plain text on disk, read by several
processes. The cockpit only ever writes the WORD "reunlock" and a worker name; the password is
read LOCALLY, on this machine, from this machine's `.env`, by the coordinator that already has
it, and is injected into exactly one transient turn -- never written back to any file this
suite can see. `test_the_command_file_never_carries_the_password_even_though_the_delivered_turn_does`
asserts that on the FILE'S BYTES, not on intent.

relay/relay_fleet.py and relay/copilot_autopilot_relay.py are contested (two other agents
editing them concurrently) -- everything here imports from relay_fleet rather than duplicating
its UNLOCK_PREFIX text or its password reader, which is also what apply_reunlock itself does.
"""
import json
import os

import relay.relay_fleet as relay_fleet
from relay.fleet_runner import apply_reunlock, read_commands, _snapshot
from relay.relay_fleet import UNLOCK_PREFIX

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet_runner.py")

#: A password-shaped string that is NOT a real secret and never touches an actual .env --
#: used only so the tests can prove it did or did not end up somewhere it should not.
FAKE_PW = "not-a-real-secret-9f3c7a"


class _W:
    """A worker as deliver_steers (and therefore apply_reunlock) reads one -- the same
    minimal shape tests/test_steer_delivery.py uses for the function this one is built on."""

    def __init__(self, name, status):
        self.name = name
        self.status = status
        self.msgs = []

    def steer(self, text):
        self.msgs.append(text)


def _fake_pw(monkeypatch, value=FAKE_PW):
    monkeypatch.setattr(relay_fleet, "_unlock_password", lambda: value)


# ---- the password never touches the command file, the turn it builds does ------------------

def test_the_command_file_never_carries_the_password_even_though_the_delivered_turn_does(
        tmp_path, monkeypatch):
    """Simulates the whole path: the cockpit writes ONLY {"reunlock": "w0"} to the command
    file (no secret -- there is nothing in FleetCockpit.cs that could put one there), the
    coordinator reads it with the same read_commands() the fleet loop polls with, and only
    THEN does apply_reunlock read the password locally and build the turn."""
    state_dir = tmp_path
    cmd_path = state_dir / "commands.json"
    cmd_path.write_text(json.dumps({"reunlock": "w0"}), encoding="utf-8")
    raw_before = cmd_path.read_bytes()
    assert FAKE_PW.encode("utf-8") not in raw_before, (
        "sanity check on the fixture itself: the file the cockpit 'wrote' must not carry it")

    _fake_pw(monkeypatch)

    cmds = read_commands(str(state_dir))
    assert cmds == [{"reunlock": "w0"}]
    assert not cmd_path.exists(), "read_commands must consume the legacy file"

    workers = [_W("w0", "waiting")]
    result = apply_reunlock(cmds[0]["reunlock"], workers, enqueue=lambda g: None)

    assert result["ok"] is True
    assert result["delivered"] == 1
    assert workers[0].msgs, "the worker was never actually given a turn"
    assert workers[0].msgs[0] == UNLOCK_PREFIX % FAKE_PW, (
        "the delivered turn must be the real unlock instruction, verbatim")

    # THE ASSERTION THAT MATTERS: sweep every byte under state_dir, not just the one file
    # this test wrote -- a regression that echoed the turn text back into an ack file, a
    # .bad rename, or anything else under commands.d would be invisible to a narrower check.
    for root, _dirs, files in os.walk(state_dir):
        for fn in files:
            data = open(os.path.join(root, fn), "rb").read()
            assert FAKE_PW.encode("utf-8") not in data, (
                "the password leaked onto disk at %s" % os.path.join(root, fn))

    # And the receipt meant for status.json must not carry it either.
    assert FAKE_PW not in json.dumps(result)


def test_a_broadcast_target_reaches_every_live_worker_and_only_those(monkeypatch):
    """"" and "*" both mean every live worker, mirroring deliver_steers' own broadcast rule
    (empty name) plus the wildcard a person reaches for first when typing a command by hand."""
    _fake_pw(monkeypatch)
    workers = [_W("w0", "done"), _W("w1", "waiting"), _W("w2", "refuting"), _W("w3", "pending")]
    result = apply_reunlock("*", workers, enqueue=lambda g: None)
    assert result["ok"] is True
    assert result["delivered"] == 2
    assert {w.name for w in workers if w.msgs} == {"w1", "w2"}

    result2 = apply_reunlock("", [_W("w5", "waiting")], enqueue=lambda g: None)
    assert result2["ok"] is True
    assert result2["target"] == "*"


# ---- with no local password, the operator gets a stated reason, not silence ------------------

def test_no_local_password_gives_a_reason_instead_of_silence(monkeypatch):
    """'I pressed the button and nothing happened' is exactly the failure this whole feature
    exists to remove. A missing password must be REPORTED, and nothing may be delivered."""
    import tools.secret_store as secret_store

    monkeypatch.setattr(relay_fleet, "_unlock_password", lambda: "")
    monkeypatch.setattr(secret_store, "unlock_password_problem",
                        lambda: secret_store.PROBLEM_UNSET)
    said = []
    workers = [_W("w0", "waiting")]

    result = apply_reunlock("w0", workers, log=said.append, enqueue=lambda g: None)

    assert result["ok"] is False
    assert result["delivered"] == 0
    assert result["reason"], "a blank reason is the silence this exists to remove"
    assert "password" in result["reason"].lower()
    assert workers[0].msgs == [], "nothing may be delivered without a password"
    assert any("REFUSED" in m for m in said), said


# ---- an unknown or dead worker name is reported, not swallowed -------------------------------

def test_an_unknown_worker_name_is_reported_not_swallowed(monkeypatch):
    _fake_pw(monkeypatch)
    said = []
    workers = [_W("w1", "waiting")]

    result = apply_reunlock("w99", workers, log=said.append, enqueue=lambda g: None)

    assert result["ok"] is False
    assert result["delivered"] == 0
    assert any("DROPPED" in m and "w99" in m for m in said), said
    assert "w99" in result["reason"] or "DROPPED" in result["reason"]
    assert workers[0].msgs == []


def test_a_worker_that_has_already_finished_is_reported_not_swallowed(monkeypatch):
    _fake_pw(monkeypatch)
    said = []
    # No `goal` attribute (matching _W's minimal shape), so a follow-up cannot be built and
    # deliver_steers falls through to its "already <status>" drop -- which is the report that
    # matters here: a re-unlock aimed at a worker that no longer exists must say so.
    workers = [_W("w0", "done")]

    result = apply_reunlock("w0", workers, log=said.append, enqueue=lambda g: None)

    assert result["ok"] is False
    assert result["delivered"] == 0
    assert any("DROPPED" in m and "already" in m for m in said), said


# ---- the receipt reaches status.json -----------------------------------------------------

def test_the_snapshot_carries_the_reunlock_receipt():
    """The button's whole point is that the operator can SEE what happened. _snapshot is what
    status.json is built from, so the receipt has to survive that trip unchanged."""

    class _SW:
        """Same minimal shape tests/test_outcome_reporting.py's _W uses for _snapshot --
        only the fields it actually reads are needed."""

        def __init__(self):
            self.status = "waiting"
            self.name = "w0"
            self.goal = "g"
            self.outcome = ""
            self.reason = ""
            self.turn = 1
            self.max_turns = 10
            self.page = None
            self.checks = []
            self.cwd = ""
            self.last_response = ""
            self.conv_url = ""
            self.conv_title = ""
            self.transcript = ""
            self.verified = False
            self.verify_attempts = 0
            self.closed = False
            self.phase_events = []
            self.subtask_index = None
            self.task_envelope = None
            self.plan_steps = []
            self.next_step = ""
            self.self_confidence = ""
            self.steer_msgs = []
            self.eval_busy_until = 0.0
            self.display_result = ""

        def tab_load(self):
            return 0

    receipt = {"ts": 1.0, "target": "w0", "ok": True, "delivered": 1,
              "reason": "queued for w0 (waiting) -- takes effect on its next turn"}
    snap = _snapshot([_SW()], 0.0, 1, reunlock=receipt)
    assert snap["reunlock"] == receipt

    snap_default = _snapshot([_SW()], 0.0, 1)
    assert snap_default["reunlock"] is None, (
        "a run that has never pressed the button must show that plainly, not omit the key")


# ---- the wiring exists, and does not duplicate what it must import ----------------------------

def test_the_command_is_wired_through_apply_reunlock():
    src = open(RUNNER, encoding="utf-8").read()
    assert 'if "reunlock" in cmd:' in src
    assert 'apply_reunlock(cmd.get("reunlock")' in src
    assert "reunlock_box[0] = apply_reunlock" in src, (
        "the outcome must be kept for status.json, not just printed to the console")


def test_apply_reunlock_imports_the_prefix_rather_than_copying_it():
    """relay/relay_fleet.py is contested (two other agents editing it). Nothing here may
    duplicate UNLOCK_PREFIX's text -- a copy would silently drift from the two-factor wording
    that module's own history records paying to get right once already."""
    src = open(RUNNER, encoding="utf-8").read()
    assert "UNLOCK_PREFIX =" not in src, "UNLOCK_PREFIX must be owned by relay_fleet.py alone"
    assert "from relay.relay_fleet import UNLOCK_PREFIX" in src

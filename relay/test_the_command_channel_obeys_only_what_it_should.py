# -*- coding: utf-8 -*-
"""The fleet's command channel obeys what the operator's tools can send, and nothing else (SEC-08).

WHAT WAS WRONG.
  * relay/fleet_runner.read_commands took the `ack` path a command file named and did
    os.makedirs() + open(ack, "w") on it: anything able to drop a JSON file into
    <state>/commands.d/ could create directories and overwrite files anywhere this account can
    write -- outside a folder the MCP file tools were scoped to, too.
  * _apply_command coerced each field where it used it and swallowed what failed: a disk floor of
    1e9 GB, a negative RAM floor, NaN, a steer of any size, an unknown key -- applied, half
    applied or dropped, with nothing recorded.
  * tools/file_ops let write_file put a file in commands.d/ at all.

WHAT THESE TESTS HOLD, at runtime, through the real reader and writers:
  * the receipt lands only at <state>/acks/<id>.ack, never where the file says, never through a
    link planted at that name or a junction put in place of acks/;
  * every command is validated whole against the settings panel's own bounds; a refusal is
    recorded (status.json `command_rejections`, the console, and the receipt) and names keys and
    limits, never the text a field carried;
  * every shape the real writers send is still admitted -- a validator that refuses the cockpit
    is a broken cockpit;
  * the MCP file tools refuse the channel.
"""
import json
import math
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import fleet_runner as FR  # noqa: E402
from relay import task_router as TR  # noqa: E402

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet_runner.py")


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(tmp_path / "fleet"))
    os.makedirs(str(tmp_path / "fleet"), exist_ok=True)
    TR.ensure_dirs()
    return tmp_path / "fleet"


def _drop(state, cmd, name="0000000000000000001-000000001-aaaaaaaa.json"):
    d = os.path.join(str(state), FR.COMMANDS_DIR)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
        json.dump(cmd, fh)


def _admit(cmd, state):
    box, said = [], []
    ok = FR.admit_command(cmd, str(state), box, log=said.append)
    return ok, box, said


# ======================================================================= (a) the ack path

def test_an_ack_outside_the_state_dir_writes_nothing_there(state, tmp_path):
    outside = tmp_path / "elsewhere" / "deep" / "victim.ack"
    cmd = {"add_goal": [{"text": "hello"}], "ack": str(outside)}
    _drop(state, cmd)
    got = FR.read_commands(str(state))
    assert got == [cmd], "the reader still hands every parsed command to the gate"
    assert not (tmp_path / "elsewhere").exists(), (
        "the reader created directories where the command file told it to")
    assert not os.path.exists(os.path.join(str(state), FR.ACKS_DIR, "victim.ack")), (
        "an ack naming another directory must be refused, not quietly redirected")
    ok, box, _ = _admit(cmd, state)
    assert not ok and "ack" in json.dumps(box)


def test_an_existing_file_named_by_ack_is_not_overwritten(state, tmp_path):
    victim = tmp_path / "settings.txt"
    victim.write_text("disk_floor_gb=6\n", encoding="utf-8")
    _drop(state, {"pause": True, "ack": str(tmp_path / "settings.txt")})
    FR.read_commands(str(state))
    assert victim.read_text(encoding="utf-8") == "disk_floor_gb=6\n"


@pytest.mark.parametrize("claimed", [
    "{acks}/../escape.ack",
    "{acks}/../../escape.ack",
    "{acks}/sub/x.ack",
    "{acks}/CON.ack",
    "{acks}/.hidden.ack",
    "{acks}/x.json",
    "{acks}/",
    "x.ack",
])
def test_an_ack_that_is_not_a_plain_name_in_acks_is_refused(state, claimed):
    acks = os.path.join(str(state), FR.ACKS_DIR)
    claimed = claimed.format(acks=acks)
    assert FR.ack_receipt_path(str(state), claimed) is None, claimed
    assert FR.validate_command({"stop": True, "ack": claimed}, str(state)), claimed


def test_the_senders_own_ack_still_lands_and_confirms(state):
    """The real sender (task_router.fleet_handoff) through the real reader."""
    with open(os.path.join(str(state), "status.json"), "w", encoding="utf-8") as fh:
        json.dump({"running": True, "updated": __import__("time").time()}, fh)
    status, _ = TR.fleet_handoff("a goal", "abc123def456", str(state))
    assert status == "dispatched"
    cmds = FR.read_commands(str(state))
    assert TR.fleet_landing_confirmed("abc123def456", str(state)) is True
    ok, box, _ = _admit(cmds[0], state)
    assert ok, box
    body = json.load(open(TR._ack_path("abc123def456", str(state)), encoding="utf-8"))
    assert body["read"] is True and "rejected" not in body


def test_a_relative_or_differently_spelled_state_dir_still_matches(state, monkeypatch):
    """Identity, not spelling: the sender and the reader may spell one directory differently."""
    parent = os.path.dirname(str(state))
    monkeypatch.chdir(parent)
    rel_state = os.path.basename(str(state))
    claimed = os.path.join(str(state).upper() if os.name == "nt" else str(state),
                           FR.ACKS_DIR, "j1.ack")
    target = FR.ack_receipt_path(rel_state, claimed)
    assert target is not None
    assert os.path.basename(target) == "j1.ack"


def test_a_link_planted_at_the_receipt_name_is_replaced_not_written_through(state, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    acks = os.path.join(str(state), FR.ACKS_DIR)
    os.makedirs(acks, exist_ok=True)
    try:
        os.link(str(victim), os.path.join(acks, "jlink.ack"))
    except (OSError, NotImplementedError) as exc:
        pytest.skip("hard links unavailable here: %s" % exc)
    _drop(state, {"stop": True, "ack": os.path.join(acks, "jlink.ack")})
    FR.read_commands(str(state))
    assert victim.read_text(encoding="utf-8") == "precious", "the receipt wrote through a link"
    assert json.load(open(os.path.join(acks, "jlink.ack"), encoding="utf-8"))["read"] is True


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows mechanism")
def test_acks_replaced_by_a_junction_does_not_carry_the_write_out(state, tmp_path):
    import _winapi
    outside = tmp_path / "outside_target"
    outside.mkdir()
    acks = os.path.join(str(state), FR.ACKS_DIR)
    _winapi.CreateJunction(str(outside), acks)
    _drop(state, {"stop": True, "ack": os.path.join(acks, "jj.ack")})
    FR.read_commands(str(state))
    assert os.listdir(str(outside)) == [], "the receipt followed a junction out of the state dir"


def test_a_rejected_command_with_a_good_ack_gets_a_receipt_that_says_so(state):
    """It WAS read -- that is all a receipt claims -- and the sender learns why it did nothing,
    instead of re-sending an invalid goal forever."""
    TR.write_command(str(state), {"add_goal": [{"text": "x" * (FR.MAX_COMMAND_TEXT + 1)}],
                                  "ack": TR._ack_path("jbig", str(state))})
    FR.read_commands(str(state))
    body = json.load(open(TR._ack_path("jbig", str(state)), encoding="utf-8"))
    assert body["read"] is True and body["rejected"] is True
    assert any("add_goal" in e for e in body["errors"])
    assert "xxxxxxxx" not in json.dumps(body), "the receipt echoed the goal text"


# ======================================================================= (b) schema + bounds

@pytest.mark.parametrize("cmd", [
    {"set_disk_floor_gb": 100.0}, {"set_disk_floor_gb": 0}, {"set_disk_floor_gb": 6.5},
    {"set_ram_floor_mb": 65536}, {"set_ram_floor_mb": 0.0}, {"set_ram_floor_mb": 512.0},
    {"set_maxtabs": 1}, {"set_maxtabs": 100}, {"set_maxtabs": 4.0},
])
def test_values_the_settings_panel_can_produce_are_admitted(cmd, state):
    ok, box, _ = _admit(cmd, state)
    assert ok, box


@pytest.mark.parametrize("cmd", [
    {"set_disk_floor_gb": 100.1}, {"set_disk_floor_gb": -1}, {"set_disk_floor_gb": 1e9},
    {"set_disk_floor_gb": math.nan}, {"set_disk_floor_gb": math.inf},
    {"set_disk_floor_gb": "8"}, {"set_disk_floor_gb": True}, {"set_disk_floor_gb": None},
    {"set_ram_floor_mb": 65537}, {"set_ram_floor_mb": -0.5}, {"set_ram_floor_mb": [512]},
    {"set_maxtabs": 0}, {"set_maxtabs": 101}, {"set_maxtabs": 2.5}, {"set_maxtabs": True},
    {"set_autoscale": {"on": 1, "max": 1000}}, {"set_autoscale": {"on": "yes"}},
    {"set_autoscale": {"on": 1, "evil": 1}}, {"set_autoscale": [1]},
    {"close": "w0"}, {"close": [""]}, {"close": ["w" * 65]}, {"close": [{"n": 1}]},
    {"steer": [{"worker": "w0", "text": "x" * (FR.MAX_COMMAND_TEXT + 1)}]},
    {"steer": [{"worker": "w0", "text": "hi", "run": "calc.exe"}]}, {"steer": [None]},
    {"steer": [{"worker": "w0\nfake", "text": "hi"}]},
    {"reunlock": 5}, {"reunlock": "w" * 65},
    {"add_goal": [{"text": "a", "shell": "rm -rf"}]}, {"add_goal": [5]},
    {"add_goal": [{"text": "a", "checks": "pytest -q"}]},
    {"add_goal": [{"text": "a", "checks": [{"type": "pytest"}] * (FR.MAX_CHECKS + 1)}]},
    {"add_goal": [{"text": "a", "jid": "../../x"}]},
    {"add_goal": [{"text": "a", "cwd": "C:\\x\n"}]},
    {"add_goal": ["g"] * (FR.MAX_ITEMS + 1)},
    {"pause": "yes"}, {"stop": 2},
    {"unknown": True}, {}, [], "stop", None,
    {"pause": True, "set_disk_floor_gb": -5},
])
def test_anything_else_is_refused_whole_and_recorded(cmd, state):
    ok, box, said = _admit(cmd, state)
    assert not ok, "admitted %r" % (cmd,)
    assert len(box) == 1 and box[0]["errors"], box
    assert said and said[0].startswith("[command] REJECTED"), said


def test_a_refusal_names_keys_never_the_text_it_carried(state):
    secret = "the operators own words " + "z" * FR.MAX_COMMAND_TEXT
    ok, box, said = _admit({"steer": [{"worker": "w0", "text": secret}]}, state)
    assert not ok
    blob = json.dumps(box) + " ".join(said)
    assert "operators own words" not in blob and "zzzz" not in blob
    assert box[0]["keys"] == ["steer"]


def test_the_record_is_bounded(state):
    box = []
    for i in range(FR.MAX_REJECTIONS_KEPT + 15):
        FR.admit_command({"bogus%d" % i: 1}, str(state), box, log=lambda m: None)
    assert len(box) == FR.MAX_REJECTIONS_KEPT
    assert box[-1]["keys"] == ["bogus%d" % (FR.MAX_REJECTIONS_KEPT + 14)], "newest must be kept"


def test_refusals_reach_status_json():
    box = []
    FR.admit_command({"set_disk_floor_gb": -1}, None, box, log=lambda m: None)
    snap = FR._snapshot([], 0.0, 0, command_rejections=box)
    assert snap["command_rejections"] == box
    assert FR._snapshot([], 0.0, 0)["command_rejections"] == []


# ------------------------------------------------ every real writer's shape is still admitted

# One per sender in ui/FleetCockpit.cs, ui/ChatSend.cs, bench/fleet_ctl.py, relay/code_task.py --
# written from those call sites. A validator stricter than its writers is a broken cockpit.
WRITER_SHAPES = [
    {"close": ["w1"]},                                              # RequestClose
    {"set_maxtabs": 4},                                             # RequestSetMaxtabs
    {"set_autoscale": {"on": 1, "default": 2, "max": 100}},         # RequestSetAutoscale
    {"set_autoscale": {"on": 0, "default": 3, "max": 3}},
    {"steer": [{"worker": "w0", "text": "focus on the tests"}]},    # RequestSteer
    {"steer": [{"worker": "", "text": "everyone: stop"}]},          # broadcast
    {"reunlock": ""}, {"reunlock": "w2"}, {"reunlock": "*"},        # the fallback button
    {"pause": True}, {"pause": False}, {"stop": True},              # Pause / Stop / fleet_ctl
    {"set_disk_floor_gb": 6.0}, {"set_disk_floor_gb": 0.0},         # SetDiskFloor / 強制開始
    {"set_ram_floor_mb": 512.0},                                    # SetRamFloor
    {"add_goal": [{"text": "retry me", "checks": None, "cwd": "", "priority": True}]},  # Retry
    {"add_goal": [{"text": "retry me", "checks": [{"type": "pytest", "args": "-q"}],
                   "cwd": "C:\\work", "priority": True}]},
    {"add_goal": [{"text": "follow up", "resume_conv": "sess:11111111-2222",
                   "follow_up_to": "the earlier goal", "priority": True,
                   "new_task": True}]},                              # ChatSend follow-up
    {"add_goal": {"text": "one", "priority": False}},               # ChatSend AppendCommand
    {"add_goal": ["a bare string goal"]},
]


@pytest.mark.parametrize("cmd", WRITER_SHAPES)
def test_every_shape_a_real_writer_sends_is_admitted(cmd, state):
    ok, box, _ = _admit(cmd, state)
    assert ok, box


def test_task_router_writes_pass_the_gate_end_to_end(state):
    TR.add_goal_to_live_fleet("from the router", str(state), priority=True, jid="0a1b2c3d4e5f")
    TR.add_goal_to_live_fleet("from code_task", str(state),
                              entry={"text": "from code_task", "cwd": str(state),
                                     "priority": False, "checks": [{"type": "pytest"}]})
    cmds = FR.read_commands(str(state))
    assert len(cmds) == 2
    for c in cmds:
        ok, box, _ = _admit(c, state)
        assert ok, box


# ======================================================================= the wiring in main()

def test_apply_command_passes_the_gate_before_it_touches_anything():
    """_apply_command is a closure inside main(), so the one thing a runtime test cannot reach
    is that it calls the gate first. Everything the gate DOES is tested above at runtime."""
    src = open(RUNNER, encoding="utf-8").read()
    body = src[src.index("    def _apply_command(cmd, workers):"):]
    body = body[:body.index("\n    def ", 10)]
    gate = body.index("_errs = validate_command(cmd, args.state_dir)")
    reject = body.index("record_command_rejection(rejections_box, cmd, _errs)")
    first_effect = body.index("by_name = {w.name: w for w in workers}")
    assert gate < reject < first_effect
    assert "command_rejections=rejections_box" in src


# ======================================================================= (c) the file tools

@pytest.mark.parametrize("rel", [
    ".fleet/commands.d/0001.json", ".fleet/commands.d", ".fleet/acks/j.ack",
    ".fleet/commands.json", ".FLEET/Commands.D/x.json",
])
def test_the_mcp_file_tools_refuse_the_command_channel(rel, tmp_path):
    from tools import file_ops as FO
    with pytest.raises(PermissionError, match="command channel"):
        FO._validate_path(str(tmp_path / rel))


def test_a_configured_state_dir_is_protected_by_its_own_path(tmp_path, monkeypatch):
    from tools import file_ops as FO
    sd = tmp_path / "my_state"
    monkeypatch.setenv("FLEET_STATE_DIR", str(sd))
    with pytest.raises(PermissionError, match="command channel"):
        FO._validate_path(str(sd / "commands.d" / "x.json"))


def test_the_rest_of_the_state_dir_is_not_caught(tmp_path):
    from tools import file_ops as FO
    for rel in (".fleet/status.json", ".fleet/commands.d.txt", "notes/commands.d/x"):
        try:
            FO._validate_path(str(tmp_path / rel))
        except PermissionError as exc:
            assert "command channel" not in str(exc), rel

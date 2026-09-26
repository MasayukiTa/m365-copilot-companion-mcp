# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from relay import fleet_runner as fr
from relay import task_router as tr


def _command(state, text="rescue me"):
    return tr.write_command(str(state), {"add_goal": [{"text": text, "priority": False}]})


def test_specific_adopt_claim_must_be_inside_this_state_dirs_command_channel(tmp_path):
    state = tmp_path / "fleet"
    other = tmp_path / "other"
    good = _command(state)
    bad = _command(other)
    c = fr.claim_specific_command(str(state), good)
    assert c is not None and c["cmd"]["add_goal"][0]["text"] == "rescue me"
    fr.restore_command_claim(c)
    assert fr.claim_specific_command(str(state), bad) is None
    assert os.path.isfile(bad)


def test_adopt_accepts_only_a_goal_command_and_restores_invalid_input(tmp_path):
    state = tmp_path / "fleet"
    good = _command(state, "goal A")
    claim, goals, errors = fr.claim_adopt_command(str(state), good)
    assert claim is not None and errors == []
    assert [fr.goal_fields(g)[0] for g in goals] == ["goal A"]
    fr.restore_command_claim(claim)

    mixed = tr.write_command(str(state), {"add_goal": [{"text": "x"}], "stop": True})
    claim, goals, errors = fr.claim_adopt_command(str(state), mixed)
    assert claim is None and goals == [] and errors
    assert os.path.isfile(mixed), "an invalid rescue command must be returned to the channel"


def test_duplicate_rescue_runner_refuses_before_touching_the_command(tmp_path):
    state = tmp_path / "fleet"
    os.makedirs(state, exist_ok=True)
    cmd = _command(state)
    # Parent process is live. The subprocess must hit the single-instance marker guard before
    # --adopt-command is allowed to claim anything.
    fr._write_active_marker(str(state), argv=["-g", "existing"], pid=os.getpid(), start_ts=1.0)
    r = subprocess.run([
        sys.executable, "-m", "relay.fleet_runner", "--adopt-command", cmd,
        "--state-dir", str(state), "--agent-url", "http://127.0.0.1:9",
    ], cwd=str(REPO), capture_output=True, text=True, timeout=60)
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert os.path.isfile(cmd), "a runner that lost the state-dir race must not take the task"


def test_adopt_commit_happens_only_after_ledger_and_active_marker_exist():
    src = Path(fr.__file__).read_text(encoding="utf-8")
    main = src[src.index("def main():"):]
    ledger = main.index("_write_goals_ledger(")
    marker = main.index("_write_active_marker(")
    commit = main.index("commit_command_claim(args.state_dir, _adopt_claim")
    assert ledger < marker < commit
    assert "--adopt-command" in main
    assert "_adopt_goals + _read_goals(args)" in main

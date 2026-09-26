# -*- coding: utf-8 -*-
"""A fleet command is durable until it has actually been applied/committed.

The live runner used to delete commands.d/<id>.json inside read_commands(), then apply it later.
A crash in that gap destroyed an add_goal before the live-goal ledger could record it.  The
command channel now has an ownership boundary: atomic claim -> apply -> commit.  Dead owners'
claims are recovered by the next coordinator.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from relay import fleet_runner as fr  # noqa: E402
from relay import task_router as tr  # noqa: E402


def _write(state, text="durable goal", ack=None):
    patch = {"add_goal": [{"text": text, "priority": False}]}
    if ack:
        patch["ack"] = ack
    return tr.write_command(str(state), patch)


def test_claim_hides_the_command_but_does_not_destroy_it(tmp_path):
    original = _write(tmp_path)
    claims = fr.claim_commands(str(tmp_path))
    assert len(claims) == 1
    c = claims[0]
    assert c["cmd"]["add_goal"][0]["text"] == "durable goal"
    assert not os.path.exists(original), "the unclaimed name must disappear atomically"
    assert os.path.isfile(c["claimed"]), "the command itself must still exist until commit"
    fr.restore_command_claim(c)
    assert os.path.isfile(original)


def test_commit_is_the_point_the_command_disappears_and_ack_means_applied(tmp_path):
    ack = str(tmp_path / "acks" / "j1.ack")
    original = _write(tmp_path, ack=ack)
    c = fr.claim_commands(str(tmp_path))[0]
    assert not os.path.exists(ack)
    assert fr.commit_command_claim(str(tmp_path), c, applied=True, rejected_errors=[]) is True
    assert not os.path.exists(c["claimed"])
    assert not os.path.exists(original)
    body = json.loads(Path(ack).read_text(encoding="utf-8"))
    assert body["read"] is True and body["applied"] is True
    assert not body.get("rejected")


def test_rejected_command_is_committed_with_a_rejected_receipt(tmp_path):
    ack = str(tmp_path / "acks" / "j2.ack")
    p = tr.write_command(str(tmp_path), {"unknown": 1, "ack": ack})
    c = fr.claim_commands(str(tmp_path))[0]
    errors = fr.validate_command(c["cmd"], str(tmp_path))
    assert errors
    assert fr.commit_command_claim(str(tmp_path), c, applied=False, rejected_errors=errors)
    assert not os.path.exists(p)
    body = json.loads(Path(ack).read_text(encoding="utf-8"))
    assert body["rejected"] is True
    assert body["applied"] is False
    assert body["errors"]


def test_a_process_that_dies_after_claim_does_not_lose_the_command(tmp_path):
    original = _write(tmp_path, text="survive a crash")
    code = r'''\
import json, sys
from relay import fleet_runner as fr
c = fr.claim_commands(sys.argv[1])
print(json.dumps([x["claimed"] for x in c]))
# exit without commit or restore: this is the crash boundary under test
'''
    r = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=str(REPO),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (r.stdout, r.stderr)
    dead_claims = json.loads(r.stdout.strip().splitlines()[-1])
    assert len(dead_claims) == 1 and os.path.isfile(dead_claims[0])
    assert not os.path.exists(original)

    recovered = fr.claim_commands(str(tmp_path))
    assert len(recovered) == 1, "the next live coordinator must recover the dead owner's claim"
    assert recovered[0]["cmd"]["add_goal"][0]["text"] == "survive a crash"
    fr.restore_command_claim(recovered[0])


def test_an_applied_leftover_is_never_replayed(tmp_path):
    _write(tmp_path, text="already applied")
    c = fr.claim_commands(str(tmp_path))[0]
    applied = c["claimed"] + ".applied"
    os.replace(c["claimed"], applied)
    # Even if an earlier process died before deleting its applied tombstone, it is not work.
    got = fr.claim_commands(str(tmp_path))
    assert got == []
    assert not os.path.exists(applied), "stale applied tombstones should be housekeeping only"


def test_live_drain_uses_claim_apply_commit_not_read_then_delete():
    src = Path(fr.__file__).read_text(encoding="utf-8")
    i = src.index("def _drain_commands(workers):")
    block = src[i:i + 1800]
    assert "claim_commands(args.state_dir)" in block
    assert "commit_command_claim(" in block
    assert "restore_command_claim(" in block
    assert "for cmd in read_commands(args.state_dir)" not in block

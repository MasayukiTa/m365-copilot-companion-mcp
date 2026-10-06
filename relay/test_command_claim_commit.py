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
    block = src[i:i + 2200]
    assert "claim = claim_next_command(args.state_dir)" in block
    assert "commit_command_claim(" in block
    assert "restore_command_claim(" in block
    assert "for cmd in read_commands(args.state_dir)" not in block
    assert "for claim in claim_commands(args.state_dir)" not in block


def test_applied_tombstone_recovers_missing_ack_instead_of_just_being_deleted(tmp_path):
    """PR47 #4111676530: crash after commit rename but before receipt must be recoverable."""
    ack = str(tmp_path / 'acks' / 'recover.ack')
    _write(tmp_path, text='receipt survives', ack=ack)
    c = fr.claim_commands(str(tmp_path))[0]
    applied = c['claimed'] + '.applied'
    os.replace(c['claimed'], applied)   # exact crash boundary: commit point happened, no ack yet
    assert not os.path.exists(ack)
    got = fr.claim_commands(str(tmp_path))
    assert got == [], 'an applied tombstone is never replayable work'
    assert os.path.isfile(ack), 'recovery must publish the receipt before clearing the tombstone'
    body = json.loads(Path(ack).read_text(encoding='utf-8'))
    assert body.get('applied') is True and body.get('read') is True
    assert not os.path.exists(applied)


def test_append_can_return_only_goals_newly_admitted_to_the_ledger(tmp_path):
    """PR47 #4111676566: the ledger is the idempotency record for recovered add_goal commands."""
    fr._write_goals_ledger(str(tmp_path), ['same'], started=1.0, raise_on_error=True)
    got = fr._append_goals_ledger(str(tmp_path), ['same', 'new'], started=1.0,
                                  raise_on_error=True, return_new=True)
    assert [fr.goal_fields(g)[0] for g in got] == ['new']


def test_live_drain_keeps_failed_commits_for_commit_only_retry_not_reapply():
    """PR47 #4111676622: a failed post-apply rename must not strand or re-run the command."""
    src = Path(fr.__file__).read_text(encoding='utf-8')
    i = src.index('def _drain_commands(workers):')
    block = src[i:i + 2600]
    assert '_pending_command_commits' in block
    assert 'retry_pending_command_commits(' in block
    assert '_pending_command_commits.append(' in block


def test_commit_only_retry_never_calls_command_application_again(tmp_path, monkeypatch):
    claim = {"claimed": "c", "cmd": {"stop": True}, "name": "c"}
    pending = [(claim, True, [])]
    answers = iter([False, True])
    calls = []
    def fake_commit(state_dir, got, applied=None, rejected_errors=None):
        calls.append((got, applied, list(rejected_errors or [])))
        return next(answers)
    monkeypatch.setattr(fr, "commit_command_claim", fake_commit)
    assert fr.retry_pending_command_commits(str(tmp_path), pending) == 1
    assert len(pending) == 1
    assert fr.retry_pending_command_commits(str(tmp_path), pending) == 0
    assert pending == []
    assert len(calls) == 2 and all(c[1] is True for c in calls)


def test_receipt_failure_is_a_pending_commit_and_retries_from_the_existing_tombstone(tmp_path, monkeypatch):
    """PR47 #4115030177: effects are committed, but handoff is not complete until ack is durable."""
    ack = str(tmp_path / "acks" / "later.ack")
    _write(tmp_path, text="receipt later", ack=ack)
    c = fr.claim_commands(str(tmp_path))[0]
    real_write = fr._write_receipt
    monkeypatch.setattr(fr, "_write_receipt", lambda *a, **k: False)
    assert fr.commit_command_claim(str(tmp_path), c, applied=True) is False
    tomb = c["claimed"] + ".applied"
    assert os.path.isfile(tomb)
    assert not os.path.exists(ack)

    # The retry is commit-only: the original .claim path is already gone, so this proves
    # commit_command_claim can continue from the .applied tombstone instead of trying to rename
    # or re-apply the command a second time.
    pending = [(c, True, [])]
    monkeypatch.setattr(fr, "_write_receipt", real_write)
    assert fr.retry_pending_command_commits(str(tmp_path), pending) == 0
    assert pending == []
    assert os.path.isfile(ack)
    assert not os.path.exists(tomb)


def test_live_apply_enqueues_only_newly_durable_goals():
    src = Path(fr.__file__).read_text(encoding="utf-8")
    i = src.index("def _apply_command(cmd, workers, submission_id=None):")
    block = src[i:i + 5000]
    assert "return_new=True" in block
    assert "for g in _new_cmd_goals:" in block
    assert "for g in _cmd_goals:" not in block

def test_claim_owner_identity_includes_process_birth_token(tmp_path):
    p = _write(tmp_path, text="birth token")
    c = fr.claim_commands(str(tmp_path))[0]
    pid, birth = fr._claim_owner_identity(c["claimed"])
    assert pid == os.getpid()
    assert birth > 0, c["claimed"]
    fr.restore_command_claim(c)


def test_pid_reuse_does_not_strand_a_dead_claim(tmp_path, monkeypatch):
    """A different process reusing the same numeric pid is not the claim owner."""
    cmd = _write(tmp_path, text="recover after pid reuse")
    claimed = cmd + ".claim-4242-111000"
    os.replace(cmd, claimed)
    monkeypatch.setattr(fr, "_pid_alive", lambda pid: int(pid) == 4242)
    monkeypatch.setattr(fr, "_pid_birth_token", lambda pid: 222000 if int(pid) == 4242 else 0)
    got = fr.claim_commands(str(tmp_path))
    assert len(got) == 1
    assert got[0]["cmd"]["add_goal"][0]["text"] == "recover after pid reuse"
    fr.restore_command_claim(got[0])


def test_same_pid_and_birth_token_keeps_a_live_claim_owned(tmp_path, monkeypatch):
    cmd = _write(tmp_path, text="still owned")
    claimed = cmd + ".claim-4242-111000"
    os.replace(cmd, claimed)
    monkeypatch.setattr(fr, "_pid_alive", lambda pid: int(pid) == 4242)
    monkeypatch.setattr(fr, "_pid_birth_token", lambda pid: 111000 if int(pid) == 4242 else 0)
    assert fr.claim_commands(str(tmp_path)) == []
    assert os.path.isfile(claimed)


def test_legacy_pid_only_claim_remains_conservative(tmp_path, monkeypatch):
    cmd = _write(tmp_path, text="legacy owner")
    claimed = cmd + ".claim-4242"
    os.replace(cmd, claimed)
    monkeypatch.setattr(fr, "_pid_alive", lambda pid: int(pid) == 4242)
    monkeypatch.setattr(fr, "_pid_birth_token", lambda pid: 999999)
    assert fr.claim_commands(str(tmp_path)) == []
    assert os.path.isfile(claimed)


def test_production_claims_only_one_command_at_a_time(tmp_path):
    _write(tmp_path, text="first")
    _write(tmp_path, text="second")
    c = fr.claim_next_command(str(tmp_path))
    assert c is not None
    assert os.path.isfile(c["claimed"])
    command_dir = tmp_path / fr.COMMANDS_DIR
    remaining = sorted(command_dir.glob("*.json"))
    assert len(remaining) == 1, "later work must remain unclaimed while the first command is in-flight"
    fr.restore_command_claim(c)
    assert len(list(command_dir.glob("*.json"))) == 2


def test_live_drain_never_preclaims_later_commands_before_current_commit():
    src = Path(fr.__file__).read_text(encoding="utf-8")
    i = src.index("def _drain_commands(workers):")
    block = src[i:i + 2400]
    assert "claim = claim_next_command(args.state_dir)" in block
    assert "for claim in claim_commands(args.state_dir)" not in block
    assert "while True:" in block
    # A failed apply is restored then the sweep stops, otherwise the same restored file could
    # be immediately reclaimed in a tight loop.
    assert "restore_command_claim(claim)" in block
    restore_i = block.index("restore_command_claim(claim)")
    assert "return" in block[restore_i:restore_i + 160]


def test_rejected_tombstone_recovery_preserves_rejected_decision_after_state_changes(tmp_path, monkeypatch):
    """The tombstone suffix is the durable decision; current validation state cannot rewrite history."""
    ack = str(tmp_path / "acks" / "rejected-recover.ack")
    path = tr.write_command(str(tmp_path), {"unknown": 1, "ack": ack})
    c = fr.claim_commands(str(tmp_path))[0]
    rejected = c["claimed"] + ".rejected"
    os.replace(c["claimed"], rejected)

    # Simulate tenant/config/state changing between the original refusal and crash recovery.
    # Re-validating now says OK, but the already-committed .rejected decision must not become
    # a successful/"dispatched" landing receipt.
    monkeypatch.setattr(fr, "validate_command", lambda cmd, state_dir: [])
    assert fr._recover_committed_claim_receipt(str(tmp_path), rejected, False) is True
    body = json.loads(Path(ack).read_text(encoding="utf-8"))
    assert body["read"] is True
    assert body["applied"] is False
    assert body["rejected"] is True
    assert isinstance(body.get("errors"), list)


def test_command_submission_identity_distinguishes_intentional_same_text_retries(tmp_path):
    fr._write_goals_ledger(str(tmp_path), [], started=1.0, raise_on_error=True)

    p1 = _write(tmp_path, text="same retry text")
    c1 = fr.claim_next_command(str(tmp_path))
    assert c1 is not None
    g1 = fr.goals_from_command(c1["cmd"], submission_id=c1["name"])
    assert len(g1) == 1 and g1[0].get("jid")
    n1 = fr._append_goals_ledger(str(tmp_path), g1, started=1.0, raise_on_error=True, return_new=True)
    assert len(n1) == 1
    fr.restore_command_claim(c1)

    # Re-reading THE SAME durable command after a crash must be idempotent.
    c1b = fr.claim_next_command(str(tmp_path))
    g1b = fr.goals_from_command(c1b["cmd"], submission_id=c1b["name"])
    assert g1b[0]["jid"] == g1[0]["jid"]
    assert fr._append_goals_ledger(str(tmp_path), g1b, started=1.0, raise_on_error=True, return_new=True) == []
    assert fr.commit_command_claim(str(tmp_path), c1b, applied=True)

    # A NEW command with the SAME text is an intentional retry and must get a new identity.
    p2 = _write(tmp_path, text="same retry text")
    assert p2 != p1
    c2 = fr.claim_next_command(str(tmp_path))
    g2 = fr.goals_from_command(c2["cmd"], submission_id=c2["name"])
    assert g2[0]["jid"] != g1[0]["jid"]
    n2 = fr._append_goals_ledger(str(tmp_path), g2, started=1.0, raise_on_error=True, return_new=True)
    assert len(n2) == 1, "a deliberate same-text retry must become a new durable task"
    fr.restore_command_claim(c2)


def test_command_submission_identity_preserves_explicit_jid():
    cmd = {"add_goal": [{"text": "x", "jid": "callerjid123"}]}
    got = fr.goals_from_command(cmd, submission_id="some-command.json")
    assert got[0]["jid"] == "callerjid123"


def test_multi_goal_command_gets_stable_distinct_per_item_jids():
    cmd = {"add_goal": [{"text": "x"}, {"text": "x"}]}
    a = fr.goals_from_command(cmd, submission_id="cmd-abc.json")
    b = fr.goals_from_command(cmd, submission_id="cmd-abc.json")
    assert a[0]["jid"] != a[1]["jid"]
    assert [g["jid"] for g in a] == [g["jid"] for g in b]


def test_live_drain_passes_claim_identity_into_goal_admission():
    src = Path(fr.__file__).read_text(encoding="utf-8")
    i = src.index("def _drain_commands(workers):")
    block = src[i:i + 3500]
    assert '_apply_command(claim["cmd"], workers, submission_id=claim["name"])' in block
    assert "goals_from_command(cmd, submission_id=submission_id)" in block


def test_live_command_admission_refuses_local_loop_control_envelopes():
    for text in (
        "Execute LOCAL_LOOP job companion_abc (seq=1, worker=local_x).",
        "Run LOCAL_LOOP job companion_abc (seq=1, worker=local_x).",
        "LOCAL_LOOP RUN companion_abc seq=1 worker=local_x",
        "LOCAL_LOOP bootstrap companion_abc",
        "LOCAL_LOOP protocol companion_abc",
        "RUN companion_20260929_x seq=1 worker=local_abc",
        "RUN job_1 seq=2 worker=local_xyz",
        "LOCAL_LOOP job companion_20260929_x seq=1 worker=local_abc: claim and execute the operator-authored turn under the standard agent contract",
        "LOCAL_LOOP job companion_20260929_x seq=1 worker=local_abc を、標準エージェント契約の下で claim_turn して実行する",
        "Claim and execute LOCAL_LOOP job companion_20260929_x seq=1 worker=local_abc under the standard agent contract",
        "Claim and execute LOCAL_LOOP job companion_20260929_x seq=1 worker=local_abc: claim_turn(expected_seq=1) then execute the operator-authored turn",
    ):
        errors = fr.validate_command({"add_goal": [{"text": text}]})
        assert errors and any("LOCAL_LOOP control" in e for e in errors), (text, errors)


def test_cli_goal_boundary_rejects_local_loop_control_envelopes_before_start():
    good = [
        {"text": "Inspect the LOCAL_LOOP implementation and report races"},
        {"text": "Run companion analysis and summarize the result"},
        {"text": "LOCAL_LOOP job scheduling is too slow; investigate it"},
    ]
    assert fr.reject_local_loop_control_goals(good) == []
    bad = [{"text": "Run LOCAL_LOOP job companion_abc (seq=1, worker=local_x)."}]
    errors = fr.reject_local_loop_control_goals(bad)
    assert errors and "LOCAL_LOOP control" in errors[0]


def test_resume_expansion_is_rechecked_for_old_control_envelopes():
    src = Path(fr.__file__).read_text(encoding="utf-8")
    resume = src[src.index("if args.resume:"):src.index("if not args.agent_url:")]
    assert "goals = resume_goals + goals" in resume
    assert resume.index("goals = resume_goals + goals") < resume.index("reject_local_loop_control_goals(goals)")
    assert "return 4" in resume

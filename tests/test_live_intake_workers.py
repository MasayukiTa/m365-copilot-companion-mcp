"""Exercise the router command, admission gate, ledger and live worker sweep together."""
from relay import fleet_runner as fr, relay_fleet as rf, task_router as tr
from relay import test_admission as harness


def test_live_submissions_start_once(tmp_path, monkeypatch):
    state_dir = str(tmp_path)
    initial = {"text": "already running", "jid": "existing"}
    fr._write_goals_ledger(state_dir, [initial], 1, raise_on_error=True)
    monkeypatch.setattr(rf, "avail_phys_mb", lambda: 64000.0)
    monkeypatch.setattr(rf, "free_disk_gb", lambda *a, **k: 500.0)
    controls = {"control": {}}
    original = harness._install_fake_worker(controls)
    added = []
    seen = []
    ticks = [0]

    def tick(workers):
        ticks[0] += 1
        if ticks[0] == 1:
            assert workers[0].status == "waiting"
            for jid in ("one", "two", "three", "existing"):
                tr.add_goal_to_live_fleet(jid, state_dir=state_dir, jid=jid,
                                          origin={"via": "mcp", "source": "test"})
        while True:
            claim = fr.claim_next_command(state_dir)
            if claim is None:
                break
            assert fr.validate_command(claim["cmd"], state_dir) == []
            goals = fr.goals_from_command(claim["cmd"], submission_id=claim["name"])
            added.extend(fr._append_goals_ledger(state_dir, goals, 1,
                                                 raise_on_error=True, return_new=True))
            assert fr.commit_command_claim(state_dir, claim, applied=True)
        live = [w for w in workers if w.status == "waiting"]
        if len(live) == 4:
            seen.extend(w.jid for w in live)
            controls["control"].update({w.name: "done" for w in live})
        if ticks[0] > 12:
            controls["control"].update({w.name: "done" for w in workers})

    try:
        rf.run_relay_fleet(harness.FakeContext(), [initial], "http://agent",
                           max_concurrent=4, poll_s=0, on_tick=tick, add_box=added,
                           notify=lambda *a, **k: None, fanout=False)
    finally:
        harness._restore_worker(original)
    assert sorted(seen) == ["existing", "one", "three", "two"]
    assert len(fr._read_goals_ledger(state_dir)[1]) == 4


def test_origin_validation_is_bounded():
    assert fr.validate_command({"add_goal": {"text": "x", "origin": {"via": "mcp", "source": "agent"}}}) == []
    assert fr.validate_command({"add_goal": {"text": "x", "origin": {"via": []}}})
    assert fr.validate_command({"add_goal": {"text": "x", "origin": {"extra": "x"}}})

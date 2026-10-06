import json

from relay.local_job_store import LocalJobStore
from relay.local_loop_controller import main


def _job(job_id="job_1"):
    return {
        "job_id": job_id,
        "execution_profile": "LOCAL_LOOP",
        "data_location": "LOCAL",
        "requires_local_tool": True,
        "task": {"type": "companion_task", "instruction": "Prepare the report"},
        "constraints": {"max_turns": 20},
        "acceptance_checks": [],
    }


def test_operator_steer_cli_needs_no_browser_or_agent_url(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCP_EXECUTION_PROFILES", "1")
    monkeypatch.delenv("MCP_FLEET_AGENT_URL", raising=False)
    monkeypatch.delenv("MCP_IMPL_AGENT_URL", raising=False)
    db = tmp_path / "jobs.sqlite3"
    state = tmp_path / "fleet"
    store = LocalJobStore(db)
    store.create_job(_job(), now=1)
    steer = tmp_path / "steer.txt"
    steer.write_text("Focus only on the executive summary", encoding="utf-8")

    rc = main([
        "--operator-steer-job-id", "job_1",
        "--operator-steer-file", str(steer),
        "--db", str(db),
        "--state-dir", str(state),
    ])

    assert rc == 0
    assert not steer.exists()
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["ok"] is True and result["pending"] == 1
    status = json.loads((state / "status.json").read_text(encoding="utf-8"))
    worker = next(w for w in status["workers"] if w["name"] == "job_1")
    assert worker["execution"]["operator_steer_pending"] == 1
    assert worker["execution"]["last_progress"] == "Focus only on the executive summary"


def test_operator_steer_cli_failure_keeps_the_only_input_copy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCP_EXECUTION_PROFILES", "1")
    monkeypatch.delenv("MCP_FLEET_AGENT_URL", raising=False)
    monkeypatch.delenv("MCP_IMPL_AGENT_URL", raising=False)
    db = tmp_path / "jobs.sqlite3"
    state = tmp_path / "fleet"
    store = LocalJobStore(db)
    store.create_job(_job(), now=1)
    claim = store.claim_turn("job_1", 1, "w", now=2)
    store.commit_turn(
        "job_1", 1, claim["lease_id"], claim["fencing_token"],
        "CANDIDATE_DONE", "done", now=3,
    )
    store.verify_candidate("job_1", True, "verified", now=4)

    steer = tmp_path / "steer.txt"
    steer.write_text("Too late to alter this terminal job", encoding="utf-8")
    rc = main([
        "--operator-steer-job-id", "job_1",
        "--operator-steer-file", str(steer),
        "--db", str(db),
        "--state-dir", str(state),
    ])

    assert rc == 2
    assert steer.exists()
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["ok"] is False
    assert result["error"] == "JOB_TERMINAL"


def test_operator_steer_cli_requires_both_job_and_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_EXECUTION_PROFILES", "1")
    try:
        main(["--operator-steer-job-id", "job_1", "--state-dir", str(tmp_path)])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("argparse should reject a partial operator-steer mode")

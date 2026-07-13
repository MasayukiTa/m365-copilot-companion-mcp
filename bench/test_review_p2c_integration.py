import json

from bench import review_run
from relay.review_resilience import TaskEnvelope


def test_fleet_cmd_adds_p2c_flags_only_when_requested(monkeypatch):
    monkeypatch.setattr(review_run, "VENVPY", "python.exe")
    normal = review_run.fleet_cmd("goals.jsonl", 4, "auto")
    assert "--resilience-profile" not in normal
    assert normal[normal.index("--max-turns") + 1] == review_run.FLEET_MAX_TURNS

    p2c = review_run.fleet_cmd(
        "goals.jsonl", 4, "auto", state_dir="state",
        resilience_profile="review", max_turns=12,
    )
    assert p2c[p2c.index("--resilience-profile") + 1] == "review"
    assert p2c[p2c.index("--max-fresh-replays") + 1] == "1"
    assert p2c[p2c.index("--max-turns") + 1] == "12"


def test_recovery_decomposes_twice_refused_worker_and_merges_findings(tmp_path, monkeypatch):
    parent = TaskEnvelope(
        "review-correctness-0001", None, "campaign", "producer", "goal", str(tmp_path),
        metadata={
            "scope": ["a.py"],
            "authorization_preamble": "AUTHORIZED\n",
            "output_contract": "FINDINGS",
            "resilience_profile": "review",
        },
    )
    initial = tmp_path / "initial"
    initial.mkdir()
    (initial / "status.json").write_text(json.dumps({"workers": [{
        "name": "w0", "task_id": parent.task_id, "status": "content_refused",
        "outcome": "CONTENT_REFUSED", "fresh_replay_count": 1,
        "refusal_history": [{"response": "refused once"}, {"response": "refused twice"}],
    }]}), encoding="utf-8")

    calls = []

    def fake_run(goals_path, max_concurrent, effort, state_dir=None, **kwargs):
        calls.append((state_dir, kwargs))
        state = review_run.os.path.abspath(state_dir)
        review_run.os.makedirs(state, exist_ok=True)
        if "decomposer" in state:
            last = (
                '<<<SUBTASKS>>>\n[{"title":"input","objective":"inspect input",'
                '"files":["a.py"],"expected_evidence":["line"],'
                '"output_contract":"FINDINGS","reason_for_split":"input path"}]\n'
                '<<<END_SUBTASKS>>>\nDONE'
            )
            workers = [{"name": "w0", "status": "done", "outcome": "DONE",
                        "display_result": last}]
        else:
            last = (
                '<<<FINDINGS>>>\n[{"file":"a.py","line":3,"severity":"high",'
                '"title":"bug","detail":"proof"}]\n<<<END_FINDINGS>>>\nDONE'
            )
            workers = [{"name": "w0", "status": "done", "outcome": "DONE",
                        "display_result": last, "task_id": parent.task_id + "-d1-c1",
                        "fresh_replay_count": 0}]
        with open(review_run.os.path.join(state, "status.json"), "w", encoding="utf-8") as f:
            json.dump({"workers": workers}, f)
        return 0

    monkeypatch.setattr(review_run, "run_fleet", fake_run)
    agg = {"workers_total": 1, "parse_errors": 1, "findings": [],
           "by_severity": {"high": [], "medium": [], "low": []}}
    metrics = review_run.recover_content_refusals(
        agg, str(initial), {parent.task_id: parent}, "review", str(tmp_path), "stamp",
        4, "auto", str(tmp_path),
    )
    assert [f["title"] for f in agg["findings"]] == ["bug"]
    assert agg["parse_errors"] == 0
    assert metrics["fresh_replays"] == 1
    assert metrics["decomposed_parents"] == 1
    assert metrics["child_goals"] == 1
    assert metrics["unresolved_refusals"] == 0
    assert all(call[1]["resilience_profile"] == "review" for call in calls)

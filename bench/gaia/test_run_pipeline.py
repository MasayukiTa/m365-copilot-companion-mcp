import json
import os

from bench.gaia import run_pipeline


def _rows(count):
    return [
        {"task_id": "task-%03d" % index, "correct": index % 2 == 0, "level": 1}
        for index in range(count)
    ]


def test_compute_final_marks_incomplete_current_rows_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(run_pipeline, "FLEET_GAIA", tmp_path)
    monkeypatch.setattr(run_pipeline, "FINAL_JSON", tmp_path / "gaia_final_127.json")
    monkeypatch.setattr(run_pipeline, "PIPELINE_FINAL_JSON", tmp_path / "pipeline_final.json")
    payload = run_pipeline.compute_final(_rows(126), [], not_before=0)
    assert payload["status"] == "partial"
    assert payload["answered"] == 126


def test_compute_final_ignores_stale_controller_artifact(tmp_path, monkeypatch):
    final = tmp_path / "gaia_final_127.json"
    final.write_text(json.dumps(_rows(127)), encoding="utf-8")
    os.utime(final, (10, 10))
    monkeypatch.setattr(run_pipeline, "FLEET_GAIA", tmp_path)
    monkeypatch.setattr(run_pipeline, "FINAL_JSON", final)
    monkeypatch.setattr(run_pipeline, "PIPELINE_FINAL_JSON", tmp_path / "pipeline_final.json")
    payload = run_pipeline.compute_final(_rows(127), [], not_before=20)
    assert payload["status"] == "complete"
    assert payload["answered"] == 127


def test_failed_pipeline_artifact_never_claims_complete(tmp_path, monkeypatch):
    path = tmp_path / "pipeline_final.json"
    monkeypatch.setattr(run_pipeline, "PIPELINE_FINAL_JSON", path)
    payload = run_pipeline._write_failed_pipeline(["runner failed"])
    assert payload["status"] == "failed"
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "failed"

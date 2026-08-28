"""What the scorecard records about WHICH ARM ran, and what it does when nobody said.

The defect: `effort` was the literal "auto" in the genome, so every archived result claimed
the same arm whatever had run. Combined with the panel and research budgets not being manifest
parameters -- so two efforts hashed to the same harness_id -- the archive could not distinguish
the two arms of the comparison it existed to serve.
"""
import json

from bench.pro_record_result import _load_run_config


def test_a_recorded_arm_is_read_back_whole(tmp_path):
    p = tmp_path / "cfg.json"
    cfg = {"effort": "ultra", "harness_id": "abc123",
           "parameters": {"review_lens_count": 3, "max_research": 8}}
    p.write_text(json.dumps(cfg), encoding="utf-8")
    assert _load_run_config(str(p)) == cfg


def test_a_missing_run_config_reads_as_empty_not_as_a_default_arm(tmp_path):
    """UNKNOWN, not "auto". A guess here is how an unlabelled row joins the arm it did not
    belong to -- which is the exact way a comparison between two efforts can be lost."""
    assert _load_run_config(str(tmp_path / "nope.json")) == {}


def test_a_corrupt_run_config_reads_as_empty_rather_than_raising(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text("{not json", encoding="utf-8")
    assert _load_run_config(str(p)) == {}


def test_a_run_config_that_is_not_an_object_is_refused(tmp_path):
    """A JSON list would otherwise reach `.get` and take the recorder down at report time,
    after the run it was meant to record has already finished."""
    p = tmp_path / "cfg.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert _load_run_config(str(p)) == {}


def test_the_runner_and_the_recorder_agree_on_where_the_arm_is_written():
    """Two paths for one file is one place for them to drift. The runner writes it and the
    recorder defaults to reading it; if these ever differ, every run records an unknown arm
    and the only symptom is a warning nobody connects to a rename."""
    import inspect

    import bench.pro_record_result as rec
    import bench.pro_run_50 as run

    default = inspect.signature(rec.main).parameters  # argv-based; read the parser instead
    assert default is not None
    src = inspect.getsource(rec.main)
    assert "pro_run_config.json" in src
    assert run.RUN_CONFIG.endswith("pro_run_config.json")


def test_the_runner_names_its_effort_once():
    """The effort was a literal on the fleet's command line AND a literal in the recorder.
    One name, used in both places it is needed."""
    import inspect

    import bench.pro_run_50 as run
    src = inspect.getsource(run.main)
    assert '"--effort", EFFORT' in src
    assert '"--effort", "auto"' not in src

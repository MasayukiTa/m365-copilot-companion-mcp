"""Defects covered here (validity judge correction loop):

1. The worker is told only engine CODES, never the engine's own messages, so it is corrected
   repeatedly without learning what is wrong. The preflight must surface the engine's exact
   text -- unknown key paths, the load_claim messages, and any claim column that is not a column
   of the data file (together with the columns that DO exist) -- in the correction and in the
   durable record.

2. judge_result.json is write-once, so a stale harness ERROR record left by a failed request
   blocks a corrected (rewritten) request from ever being judged. The rewrite must supersede the
   stale ERROR record -- but must NEVER move aside a real verdict (HOLD/NA/ADOPT).
"""
import json
import os
from types import SimpleNamespace

from relay import judge_gate
from relay.judge_claim_preflight import check


def test_preflight_reports_engine_messages_and_columns(tmp_path):
    """check() returns the engine's own messages AND the bad columns next to the real header."""
    class Error(Exception):
        msgs = ["binomial_agg needs response.trials_column", "per_unit contrast needs value > 0"]

    def load_claim(claim):
        raise Error()

    engine = SimpleNamespace(unknown_key_paths=lambda claim: ["exposure.contrast.unit"],
                             load_claim=load_claim)
    data = tmp_path / "data.csv"
    data.write_text("actual,other\n1,2\n", encoding="utf-8")
    claim = {"response": {"column": "bad", "trials_column": "trials"},
             "exposure": {"column": "dose", "contrast": {"unit": "x"}},
             "hierarchy": {"unit_column": "lot"}}
    messages = check(claim, str(data), engine)
    text = "\n".join(messages)
    for expected in ("exposure.contrast.unit", *Error.msgs, "response.column", "response.trials_column",
                     "exposure.column", "hierarchy.unit_column", "['actual', 'other']"):
        assert expected in text


def test_retry_archives_error_but_never_verdict(tmp_path):
    """A rewritten request supersedes a stale harness ERROR record, but never a real verdict."""
    request = tmp_path / "judge_request.json"
    result = tmp_path / "judge_result.json"
    request.write_text("{}", encoding="utf-8")
    for token in ("HOLD", "NA", "ADOPT", "ERROR"):
        result.write_text(json.dumps({"source": "harness", "verdict": {"verdict": token}}), encoding="utf-8")
        now = result.stat().st_mtime_ns
        os.utime(request, ns=(now + 1000000000, now + 1000000000))
        judge_gate._supersede_retryable_result(str(request))
        assert result.exists() == (token != "ERROR")
    archives = list(tmp_path.glob("judge_result.superseded.*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["verdict"]["verdict"] == "ERROR"


def test_correcting_record_is_never_superseded(tmp_path):
    """A harness-correction record (bounded correction history, no verdict) is preserved so
    _write_judge_result can fold its history into the eventual authoritative record."""
    request = tmp_path / "judge_request.json"
    result = tmp_path / "judge_result.json"
    request.write_text("{}", encoding="utf-8")
    result.write_text(json.dumps({"source": "harness-correction", "status": "correcting",
                                  "corrections": [{"attempt": 1, "messages": ["x"]}]}),
                      encoding="utf-8")
    now = result.stat().st_mtime_ns
    os.utime(request, ns=(now + 1000000000, now + 1000000000))
    judge_gate._supersede_retryable_result(str(request))
    assert result.exists()
    assert not list(tmp_path.glob("judge_result.superseded.*.json"))


def test_correction_and_record_include_preflight(tmp_path, monkeypatch):
    """End to end: an engine that cannot load a worker-supplied claim sends the worker back with
    the engine's own messages AND the bad column, and the same text lands in the durable record."""
    root = tmp_path / "engine"
    package = root / "validity"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "claim.py").write_text(
        "def unknown_key_paths(c): return ['exposure.contrast.unit']\n"
        "class Invalid(Exception):\n msgs = ['binomial_agg needs response.trials_column', "
        "'per_unit contrast needs value > 0']\n"
        "def load_claim(c): raise Invalid()\n", encoding="utf-8")
    monkeypatch.setattr(judge_gate, "ENGINE_ROOT", str(root))
    monkeypatch.setattr(judge_gate, "is_data_analysis_goal", lambda *a: True)
    monkeypatch.setattr(judge_gate, "run_engine_judge", lambda *a, **kw: {
        "engine_available": True, "verdict": "ERROR"})
    (tmp_path / "data.csv").write_text("actual,other\n1,2\n", encoding="utf-8")
    claim = {"id": "case", "response": {"column": "bad"},
             "exposure": {"contrast": {"unit": "x"}}}
    request = {"claim_id": "case", "data_location": "data.csv", "fetched_at": "2026-01-01T00:00:00Z",
               "registration": {"root": "registry", "claim": claim,
                                "registered_at": "2025-01-01T00:00:00Z"}}
    (tmp_path / "judge_request.json").write_text(json.dumps(request), encoding="utf-8")
    _, detail, correction = judge_gate.complete_judge_request("analysis", str(tmp_path), correction_budget=2)
    record = json.loads((tmp_path / "judge_result.json").read_text(encoding="utf-8"))
    for expected in ("exposure.contrast.unit", "binomial_agg needs response.trials_column",
                     "per_unit contrast needs value > 0", "['actual', 'other']"):
        assert expected in detail
        assert expected in " ".join(correction["messages"])
        assert expected in " ".join(record["corrections"][0]["messages"])

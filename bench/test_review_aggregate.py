"""Hermetic unit tests for bench/review_aggregate.py.

No git, no network, no fleet subprocess -- just status.json / transcript.jsonl fixtures
written to tmp_path, matching relay/fleet_runner.py's final-snapshot shape and
relay/relay_fleet.py's _Transcript jsonl format.

  .venv\\Scripts\\python.exe -m pytest bench/test_review_aggregate.py -q
"""
import json
import os

import pytest

from bench.review_build_goals import FINDINGS_BEGIN, FINDINGS_END
from bench.review_aggregate import (
    aggregate,
    load_transcript_final_answer,
    parse_findings_block,
    render_json,
    render_markdown,
    worker_final_text,
)


def _findings_block(items):
    return FINDINGS_BEGIN + "\n" + json.dumps(items, ensure_ascii=False) + "\n" + FINDINGS_END


# --- parse_findings_block ---------------------------------------------------------------

def test_parse_findings_block_valid():
    items = [{"file": "a.py", "line": 12, "severity": "high", "title": "t", "detail": "d"}]
    text = "some preamble\n" + _findings_block(items) + "\nDONE"
    found, err = parse_findings_block(text)
    assert err is False
    assert found == items


def test_parse_findings_block_empty_array_is_not_an_error():
    text = _findings_block([]) + "\nDONE"
    found, err = parse_findings_block(text)
    assert found == []
    assert err is False


def test_parse_findings_block_missing():
    found, err = parse_findings_block("I looked at the files and found nothing. DONE")
    assert found == []
    assert err is True


def test_parse_findings_block_empty_text():
    found, err = parse_findings_block("")
    assert found == []
    assert err is True


def test_parse_findings_block_malformed_json():
    text = FINDINGS_BEGIN + "\n[{not valid json}]\n" + FINDINGS_END
    found, err = parse_findings_block(text)
    assert found == []
    assert err is True


def test_parse_findings_block_not_a_list():
    text = FINDINGS_BEGIN + '\n{"file": "a.py"}\n' + FINDINGS_END
    found, err = parse_findings_block(text)
    assert found == []
    assert err is True


def test_parse_findings_block_prose_after_end_still_parses():
    items = [{"file": "b.py", "line": None, "severity": "low", "title": "t", "detail": ""}]
    text = _findings_block(items) + "\n以上です。ご確認ください。\nDONE"
    found, err = parse_findings_block(text)
    assert err is False
    assert found == items


def test_parse_findings_block_never_raises_on_garbage():
    for garbage in (None, 12345, FINDINGS_BEGIN, FINDINGS_END, FINDINGS_END + FINDINGS_BEGIN):
        found, err = parse_findings_block(garbage if isinstance(garbage, str) else str(garbage))
        assert isinstance(found, list)
        assert isinstance(err, bool)


# --- load_transcript_final_answer --------------------------------------------------------

def _write_transcript(path, name="w0", goal="review goal", turns=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [{"meta": True, "key": "run_w0", "name": name, "goal": goal, "ts": 1.0}]
    for t in (turns or []):
        lines.append(t)
    with open(path, "w", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")


def test_load_transcript_final_answer_normal(tmp_path):
    path = str(tmp_path / "transcripts" / "run_w0.jsonl")
    _write_transcript(path, turns=[
        {"turn": 1, "role": "user", "text": "please review", "ts": 2.0},
        {"turn": 1, "role": "assistant", "text": "working on it", "ts": 3.0},
        {"turn": 2, "role": "user", "text": "continue", "ts": 4.0},
        {"turn": 2, "role": "assistant", "text": "final answer here", "ts": 5.0},
    ])
    assert load_transcript_final_answer(path) == "final answer here"


def test_load_transcript_final_answer_no_assistant_turn(tmp_path):
    path = str(tmp_path / "transcripts" / "run_w1.jsonl")
    _write_transcript(path, turns=[{"turn": 1, "role": "user", "text": "hi", "ts": 2.0}])
    assert load_transcript_final_answer(path) == ""


def test_load_transcript_final_answer_missing_file(tmp_path):
    assert load_transcript_final_answer(str(tmp_path / "nope.jsonl")) == ""


def test_load_transcript_final_answer_corrupt_lines(tmp_path):
    path = str(tmp_path / "transcripts" / "run_w2.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json at all\n")
        f.write(json.dumps({"turn": 1, "role": "assistant", "text": "ok answer"}) + "\n")
        f.write("garbage garbage\n")
    assert load_transcript_final_answer(path) == "ok answer"


def test_load_transcript_final_answer_empty_path():
    assert load_transcript_final_answer("") == ""
    assert load_transcript_final_answer(None) == ""


# --- worker_final_text -------------------------------------------------------------------

def test_worker_final_text_prefers_display_result(tmp_path):
    w = {"display_result": "from display_result", "last": "from last", "transcript": ""}
    assert worker_final_text(w, str(tmp_path)) == "from display_result"


def test_worker_final_text_falls_back_to_last(tmp_path):
    w = {"display_result": "", "last": "from last", "transcript": ""}
    assert worker_final_text(w, str(tmp_path)) == "from last"


def test_worker_final_text_falls_back_to_transcript(tmp_path):
    tpath = tmp_path / "transcripts" / "run_w3.jsonl"
    _write_transcript(str(tpath), turns=[
        {"turn": 1, "role": "assistant", "text": "recovered from transcript"},
    ])
    w = {"display_result": "", "last": "", "transcript": str(tpath)}
    assert worker_final_text(w, str(tmp_path / "transcripts")) == "recovered from transcript"


def test_worker_final_text_relative_transcript_resolved_under_dir(tmp_path):
    tdir = tmp_path / "transcripts"
    tpath = tdir / "run_w4.jsonl"
    _write_transcript(str(tpath), turns=[{"turn": 1, "role": "assistant", "text": "hi there"}])
    w = {"display_result": None, "last": None, "transcript": "run_w4.jsonl"}
    assert worker_final_text(w, str(tdir)) == "hi there"


def test_worker_final_text_never_raises_on_garbage():
    assert worker_final_text({}, "/does/not/exist") == ""
    assert worker_final_text({"transcript": None}, None) == ""


# --- aggregate ------------------------------------------------------------------------------

def _status_with_workers(workers):
    return {"total": len(workers), "done_count": len(workers), "running": False,
            "elapsed_s": 1.2, "workers": workers}


def test_aggregate_mixed_workers(tmp_path):
    status_path = tmp_path / "status.json"
    transcripts_dir = tmp_path / "transcripts"

    ok_findings = [
        {"file": "a.py", "line": 10, "severity": "high", "title": "SQL injection",
         "detail": "unescaped input"},
        {"file": "a.py", "line": 20, "severity": "low", "title": "naming",
         "detail": "misleading name"},
    ]
    workers = [
        {
            "name": "w0", "goal": "security review group 1", "outcome": "DONE",
            "reason": "refuter#1: UPHELD", "verified": True,
            "display_result": "見つけました。" + _findings_block(ok_findings) + "\nDONE",
            "transcript": "",
        },
        {
            "name": "w1", "goal": "security review group 2", "outcome": "DONE",
            "reason": "refuter#1: REFUTED", "verified": False,
            "display_result": "何も見つかりませんでした。DONE",  # no findings block -> parse_error
            "transcript": "",
        },
        {
            "name": "w2", "goal": "security review group 3", "outcome": "STUCK",
            "reason": "", "verified": None,
            "display_result": "", "last": "",
            "transcript": "w2.jsonl",
        },
    ]
    _write_transcript(str(transcripts_dir / "w2.jsonl"), turns=[
        {"turn": 1, "role": "assistant",
         "text": _findings_block([{"file": "b.py", "line": None, "severity": "medium",
                                    "title": "todo left in", "detail": ""}])},
    ])

    status_path.write_text(json.dumps(_status_with_workers(workers), ensure_ascii=False),
                            encoding="utf-8")

    agg = aggregate(str(status_path), str(transcripts_dir), now=12345.0)

    assert agg["generated_at"] == 12345.0
    assert agg["workers_total"] == 3
    assert agg["parse_errors"] == 1  # only w1
    assert len(agg["findings"]) == 3  # 2 from w0 + 1 from w2's transcript fallback

    by_sev = agg["by_severity"]
    assert len(by_sev["high"]) == 1
    assert len(by_sev["medium"]) == 1
    assert len(by_sev["low"]) == 1

    high = by_sev["high"][0]
    assert high["file"] == "a.py"
    assert high["worker"] == "w0"
    assert high["reason"] == "refuter#1: UPHELD"
    assert high["verified"] is True

    medium = by_sev["medium"][0]
    assert medium["worker"] == "w2"
    assert medium["file"] == "b.py"


def test_aggregate_corrupt_status_json_never_raises(tmp_path):
    bad = tmp_path / "status.json"
    bad.write_text("{not json", encoding="utf-8")
    agg = aggregate(str(bad), str(tmp_path))
    assert "error" in agg
    assert agg["workers_total"] == 0
    assert agg["findings"] == []
    assert agg["by_severity"] == {"high": [], "medium": [], "low": []}


def test_aggregate_missing_status_json_never_raises(tmp_path):
    agg = aggregate(str(tmp_path / "nope.json"), str(tmp_path))
    assert "error" in agg
    assert agg["workers_total"] == 0


def test_aggregate_empty_workers(tmp_path):
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(_status_with_workers([])), encoding="utf-8")
    agg = aggregate(str(status_path), str(tmp_path))
    assert agg["workers_total"] == 0
    assert agg["parse_errors"] == 0
    assert agg["findings"] == []


def test_aggregate_unexpected_severity_bucketed_as_low(tmp_path):
    status_path = tmp_path / "status.json"
    workers = [{
        "name": "w0", "goal": "g", "outcome": "DONE", "reason": "", "verified": True,
        "display_result": _findings_block(
            [{"file": "x.py", "line": 1, "severity": "critical", "title": "t", "detail": ""}]
        ) + "\nDONE",
        "transcript": "",
    }]
    status_path.write_text(json.dumps(_status_with_workers(workers)), encoding="utf-8")
    agg = aggregate(str(status_path), str(tmp_path))
    assert len(agg["by_severity"]["low"]) == 1
    assert agg["by_severity"]["high"] == []


# --- render_markdown / render_json --------------------------------------------------------

def test_render_markdown_structure():
    agg = {
        "generated_at": 1.0, "workers_total": 1, "parse_errors": 0,
        "findings": [
            {"file": "a.py", "line": 5, "severity": "high", "title": "bug",
             "detail": "explanation", "worker": "w0", "reason": "refuter#1: UPHELD",
             "verified": True},
        ],
        "by_severity": {
            "high": [{"file": "a.py", "line": 5, "severity": "high", "title": "bug",
                      "detail": "explanation", "worker": "w0",
                      "reason": "refuter#1: UPHELD", "verified": True}],
            "medium": [],
            "low": [],
        },
    }
    md = render_markdown(agg)
    assert "# Review findings report" in md
    assert "high (1)" in md
    assert "medium (0)" in md
    assert "low (0)" in md
    assert "a.py:5" in md
    assert "bug" in md
    assert "explanation" in md
    assert "w0" in md
    assert "verified" in md
    assert "refuter#1: UPHELD" in md


def test_render_markdown_handles_error_and_no_findings():
    agg = {"generated_at": None, "workers_total": 0, "parse_errors": 0, "findings": [],
           "by_severity": {"high": [], "medium": [], "low": []}, "error": "boom"}
    md = render_markdown(agg)
    assert "ERROR" in md
    assert "boom" in md
    assert "_none_" in md


def test_render_json_is_plain_dict_copy():
    agg = {"generated_at": 1.0, "workers_total": 0, "parse_errors": 0, "findings": [],
           "by_severity": {"high": [], "medium": [], "low": []}}
    out = render_json(agg)
    assert out == agg
    assert out is not agg  # shallow copy, not the same object
    json.dumps(out)  # must be JSON-serializable


if __name__ == "__main__":
    import sys
    raise SystemExit(pytest.main([__file__, "-q"] + sys.argv[1:]))

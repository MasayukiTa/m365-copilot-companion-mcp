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
    load_transcript_all_assistant,
    load_transcript_final_answer,
    parse_findings_block,
    render_json,
    render_markdown,
    worker_final_text,
    _loads_tolerant,
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


# --- parse_findings_block: real-world recovery (BEGIN dropped/mangled by real workers) --------
# Verified against live transcript r6a5232d8_a0_w0: the worker reliably emits
# <<<END_FINDINGS>>> and a valid JSON array right before it, but drops <<<FINDINGS>>>.

def test_parse_findings_block_end_present_begin_missing_recovers():
    """THE KEY NEW CASE: no opening delimiter at all, but a valid array sits right
    before FINDINGS_END -- must be recovered instead of counted as a parse error."""
    items = [{"file": "a.py", "line": 12, "severity": "high", "title": "t", "detail": "d"}]
    text = ("あなたはレビューを行いました。\n"
             + json.dumps(items, ensure_ascii=False) + "\n" + FINDINGS_END + "\nDONE")
    assert FINDINGS_BEGIN not in text
    found, err = parse_findings_block(text)
    assert err is False
    assert found == items


def test_parse_findings_block_neither_delimiter_trailing_array_recovers():
    items = [{"file": "b.py", "line": 3, "severity": "medium", "title": "t2", "detail": ""}]
    text = "Here is what I found, no markers at all.\n" + json.dumps(items, ensure_ascii=False)
    found, err = parse_findings_block(text)
    assert err is False
    assert found == items


def test_parse_findings_block_end_present_no_array_before_it_is_parse_error():
    text = "I reviewed everything and found nothing worth reporting.\n" + FINDINGS_END + "\nDONE"
    found, err = parse_findings_block(text)
    assert found == []
    assert err is True


def test_parse_findings_block_bare_empty_array_before_end_is_not_an_error():
    text = "no issues found\n[]\n" + FINDINGS_END + "\nDONE"
    found, err = parse_findings_block(text)
    assert found == []
    assert err is False


def test_parse_findings_block_malformed_json_before_end_no_begin_is_parse_error():
    text = "results:\n[{not valid json at all}]\n" + FINDINGS_END + "\nDONE"
    found, err = parse_findings_block(text)
    assert found == []
    assert err is True


def test_parse_findings_block_last_resort_rejects_non_findings_array():
    """Neither delimiter present, and the last array in the text is just a list of ints
    -- must NOT be mistaken for a findings block."""
    text = "unrelated numbers mentioned in prose: [1, 2, 3, 4]"
    found, err = parse_findings_block(text)
    assert found == []
    assert err is True


def test_parse_findings_block_recovered_items_drop_non_dict_entries():
    items = [{"file": "a.py", "line": 1, "severity": "high", "title": "t", "detail": ""}, "stray"]
    text = json.dumps(items, ensure_ascii=False) + "\n" + FINDINGS_END + "\nDONE"
    found, err = parse_findings_block(text)
    assert err is False
    assert found == [items[0]]


# --- _loads_tolerant ----------------------------------------------------------------------
# Verified against live transcript r6a5232d8_a0_w1: the worker's findings array contains
# ".fleet\\gaia\\pipeline_task.log" with single, unescaped backslashes -- invalid JSON
# (json.loads raises "Invalid \\escape" on \\g and \\p). This is universal on this Windows
# product: every review will have Windows paths and regex snippets (\\d, \\g) in "detail"
# strings, so parse_findings_block must repair this instead of giving up.

def test_loads_tolerant_valid_json_unchanged():
    s = '[{"file": "a.py", "line": 1, "title": "t", "detail": "d"}]'
    assert _loads_tolerant(s) == [{"file": "a.py", "line": 1, "title": "t", "detail": "d"}]


def test_loads_tolerant_repairs_windows_path_single_backslashes():
    # Built explicitly with single backslashes in the Python source (via chr(92) concat
    # is unnecessary -- a raw string with one backslash per separator is itself invalid
    # JSON, which is exactly the live failure mode).
    bad = '[{"file": "C:\\Users\\x\\a.py", "title": "t"}]'
    # Sanity: confirm this really is invalid JSON before exercising the repair.
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)
    result = _loads_tolerant(bad)
    assert result == [{"file": "C:\\Users\\x\\a.py", "title": "t"}]


def test_loads_tolerant_repairs_regex_snippet_invalid_escape():
    bad = '[{"file": "a.py", "title": "t", "detail": "matches \\g and \\d here"}]'
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)
    result = _loads_tolerant(bad)
    assert result == [{"file": "a.py", "title": "t", "detail": "matches \\g and \\d here"}]


def test_loads_tolerant_fleet_gaia_path_from_live_transcript():
    """The exact live w1 substring: a Windows path with two backslash segments inside a
    JSON string value."""
    bad = '[{"file": "bench/x.py", "title": "t", "detail": "redirects to .fleet\\gaia\\pipeline_task.log"}]'
    result = _loads_tolerant(bad)
    assert result is not None
    assert result[0]["detail"] == "redirects to .fleet\\gaia\\pipeline_task.log"


def test_loads_tolerant_genuinely_broken_returns_none():
    assert _loads_tolerant('[{"file": "a.py", "title": "t"') is None  # unbalanced brackets
    assert _loads_tolerant("not json at all") is None
    assert _loads_tolerant("") is None


def test_loads_tolerant_preserves_valid_escapes_newline_unicode_quote():
    # Force the repair path to run (invalid \\g elsewhere in the same string) and confirm
    # legitimate \\n, \\uXXXX, and \\" escapes still decode correctly rather than being
    # corrupted into literal backslashes by the repair.
    s = '[{"file": "a.py", "title": "t", "detail": "line1\\nline2 \\u00e9 he said \\"hi\\" \\g bad"}]'
    with pytest.raises(json.JSONDecodeError):
        json.loads(s)
    result = _loads_tolerant(s)
    assert result is not None
    detail = result[0]["detail"]
    assert "line1\nline2" in detail
    assert "é" in detail
    assert 'he said "hi"' in detail
    assert "\\g" in detail  # the invalid escape survives as a literal backslash + g


# --- parse_findings_block: Windows-path invalid JSON end-to-end --------------------------

def test_parse_findings_block_recovers_windows_path_invalid_json():
    """End-to-end: a findings array between the delimiters contains an unescaped Windows
    path (invalid JSON per json.loads) -- must now recover instead of parse_error."""
    text = (FINDINGS_BEGIN
            + '\n[{"file": "C:\\Work\\some_project\\module\\example.py", "line": 1, '
              '"severity": "high", "title": "t", "detail": "d"}]\n'
            + FINDINGS_END)
    found, err = parse_findings_block(text)
    assert err is False
    assert len(found) == 1
    assert found[0]["file"] == "C:\\Work\\some_project\\module\\example.py"


def test_parse_findings_block_recovers_windows_path_when_begin_missing():
    """Same Windows-path repair, but through the (b) FALLBACK layer (FINDINGS_END present,
    FINDINGS_BEGIN dropped -- the other real-world recovery layer)."""
    items_text = '[{"file": "a.py", "title": "t", "detail": "log at .fleet\\gaia\\out.log"}]'
    text = "here is what I found\n" + items_text + "\n" + FINDINGS_END + "\nDONE"
    found, err = parse_findings_block(text)
    assert err is False
    assert len(found) == 1
    assert found[0]["detail"] == "log at .fleet\\gaia\\out.log"


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


# --- load_transcript_all_assistant --------------------------------------------------------
# Real live-fleet failure mode: 9 of 17 workers emitted their FINDINGS block in an EARLIER
# assistant turn, then continued with wrap-up prose in later turns. load_transcript_final_answer
# only sees the LAST turn and misses the block entirely; this function must see all of them.

def test_load_transcript_all_assistant_concatenates_all_turns(tmp_path):
    path = str(tmp_path / "transcripts" / "run_w7.jsonl")
    _write_transcript(path, turns=[
        {"turn": 1, "role": "user", "text": "please review", "ts": 2.0},
        {"turn": 1, "role": "assistant", "text": "turn one findings here", "ts": 3.0},
        {"turn": 2, "role": "user", "text": "continue", "ts": 4.0},
        {"turn": 2, "role": "assistant", "text": "turn two prose", "ts": 5.0},
        {"turn": 3, "role": "user", "text": "wrap up", "ts": 6.0},
        {"turn": 3, "role": "assistant", "text": "turn three wrap-up", "ts": 7.0},
    ])
    result = load_transcript_all_assistant(path)
    assert result == "turn one findings here\nturn two prose\nturn three wrap-up"


def test_load_transcript_all_assistant_missing_file(tmp_path):
    assert load_transcript_all_assistant(str(tmp_path / "nope.jsonl")) == ""


def test_load_transcript_all_assistant_corrupt_lines(tmp_path):
    path = str(tmp_path / "transcripts" / "run_w8.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json at all\n")
        f.write(json.dumps({"turn": 1, "role": "assistant", "text": "first"}) + "\n")
        f.write("garbage garbage\n")
        f.write(json.dumps({"turn": 2, "role": "assistant", "text": "second"}) + "\n")
    assert load_transcript_all_assistant(path) == "first\nsecond"


def test_load_transcript_all_assistant_empty_path():
    assert load_transcript_all_assistant("") == ""
    assert load_transcript_all_assistant(None) == ""


def test_load_transcript_all_assistant_no_assistant_turn(tmp_path):
    path = str(tmp_path / "transcripts" / "run_w9.jsonl")
    _write_transcript(path, turns=[{"turn": 1, "role": "user", "text": "hi", "ts": 2.0}])
    assert load_transcript_all_assistant(path) == ""


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


# --- worker_final_text: full transcript must win over truncated status.json fields ------------
# Verified against live status.json: worker "last" field is a ~600-char truncated snapshot
# that cuts off before the FINDINGS block; the full transcript's last assistant turn (2095
# chars in the real case) has the complete answer including the closing marker.

def test_worker_final_text_prefers_full_transcript_over_truncated_last(tmp_path):
    tpath = tmp_path / "transcripts" / "run_w5.jsonl"
    full_text = ("some preamble that goes on for a while... " * 20) + FINDINGS_END + " DONE"
    truncated_last = full_text[:600]  # what a truncated status.json snapshot would have
    assert FINDINGS_END not in truncated_last  # confirm the truncation actually lost the marker
    _write_transcript(str(tpath), turns=[{"turn": 1, "role": "assistant", "text": full_text}])
    w = {"display_result": "", "last": truncated_last, "transcript": str(tpath)}
    result = worker_final_text(w, str(tmp_path / "transcripts"))
    assert result == full_text
    assert FINDINGS_END in result


def test_worker_final_text_absolute_transcript_path_resolves(tmp_path):
    tpath = tmp_path / "somewhere_else" / "run_w6.jsonl"
    _write_transcript(str(tpath), turns=[{"turn": 1, "role": "assistant", "text": "abs answer"}])
    w = {"display_result": "", "last": "stale", "transcript": str(tpath)}
    # transcripts_dir deliberately points elsewhere -- the absolute path must still resolve.
    assert worker_final_text(w, str(tmp_path / "unrelated_dir")) == "abs answer"


def test_worker_final_text_missing_transcript_falls_back(tmp_path):
    w = {"display_result": "", "last": "fallback text", "transcript": "does_not_exist.jsonl"}
    assert worker_final_text(w, str(tmp_path)) == "fallback text"


def test_worker_final_text_both_missing_returns_empty(tmp_path):
    w = {"display_result": "", "last": "", "transcript": ""}
    assert worker_final_text(w, str(tmp_path)) == ""


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


def test_aggregate_recovers_findings_from_real_world_end_no_begin_transcripts(tmp_path):
    """End-to-end proof on a realistic final-snapshot status.json: every worker's
    transcript has FINDINGS_END but no FINDINGS_BEGIN (the real failure mode), and the
    status.json "last" field is a truncated stub that lost the findings block entirely.
    aggregate() must still recover the genuine findings with parse_errors == 0."""
    status_path = tmp_path / "status.json"
    transcripts_dir = tmp_path / "transcripts"

    def _worker(name, sev, file_):
        items = [{"file": file_, "line": 7, "severity": sev, "title": "issue in " + file_,
                  "detail": "found via live-shaped transcript"}]
        full_text = ("worker %s reviewed the assigned files and here is the result.\n" % name
                     + json.dumps(items, ensure_ascii=False) + "\n" + FINDINGS_END + "\nDONE")
        tpath = transcripts_dir / (name + ".jsonl")
        _write_transcript(str(tpath), name=name, turns=[
            {"turn": 1, "role": "assistant", "text": full_text[:600]},  # mid-run stub
            {"turn": 2, "role": "assistant", "text": full_text},         # final untruncated turn
        ])
        return {
            "name": name, "goal": "review group " + name, "outcome": "DONE",
            "reason": "refuter#1: UPHELD", "verified": True,
            "display_result": "", "last": full_text[:600],  # truncated, matches live status.json
            "transcript": str(tpath),
        }, items

    w0, items0 = _worker("w0", "high", "a.py")
    w1, items1 = _worker("w1", "medium", "b.py")
    w2, items2 = _worker("w2", "low", "c.py")
    workers = [w0, w1, w2]

    status_path.write_text(json.dumps(_status_with_workers(workers), ensure_ascii=False),
                            encoding="utf-8")

    agg = aggregate(str(status_path), str(transcripts_dir), now=999.0)

    assert agg["parse_errors"] == 0
    assert len(agg["findings"]) == 3
    assert len(agg["by_severity"]["high"]) == 1
    assert len(agg["by_severity"]["medium"]) == 1
    assert len(agg["by_severity"]["low"]) == 1
    assert agg["by_severity"]["high"][0]["file"] == "a.py"


def test_aggregate_recovers_findings_block_from_earlier_assistant_turn(tmp_path):
    """THE KEY NEW CASE (the live 9-worker gap): a worker's transcript has the FINDINGS
    block in an EARLIER assistant turn, and only unrelated wrap-up prose in the LAST
    turn. worker_final_text/load_transcript_final_answer alone would see only the last
    turn and miss the block entirely (parse_error). aggregate() must now recover it by
    falling back to load_transcript_all_assistant."""
    status_path = tmp_path / "status.json"
    transcripts_dir = tmp_path / "transcripts"

    items = [{"file": "early.py", "line": 42, "severity": "high",
              "title": "found in an earlier turn", "detail": "recovered via full scan"}]
    tpath = transcripts_dir / "w_early.jsonl"
    _write_transcript(str(tpath), name="w_early", turns=[
        {"turn": 1, "role": "user", "text": "please review", "ts": 1.0},
        {"turn": 1, "role": "assistant", "text": _findings_block(items), "ts": 2.0},
        {"turn": 2, "role": "user", "text": "anything else?", "ts": 3.0},
        {"turn": 2, "role": "assistant",
         "text": "That's everything, thanks for reviewing with me today.", "ts": 4.0},
    ])
    workers = [{
        "name": "w_early", "goal": "review group early", "outcome": "DONE",
        "reason": "refuter#1: UPHELD", "verified": True,
        "display_result": "", "last": "That's everything, thanks for reviewing with me today.",
        "transcript": str(tpath),
    }]
    status_path.write_text(json.dumps(_status_with_workers(workers), ensure_ascii=False),
                            encoding="utf-8")

    agg = aggregate(str(status_path), str(transcripts_dir), now=1.0)

    assert agg["parse_errors"] == 0
    assert len(agg["findings"]) > 0
    assert agg["findings"][0]["file"] == "early.py"
    assert agg["findings"][0]["worker"] == "w_early"
    assert agg["by_severity"]["high"][0]["title"] == "found in an earlier turn"


def test_aggregate_fast_path_last_turn_block_still_works_no_full_scan_needed(tmp_path):
    """No regression: a worker whose block IS in the last assistant turn must still be
    parsed via the fast path (worker_final_text), without needing the full-transcript
    fallback. Covers the 4 workers that already worked before this fix."""
    status_path = tmp_path / "status.json"
    transcripts_dir = tmp_path / "transcripts"

    items = [{"file": "last.py", "line": 5, "severity": "medium",
              "title": "found in last turn", "detail": ""}]
    tpath = transcripts_dir / "w_last.jsonl"
    _write_transcript(str(tpath), name="w_last", turns=[
        {"turn": 1, "role": "assistant", "text": "still working on it", "ts": 1.0},
        {"turn": 2, "role": "assistant", "text": _findings_block(items), "ts": 2.0},
    ])
    workers = [{
        "name": "w_last", "goal": "review group last", "outcome": "DONE",
        "reason": "", "verified": True,
        "display_result": "", "last": "",
        "transcript": str(tpath),
    }]
    status_path.write_text(json.dumps(_status_with_workers(workers), ensure_ascii=False),
                            encoding="utf-8")

    agg = aggregate(str(status_path), str(transcripts_dir), now=1.0)

    assert agg["parse_errors"] == 0
    assert len(agg["findings"]) == 1
    assert agg["findings"][0]["file"] == "last.py"
    assert agg["findings"][0]["worker"] == "w_last"


def test_aggregate_no_findings_in_any_turn_is_still_parse_error(tmp_path):
    """A worker with no findings-shaped array anywhere in ANY assistant turn (it
    genuinely found nothing) must remain a parse_error -- the full-transcript fallback
    must not manufacture a false recovery."""
    status_path = tmp_path / "status.json"
    transcripts_dir = tmp_path / "transcripts"

    tpath = transcripts_dir / "w_none.jsonl"
    _write_transcript(str(tpath), name="w_none", turns=[
        {"turn": 1, "role": "assistant", "text": "looking at the files now", "ts": 1.0},
        {"turn": 2, "role": "assistant",
         "text": "I reviewed everything and found nothing worth reporting.", "ts": 2.0},
    ])
    workers = [{
        "name": "w_none", "goal": "review group none", "outcome": "DONE",
        "reason": "", "verified": None,
        "display_result": "", "last": "",
        "transcript": str(tpath),
    }]
    status_path.write_text(json.dumps(_status_with_workers(workers), ensure_ascii=False),
                            encoding="utf-8")

    agg = aggregate(str(status_path), str(transcripts_dir), now=1.0)

    assert agg["parse_errors"] == 1
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


# --- P2 piece A: behavioral_verdict rendering (DEMONSTRATED marker + ranking) ----------------

def _finding(**over):
    base = {"file": "a.py", "line": 1, "severity": "high", "title": "t", "detail": "d",
            "worker": "w0", "reason": "", "verified": None}
    base.update(over)
    return base


def test_render_markdown_reproduced_marked_demonstrated_and_ranked_first():
    not_reproduced = _finding(title="confirmed-not-reproduced", verify_verdict="confirmed",
                               behavioral_verdict="not_reproduced")
    reproduced = _finding(title="confirmed-reproduced", verify_verdict="confirmed",
                           behavioral_verdict="reproduced")
    plain_confirmed = _finding(title="confirmed-plain", verify_verdict="confirmed")
    agg = {
        "generated_at": 1.0, "workers_total": 1, "parse_errors": 0,
        "findings": [not_reproduced, reproduced, plain_confirmed],
        "by_severity": {"high": [not_reproduced, reproduced, plain_confirmed],
                         "medium": [], "low": []},
    }
    md = render_markdown(agg)
    assert "DEMONSTRATED" in md
    assert "reasoned-but-not-reproduced" in md

    # ranking: the reproduced finding's line must come before both other findings' lines
    demonstrated_idx = md.index("confirmed-reproduced")
    plain_idx = md.index("confirmed-plain")
    not_repro_idx = md.index("confirmed-not-reproduced")
    assert demonstrated_idx < plain_idx
    assert demonstrated_idx < not_repro_idx


def test_render_markdown_behavioral_header_summary_present_when_any_finding_carries_it():
    agg = {
        "generated_at": 1.0, "workers_total": 1, "parse_errors": 0,
        "findings": [_finding(behavioral_verdict="reproduced"),
                     _finding(title="t2", behavioral_verdict="not_reproduced"),
                     _finding(title="t3", behavioral_verdict="inconclusive")],
        "by_severity": {"high": [], "medium": [], "low": []},
    }
    md = render_markdown(agg)
    assert "behavioral verification: reproduced=1 (DEMONSTRATED) not_reproduced=1 inconclusive=1" in md


def test_render_markdown_no_behavioral_header_when_no_finding_carries_verdict():
    agg = {
        "generated_at": 1.0, "workers_total": 1, "parse_errors": 0,
        "findings": [_finding()],
        "by_severity": {"high": [_finding()], "medium": [], "low": []},
    }
    md = render_markdown(agg)
    assert "behavioral verification" not in md
    assert "DEMONSTRATED" not in md


def test_render_markdown_behavioral_evidence_printed():
    agg = {
        "generated_at": 1.0, "workers_total": 1, "parse_errors": 0,
        "findings": [_finding(behavioral_verdict="reproduced",
                               behavioral_evidence="実際に例外が発生した")],
        "by_severity": {"high": [_finding(behavioral_verdict="reproduced",
                                           behavioral_evidence="実際に例外が発生した")],
                         "medium": [], "low": []},
    }
    md = render_markdown(agg)
    assert "実際に例外が発生した" in md


def test_render_json_adds_behavioral_summary_when_present():
    agg = {"generated_at": 1.0, "workers_total": 0, "parse_errors": 0,
           "findings": [_finding(behavioral_verdict="reproduced"),
                        _finding(title="t2", behavioral_verdict="not_reproduced")],
           "by_severity": {"high": [], "medium": [], "low": []}}
    out = render_json(agg)
    assert out["behavioral_summary"] == {"reproduced": 1, "not_reproduced": 1, "inconclusive": 0}


def test_render_json_no_behavioral_summary_when_absent():
    agg = {"generated_at": 1.0, "workers_total": 0, "parse_errors": 0,
           "findings": [_finding()],
           "by_severity": {"high": [], "medium": [], "low": []}}
    out = render_json(agg)
    assert "behavioral_summary" not in out


# --- P3 piece C: loop_meta / completeness_gaps rendering (purely additive) ------------------

def _base_agg(**over):
    agg = {"generated_at": 1.0, "workers_total": 0, "parse_errors": 0, "findings": [],
           "by_severity": {"high": [], "medium": [], "low": []}}
    agg.update(over)
    return agg


def test_render_markdown_no_p3_keys_renders_identically_to_before():
    """The core Piece C contract: an agg dict with NEITHER "loop_meta" nor
    "completeness_gaps" must render byte-identically to the pre-P3 shape."""
    agg = _base_agg()
    md = render_markdown(agg)
    assert "loop" not in md.lower()
    assert "completeness" not in md.lower()


def test_render_json_no_p3_keys_is_unchanged_shallow_copy():
    agg = _base_agg()
    out = render_json(agg)
    assert out == agg
    assert "loop_meta" not in out
    assert "completeness_gaps" not in out


def test_render_markdown_loop_meta_dry_stop():
    agg = _base_agg(loop_meta={"rounds_run": 2, "max_rounds": 5, "stopped_reason": "dry",
                                "dry_rounds_target": 2, "unique_findings": 3})
    md = render_markdown(agg)
    assert "loop: 2/5 round(s) run, stopped: dry" in md
    assert "3 unique finding(s)" in md
    assert "NOTE: stopped because the max-rounds cap" not in md


def test_render_markdown_loop_meta_max_rounds_stop_prints_no_silent_cap_note():
    agg = _base_agg(loop_meta={"rounds_run": 3, "max_rounds": 3, "stopped_reason": "max_rounds",
                                "dry_rounds_target": 2, "unique_findings": 5})
    md = render_markdown(agg)
    assert "loop: 3/3 round(s) run, stopped: max_rounds" in md
    assert "NOTE: stopped because the max-rounds cap was reached" in md


def test_render_markdown_completeness_gaps_all_present():
    agg = _base_agg(completeness_gaps={
        "missing_dimensions": ["test_hygiene"],
        "missing_files": ["c.py"],
        "unverified_claims": ["claim about a.py was never actually checked"],
    })
    md = render_markdown(agg)
    assert "completeness critic:" in md
    assert "test_hygiene" in md
    assert "c.py" in md
    assert "claim about a.py was never actually checked" in md


def test_render_markdown_completeness_gaps_all_empty_says_no_gaps():
    agg = _base_agg(completeness_gaps={
        "missing_dimensions": [], "missing_files": [], "unverified_claims": []})
    md = render_markdown(agg)
    assert "completeness critic: no gaps identified" in md


def test_render_markdown_no_completeness_gaps_key_omits_section():
    agg = _base_agg()
    md = render_markdown(agg)
    assert "completeness critic" not in md


def test_render_markdown_no_baseline_diff_key_renders_identically_to_before():
    """agg without a "baseline_diff" key must render byte-identical to before this feature
    existed -- same contract as the loop_meta/completeness_gaps regression guard above."""
    agg = _base_agg()
    md = render_markdown(agg)
    assert "baseline" not in md.lower()


def test_render_markdown_baseline_diff_counts_line():
    agg = _base_agg(baseline_diff={
        "new": [{"file": "a.py", "line": 1, "title": "New one", "severity": "high"}],
        "regressed": [{"file": "b.py", "line": 2, "title": "Regressed one", "severity": "medium"}],
        "resolved": [{"file": "c.py", "line": 3, "title": "Resolved one", "severity": "low"}],
        "unchanged": [],
    })
    md = render_markdown(agg)
    assert "baseline diff: new=1 regressed=1 resolved=1 unchanged=0" in md


def test_render_markdown_baseline_diff_lists_new_and_regressed():
    agg = _base_agg(baseline_diff={
        "new": [{"file": "a.py", "line": 1, "title": "New one", "severity": "high"}],
        "regressed": [{"file": "b.py", "line": 2, "title": "Regressed one", "severity": "medium"}],
        "resolved": [],
        "unchanged": [],
    })
    md = render_markdown(agg)
    assert "## Baseline diff: new / regressed findings (2)" in md
    assert "### New (1)" in md
    assert "a.py:1:New one:high" in md
    assert "### Regressed (1)" in md
    assert "b.py:2:Regressed one:medium" in md


def test_render_markdown_baseline_diff_only_resolved_unchanged_omits_detail_section():
    """Counts line still shows, but the actionable detail section is omitted when there's
    nothing new/regressed to act on."""
    agg = _base_agg(baseline_diff={
        "new": [], "regressed": [],
        "resolved": [{"file": "a.py", "line": 1, "title": "Fixed", "severity": "high"}],
        "unchanged": [{"file": "b.py", "line": 2, "title": "Still fine", "severity": "low"}],
    })
    md = render_markdown(agg)
    assert "baseline diff: new=0 regressed=0 resolved=1 unchanged=1" in md
    assert "## Baseline diff" not in md


def test_render_markdown_baseline_diff_empty_dict_is_falsy_omits_section():
    agg = _base_agg(baseline_diff={"new": [], "regressed": [], "resolved": [], "unchanged": []})
    md = render_markdown(agg)
    assert "baseline diff: new=0 regressed=0 resolved=0 unchanged=0" in md
    assert "## Baseline diff" not in md


def test_render_json_carries_baseline_diff_verbatim():
    agg = _base_agg(baseline_diff={
        "new": [{"file": "a.py", "line": 1, "title": "T", "severity": "high"}],
        "regressed": [], "resolved": [], "unchanged": [],
    })
    out = render_json(agg)
    assert out["baseline_diff"] == agg["baseline_diff"]
    json.dumps(out)


def test_render_json_carries_loop_meta_and_completeness_gaps_verbatim():
    """render_json needs NO extra code for this -- it's already a shallow dict copy, so any
    key present on `agg` (including the new P3 ones) is carried over automatically."""
    agg = _base_agg(
        loop_meta={"rounds_run": 2, "max_rounds": 3, "stopped_reason": "dry",
                    "dry_rounds_target": 2, "unique_findings": 1},
        completeness_gaps={"missing_dimensions": ["security"], "missing_files": [],
                            "unverified_claims": []})
    out = render_json(agg)
    assert out["loop_meta"] == agg["loop_meta"]
    assert out["completeness_gaps"] == agg["completeness_gaps"]
    json.dumps(out)  # still JSON-serializable


if __name__ == "__main__":
    import sys
    raise SystemExit(pytest.main([__file__, "-q"] + sys.argv[1:]))

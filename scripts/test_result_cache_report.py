# -*- coding: utf-8 -*-
"""scripts/result_cache_report.py: read-only, honest about thin data, states its threshold."""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import result_cache_report as R  # noqa: E402


def test_same_fingerprint_pairs_exclude_changed_and_legacy_rows(tmp_path, capsys):
    base = _row(1, hit=True)
    fp = {"kind": "file", "mtime_ns": 1, "size": 4}
    same = dict(base, fingerprint=fp, previous_fingerprint=fp,
                result_hash="new", previous_result_hash="old")
    changed = dict(same, fingerprint=dict(fp, size=5))
    _write(str(tmp_path), [base, same, changed])
    assert R.main(["--fleet-dir", str(tmp_path), "--same-fingerprint", "--by-tool", "--format", "json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["pairs"] == 1 and summary["differed"] == 1
    assert summary["by_tool"]["read_file"]["differed_share"] == 1
    assert R.main(["--fleet-dir", str(tmp_path), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["calls"] == 3


def _row(i, run="r1", tool="read_file", hit=False, differed=False, saved=0.5):
    return {"v": 1, "ts": 1000.0 + i, "run_id": run, "mode": "shadow", "tool": tool,
            "arg_hash": "h%d" % i, "read_only": True, "hit": hit,
            "matched": bool(hit and not differed), "differed": bool(hit and differed),
            "age_s": 1.0 if hit else None, "saved_s": saved if hit else None}


def _write(dirpath, rows, junk=0, old=()):
    os.makedirs(dirpath, exist_ok=True)
    p = os.path.join(dirpath, R.LOG_NAME)
    with open(p, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
        for _ in range(junk):
            fh.write("{not json\n")
    if old:
        with open(p + ".1", "w", encoding="utf-8") as fh:
            for r in old:
                fh.write(json.dumps(r) + "\n")
    return p


def _enough(hit_every, differ_every=0):
    rows = []
    for i in range(R.MIN_CALLS):
        hit = (i % hit_every == 0)
        rows.append(_row(i, run="r%d" % (i % R.MIN_RUNS), hit=hit,
                         differed=bool(hit and differ_every and (i // hit_every) % differ_every == 0)))
    return rows


def test_an_empty_or_missing_log_is_insufficient_not_an_error(tmp_path):
    rows, bad = R.read_rows(str(tmp_path / R.LOG_NAME))
    assert rows == [] and bad == 0
    label, unmet = R.verdict(R.summarise(rows))
    assert label == R.INSUFFICIENT and len(unmet) == 3


def test_the_rotated_copy_is_read_first_and_junk_is_counted(tmp_path):
    p = _write(str(tmp_path), [_row(2)], junk=2, old=[_row(1)])
    rows, bad = R.read_rows(p)
    assert [r["ts"] for r in rows] == [1001.0, 1002.0] and bad == 2


def test_counts_shares_saved_time_and_per_tool_split():
    rows = [_row(0), _row(1, hit=True, saved=2.0), _row(2, hit=True, differed=True, saved=1.0),
            _row(3, tool="list_directory"), _row(4, tool="list_directory", hit=True, saved=0.25)]
    s = R.summarise(rows)
    assert (s["calls"], s["hits"], s["matched"], s["differed"]) == (5, 3, 2, 1)
    assert s["hit_share"] == 0.6 and abs(s["differed_share"] - 1 / 3) < 1e-9
    assert s["saved_s"] == 3.25
    assert s["by_tool"]["read_file"] == {"calls": 3, "hits": 2, "differed": 1, "saved_s": 3.0}
    assert s["by_tool"]["list_directory"]["hits"] == 1


def test_enough_data_with_stable_repeats_is_worth_a_serving_test_not_a_switch():
    label, unmet = R.verdict(R.summarise(_enough(hit_every=4)))        # 25% hit, none differ
    assert label == R.WORTH_A_SERVING_TEST and unmet == []
    text = R.render(R.summarise(_enough(4)), label, unmet)
    assert "Do not switch serving on from shadow rows alone" in text


def test_repeats_whose_answers_change_are_unsafe_to_serve():
    label, _ = R.verdict(R.summarise(_enough(hit_every=4, differ_every=2)))   # half the hits differ
    assert label == R.UNSAFE_TO_SERVE


def test_too_few_repeats_is_nothing_to_gain():
    # 500 calls, exactly MIN_HITS (20) hits: 4%, under HIT_WORTH, over every other requirement.
    rows = [_row(i, run="r%d" % (i % R.MIN_RUNS), hit=(i % 25 == 0)) for i in range(500)]
    s = R.summarise(rows)
    assert s["hits"] >= R.MIN_HITS and s["hit_share"] < R.HIT_WORTH
    assert R.verdict(s)[0] == R.NOTHING_TO_GAIN


def test_every_requirement_is_checked_separately():
    base = _enough(4)
    assert R.verdict(R.summarise([dict(r, run_id="only") for r in base]))[0] == R.INSUFFICIENT
    assert R.verdict(R.summarise(base[:R.MIN_CALLS - 1]))[0] == R.INSUFFICIENT
    no_hits = [dict(r, hit=False, matched=False) for r in base]
    assert R.verdict(R.summarise(no_hits))[0] == R.INSUFFICIENT


def test_the_text_states_the_threshold_and_the_limit_of_shadow_rows():
    s = R.summarise([_row(0)])
    text = R.render(s, *R.verdict(s))
    assert "Threshold: >= 200 read-only calls, >= 3 runs, >= 20 hits." in text
    assert "not time that was saved" in text


def test_main_reads_only_and_prints_both_formats(tmp_path, capsys):
    fleet = str(tmp_path / "fleet")
    p = _write(fleet, [_row(i) for i in range(3)])
    before = os.path.getsize(p)
    assert R.main(["--fleet-dir", fleet]) == 0
    assert "INSUFFICIENT" in capsys.readouterr().out
    assert R.main(["--fleet-dir", fleet, "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "INSUFFICIENT" and out["summary"]["rows"] == 3
    assert os.path.getsize(p) == before and sorted(os.listdir(fleet)) == [R.LOG_NAME]


def test_the_report_and_the_recorder_agree_on_the_file_name():
    from relay import result_cache
    assert os.path.basename(result_cache.LOG) == R.LOG_NAME


def test_a_real_recorder_row_is_understood_by_the_report(tmp_path, monkeypatch):
    from relay import result_cache as RC
    RC._reset_for_test()
    fleet = tmp_path / "fleet"
    monkeypatch.setattr(RC, "LOG", str(fleet / R.LOG_NAME))
    p = tmp_path / "s.txt"
    p.write_text("result_cache=shadow\n", encoding="utf-8")
    RC.shadow_observe("read_file", {"path": "a"}, "same", True, 0.4, run_id="rx",
                      settings_path=str(p), now=100.0)
    RC.shadow_observe("read_file", {"path": "a"}, "same", True, 0.4, run_id="rx",
                      settings_path=str(p), now=101.0)
    RC.shadow_observe("read_file", {"path": "a"}, "changed", True, 0.6, run_id="rx",
                      settings_path=str(p), now=102.0)
    rows, bad = R.read_rows(str(fleet / R.LOG_NAME))
    s = R.summarise(rows)
    assert bad == 0 and (s["calls"], s["hits"], s["matched"], s["differed"]) == (3, 2, 1, 1)
    assert s["saved_s"] == 1.0 and s["runs"] == 1
    RC._reset_for_test()

# -*- coding: utf-8 -*-
"""scripts/team_formation_report.py: read-only, honest about thin data, states its threshold."""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import team_formation_report as R  # noqa: E402


def _row(i, run="r1", differs=False, known=True, steps=3, depth=1, rules=()):
    return {"v": 1, "ts": 1000.0 + i, "run_id": run, "differs": differs, "profile_known": known,
            "depth": depth, "rules": list(rules), "shape": {"steps": steps},
            "actual": {"roles": {"subtask": steps, "aggregator": 1}, "members": steps + 1},
            "proposed": {"roles": {}, "members": steps + 2 if differs else steps + 1}}


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


def test_an_empty_or_missing_log_is_insufficient_not_an_error(tmp_path):
    rows, bad = R.read_rows(str(tmp_path / R.LOG_NAME))
    assert rows == [] and bad == 0
    label, unmet = R.verdict(R.summarise(rows))
    assert label == R.INSUFFICIENT and len(unmet) == 4


def test_the_rotated_copy_is_read_first_and_junk_is_counted(tmp_path):
    p = _write(str(tmp_path), [_row(2)], junk=2, old=[_row(1)])
    rows, bad = R.read_rows(p)
    assert [r["ts"] for r in rows] == [1001.0, 1002.0] and bad == 2


def test_the_summary_counts_differences_rules_depths_and_members():
    rows = [_row(i, differs=(i % 2 == 0), rules=["fold_tiny"] if i % 2 == 0 else [],
                 depth=1 + (i % 2), steps=2 + i % 4, run="r%d" % (i % 6)) for i in range(10)]
    rows.append(_row(99, known=False))
    s = R.summarise(rows)
    assert (s["splits"], s["differs"], s["profile_known"]) == (11, 5, 10)
    assert s["rules"] == {"fold_tiny": 5} and s["members_added_total"] == 5
    assert s["by_depth"] == {"1": {"splits": 6, "differs": 5}, "2": {"splits": 5, "differs": 0}}
    assert s["shapes"] == 4 and s["runs"] == 6


def test_the_verdict_needs_all_four_requirements():
    rows = [_row(i, differs=True, run="r%d" % (i % 5), steps=2 + i % 3) for i in range(60)]
    s = R.summarise(rows)
    assert R.verdict(s) == (R.WORTH_A_PAIRED_TEST, [])
    for knob, value in (("splits", 59), ("profile_known", 29), ("runs", 4), ("shapes", 2)):
        thin = dict(s, **{knob: value})
        label, unmet = R.verdict(thin)
        assert label == R.INSUFFICIENT and len(unmet) == 1


def test_enough_data_with_few_differences_is_nothing_to_gain():
    rows = [_row(i, differs=(i == 0), run="r%d" % (i % 5), steps=2 + i % 3) for i in range(60)]
    assert R.verdict(R.summarise(rows)) == (R.NOTHING_TO_GAIN, [])


def test_main_reads_only_and_prints_both_formats(tmp_path, capsys):
    p = _write(str(tmp_path), [_row(i) for i in range(3)])
    before = os.stat(p).st_mtime_ns
    assert R.main(["--fleet-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Verdict: INSUFFICIENT" in out and "Threshold:" in out
    assert R.main(["--fleet-dir", str(tmp_path), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["verdict"] == "INSUFFICIENT" and data["summary"]["splits"] == 3
    assert os.stat(p).st_mtime_ns == before
    assert sorted(os.listdir(str(tmp_path))) == [R.LOG_NAME]

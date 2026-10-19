# -*- coding: utf-8 -*-
"""scripts/load_routing_report.py: read-only, honest about thin data, states its threshold."""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import load_routing_report as R  # noqa: E402


def _row(i, run="r1", differs=False, comparable=True, tight=False, queue_len=3,
         roles=("producer", "subtask"), reason="margins loose: most expensive known first"):
    cands = [{"name": "w%d" % k, "role": r, "turns": (5.0 if r != "aggregator" else None)}
             for k, r in enumerate(roles)]
    return {"v": 1, "ts": 1000.0 + i, "run_id": run, "differs": differs, "comparable": comparable,
            "tight": tight, "queue_len": queue_len, "reason": reason, "candidates": cands}


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


def test_only_comparable_rows_count_toward_the_differ_share():
    rows = [_row(i, differs=(i % 2 == 0)) for i in range(10)]
    rows += [_row(100 + i, comparable=False, differs=False) for i in range(10)]
    rows += [_row(200, queue_len=1, comparable=False)]
    s = R.summarise(rows)
    assert (s["rows"], s["had_choice"], s["comparable"], s["differs"]) == (21, 20, 10, 5)
    assert s["differ_share"] == 0.5


def test_margins_are_split_into_tight_and_loose():
    rows = [_row(0, tight=True, differs=True), _row(1, tight=True), _row(2, tight=False)]
    s = R.summarise(rows)
    assert s["by_margin"] == {"tight": {"comparable": 2, "differs": 1},
                              "loose": {"comparable": 1, "differs": 0}}


def _enough(differ_every):
    rows = []
    for i in range(R.MIN_CHOICE_ROWS):
        rows.append(_row(i, run="r%d" % (i % R.MIN_RUNS), differs=(i % differ_every == 0)))
    return rows


def test_enough_data_and_few_differences_is_nothing_to_gain():
    label, unmet = R.verdict(R.summarise(_enough(differ_every=50)))     # 2% differ
    assert label == R.NOTHING_TO_GAIN and unmet == []


def test_enough_data_and_many_differences_asks_for_a_paired_test_not_a_switch():
    label, unmet = R.verdict(R.summarise(_enough(differ_every=2)))      # 50% differ
    assert label == R.WORTH_A_PAIRED_TEST and unmet == []
    text = R.render(R.summarise(_enough(2)), label, unmet)
    assert "Do not switch routing on from shadow rows alone" in text


def test_every_requirement_is_checked_separately():
    base = _enough(2)
    one_run = [dict(r, run_id="only") for r in base]
    assert R.verdict(R.summarise(one_run))[0] == R.INSUFFICIENT
    one_role = [dict(r, candidates=[c for c in r["candidates"] if c["role"] == "producer"])
                for r in base]
    assert R.verdict(R.summarise(one_role))[0] == R.INSUFFICIENT
    few = base[:R.MIN_CHOICE_ROWS - 1]
    assert R.verdict(R.summarise(few))[0] == R.INSUFFICIENT
    no_comparable = [dict(r, comparable=False) for r in base]
    assert R.verdict(R.summarise(no_comparable))[0] == R.INSUFFICIENT


def test_the_text_states_the_threshold_and_the_limit_of_shadow_rows():
    text = R.render(R.summarise([_row(0)]), *R.verdict(R.summarise([_row(0)])))
    assert "Threshold: >= 200 rows with a choice, >= 30 comparable, >= 5 runs, >= 2 roles" in text
    assert "nothing here shows the router would have done better" in text


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
    from relay import load_routing
    assert os.path.basename(load_routing.LOG) == R.LOG_NAME


def test_a_real_recorder_row_is_understood_by_the_report(tmp_path, monkeypatch):
    from relay import load_routing as LR
    LR._reset_for_test()
    fleet = tmp_path / "fleet"
    monkeypatch.setattr(LR, "LOG", str(fleet / R.LOG_NAME))
    monkeypatch.setattr(LR, "profiles", lambda now=None: {
        "producer": {"turns": {"mean": 20.0, "label": "poor"}, "confidence": "low"},
        "subtask": {"turns": {"mean": 3.0, "label": "good"}, "confidence": "low"}})
    p = tmp_path / "s.txt"
    p.write_text("fanout_load_routing=shadow\n", encoding="utf-8")
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(p))

    def w(name, role):
        return type("W", (), {"name": name, "task_envelope": type("E", (), {"role": role})()})()

    LR.shadow_assign(w("w0", "producer"), [w("w1", "subtask")], 1, 2, 2, run_id="rx")
    rows, bad = R.read_rows(str(fleet / R.LOG_NAME))
    s = R.summarise(rows)
    assert bad == 0 and (s["rows"], s["had_choice"], s["comparable"], s["differs"]) == (1, 1, 1, 1)
    assert s["known_roles"] == 2 and s["runs"] == 1
    LR._reset_for_test()

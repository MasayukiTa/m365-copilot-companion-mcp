# -*- coding: utf-8 -*-
"""scripts/peer_requests_report.py: read-only, honest about thin data, states its threshold."""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import peer_requests_report as R  # noqa: E402


def _row(i, run="r1", request=0, offer=0, claim=0):
    return {"v": 1, "ts": 1000.0 + i, "run_id": run, "mode": "shadow", "queued": 1,
            "counts": {"request": request, "offer": offer, "claim_work": claim}}


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
    s = R.summarize(rows)
    assert s["verdict"] == R.INSUFFICIENT and s["ticks"] == 0 and len(s["unmet"]) == 3


def test_the_rotated_copy_is_read_first_and_junk_is_counted(tmp_path):
    p = _write(str(tmp_path), [_row(2)], junk=2, old=[_row(1)])
    rows, bad = R.read_rows(p)
    assert [r["ts"] for r in rows] == [1001.0, 1002.0] and bad == 2


def test_the_summary_counts_requests_offers_claims_and_claimable():
    # 10 rows: even -> request+offer+claim (claimable), odd -> request only.
    rows = []
    for i in range(10):
        if i % 2 == 0:
            rows.append(_row(i, run="r%d" % (i % 6), request=1, offer=1, claim=1))
        else:
            rows.append(_row(i, run="r%d" % (i % 6), request=1))
    s = R.summarize(rows)
    assert s["ticks"] == 10 and s["runs"] == 6
    assert s["ticks_with_request"] == 10 and s["ticks_with_offer"] == 5
    assert s["claimable_ticks"] == 5 and s["claimable_share"] == 0.5
    assert s["requests"] == 10 and s["offers"] == 5 and s["claims"] == 5


def test_the_verdict_needs_all_three_requirements():
    rows = [_row(i, run="r%d" % (i % 5), request=1, offer=1, claim=1) for i in range(60)]
    s = R.summarize(rows)
    assert s["verdict"] == R.WORTH_BOARD_PROMOTION and s["unmet"] == []
    # too few ticks
    assert R.summarize(rows[:59])["verdict"] == R.INSUFFICIENT
    # too few runs
    one_run = [_row(i, run="only", request=1, offer=1, claim=1) for i in range(60)]
    sr = R.summarize(one_run)
    assert sr["verdict"] == R.INSUFFICIENT and any("runs" in u for u in sr["unmet"])
    # too few ticks with a request
    no_req = [_row(i, run="r%d" % (i % 5), offer=1) for i in range(60)]
    snr = R.summarize(no_req)
    assert snr["verdict"] == R.INSUFFICIENT and any("request" in u for u in snr["unmet"])


def test_enough_data_with_few_claimable_is_nothing_to_gain():
    rows = [_row(i, run="r%d" % (i % 5), request=1,
                 offer=(1 if i == 0 else 0), claim=(1 if i == 0 else 0)) for i in range(60)]
    s = R.summarize(rows)
    assert s["verdict"] == R.NOTHING_TO_GAIN and s["unmet"] == []


def test_main_reads_only_and_prints_both_formats(tmp_path, capsys):
    p = _write(str(tmp_path), [_row(i, request=1, offer=1, claim=1) for i in range(3)])
    before = os.stat(p).st_mtime_ns
    assert R.main(["--fleet-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "verdict" in out and "peer work requests" in out
    assert R.main(["--fleet-dir", str(tmp_path), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["verdict"] == "INSUFFICIENT" and data["ticks"] == 3
    assert os.stat(p).st_mtime_ns == before
    assert sorted(os.listdir(str(tmp_path))) == [R.LOG_NAME]

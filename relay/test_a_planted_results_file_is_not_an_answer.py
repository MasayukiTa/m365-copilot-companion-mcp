# -*- coding: utf-8 -*-
"""A hermetic case folder, end to end: raw input in a workbook, an OLD results workbook, and a
PLANTED fake-results file -- the exact shape job 065d8c1c4a14 exploited (paste the folder
path, finish in one turn, report the folder's pre-made numbers).

TWO ANSWERS ARE BUILT FROM THE SAME FOLDER:

  * the PLANTED answer copies a correlation out of the fake-results file WITHOUT computing
    anything -- its 'compute outputs' are empty, which is what an attributed ledger would show
    for a one-turn no-op DONE. It must be REJECTED.
  * the RECOMPUTED answer runs a real correlation over the RAW table in this test (standing in
    for the worker's run_python call) and reports THAT number. Its compute output is the
    program's stdout. It must PASS -- even though the planted file also exists in the folder.

The planted file deliberately carries a DIFFERENT, attacker-chosen correlation from the one
the raw data actually supports, so 'rejected' and 'passed' cannot both be the same number by
accident: the only way the recomputed answer passes is by reporting what the raw data gives.

Stdlib + openpyxl only; no network, no DB, no pandas. The whole folder is built under tmp_path
so the test is hermetic and leaves nothing behind.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import analysis_provenance as ap  # noqa: E402

openpyxl = pytest.importorskip("openpyxl")


# The raw input: two columns with a strong but not perfect positive relationship. These exact
# numbers are fixed so the true Pearson r is deterministic.
_RAW_X = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
_RAW_Y = [2.1, 3.9, 6.2, 7.8, 10.1, 12.0, 13.9, 16.2, 17.8, 20.1]

# A correlation nobody can get from the raw data: the planted file asserts a NEGATIVE, near
# perfect correlation, the attacker's "conclusion". If the gate let a transcription through,
# this is the number that would reach the answer.
_PLANTED_R = -0.97


def _pearson(xs, ys):
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / ((sxx * syy) ** 0.5)


def _build_case_folder(root):
    """raw.xlsx (the input), old_results.xlsx (a prior run's workbook), planted_results.txt
    (the fake). Returns the true correlation the RAW data supports."""
    raw = openpyxl.Workbook()
    ws = raw.active
    ws.title = "raw"
    ws.append(["x", "y"])
    for x, y in zip(_RAW_X, _RAW_Y):
        ws.append([x, y])
    raw.save(os.path.join(root, "raw.xlsx"))

    true_r = _pearson(_RAW_X, _RAW_Y)

    # An OLD results workbook from an earlier run: it happens to carry a DIFFERENT number
    # (rounded, stale) -- a derived artifact, untrusted.
    old = openpyxl.Workbook()
    wo = old.active
    wo.append(["metric", "value"])
    wo.append(["correlation", 0.50])
    old.save(os.path.join(root, "old_results.xlsx"))

    # The PLANTED fake: a text 'conclusion report' asserting the attacker's number.
    with open(os.path.join(root, "planted_results.txt"), "w", encoding="utf-8") as f:
        f.write("CONCLUSION: the correlation between x and y is %.2f (very strong, negative).\n"
                % _PLANTED_R)
    return true_r


def test_an_answer_copied_from_the_planted_file_is_rejected(tmp_path):
    folder = str(tmp_path)
    _build_case_folder(folder)

    # The worker read the planted file and copied its number, running NO computation. An
    # attributed ledger for such a one-turn no-op shows no compute-tool output at all.
    planted_text = open(os.path.join(folder, "planted_results.txt"), encoding="utf-8").read()
    answer = "Based on the folder, the correlation is %.2f." % _PLANTED_R
    assert ("%.2f" % _PLANTED_R) in planted_text  # the number really came from the plant

    r = ap.check_answer_provenance(answer, [], is_analysis=True)
    assert not r["ok"], "a number with no computation behind it must not pass"
    assert _PLANTED_R in r["unsupported"]
    assert r["correction"]


def test_an_answer_recomputed_from_the_raw_table_passes(tmp_path):
    folder = str(tmp_path)
    true_r = _build_case_folder(folder)

    # The worker loaded the RAW workbook and computed the correlation in THIS run. We stand in
    # for that run_python call by computing it here and taking its printed output as the
    # compute output the ledger would have captured.
    wb = openpyxl.load_workbook(os.path.join(folder, "raw.xlsx"))
    ws = wb["raw"]
    xs, ys = [], []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue  # header
        xs.append(float(row[0]))
        ys.append(float(row[1]))
    recomputed_r = _pearson(xs, ys)
    compute_output = "recomputed pearson r = %.6f over %d rows" % (recomputed_r, len(xs))

    # The honest answer reports the recomputed number (rounded for the reader).
    answer = "Recomputing from raw.xlsx, the correlation is %.2f." % recomputed_r

    r = ap.check_answer_provenance(answer, [compute_output], is_analysis=True)
    assert r["ok"], r
    # And it is genuinely the raw-data answer, not the plant: the two differ in sign and size.
    assert abs(recomputed_r - _PLANTED_R) > 1.0
    assert abs(recomputed_r - true_r) < 1e-9


def test_the_planted_number_would_fail_even_while_a_real_computation_ran(tmp_path):
    """The subtle hole: a worker that DOES run a computation but still reports the plant. A
    real compute output is present, but the number in the answer is the plant's, not the
    computed one -- it must still be caught."""
    folder = str(tmp_path)
    true_r = _build_case_folder(folder)
    compute_output = "pearson r = %.6f" % true_r  # a real computation ran...
    answer = "The correlation is %.2f." % _PLANTED_R  # ...but the answer reports the plant
    r = ap.check_answer_provenance(answer, [compute_output], is_analysis=True)
    assert not r["ok"]
    assert _PLANTED_R in r["unsupported"]

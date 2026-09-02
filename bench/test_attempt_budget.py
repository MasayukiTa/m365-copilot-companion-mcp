"""An ungraded patch is not a finished instance.

THE MEASUREMENT THIS ENCODES, on the same 39 instances:

    2026-08-30    7 got 1 attempt -> 42.9% resolved
                 21 got 2         -> 81.0%              28/40 = 70.0%
    2026-09-01   37 got 1 attempt
                  3 got 2                               23/39 = 59.0%

Per-attempt quality did not fall; the second attempt stopped happening. `captured_ids()` --
"a patch file exists" -- was unioned into the skip set whether or not that patch had ever been
graded, and with the eval host down `graded_ids()` was empty, so having a file was the only
gate and every instance retired after one try.
"""
import io
import json
import os

import pytest

from bench import pro_cycle as C


@pytest.fixture(autouse=True)
def _files(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "PREDS", str(tmp_path / "preds.json"))
    monkeypatch.setattr(C, "RESULTS", str(tmp_path / "results.json"))
    monkeypatch.setattr(C, "ATTEMPTS", str(tmp_path / "attempts.json"))
    monkeypatch.setattr(C, "MAX_ATTEMPTS", 2)
    return tmp_path


def _preds(tmp_path, rows):
    io.open(str(tmp_path / "preds.json"), "w", encoding="utf-8").write(
        json.dumps(rows, ensure_ascii=False))


def _attempts(tmp_path, d):
    io.open(str(tmp_path / "attempts.json"), "w", encoding="utf-8").write(
        json.dumps(d, ensure_ascii=False))


def test_a_captured_but_ungraded_patch_is_not_finished(_files):
    # THE REGRESSION. One attempt, a patch on disk, nobody has checked whether it works.
    _preds(_files, [{"instance_id": "i1", "patch": "diff --git a/x b/x"}])
    _attempts(_files, {"i1": 1})
    assert "i1" not in C.exhausted_ids()
    assert "i1" not in C.refused_ids()


def test_it_is_finished_once_the_attempts_are_spent(_files):
    _preds(_files, [{"instance_id": "i1", "patch": "diff"}])
    _attempts(_files, {"i1": 2})
    assert "i1" in C.exhausted_ids()


def test_a_refused_diff_is_never_retried(_files):
    # Measured: 3,054,501 bytes on the first attempt and 74,850,968 on the second. Retrying an
    # over-large diff spends a batch slot to get a worse answer.
    _preds(_files, [{"instance_id": "big", "refused": True, "patch": ""}])
    _attempts(_files, {"big": 1})
    assert "big" in C.refused_ids()


def test_a_refusal_is_not_confused_with_a_capture(_files):
    _preds(_files, [{"instance_id": "big", "refused": True, "patch": ""},
                    {"instance_id": "ok", "patch": "diff"}])
    assert C.refused_ids() == {"big"}
    assert "ok" in C.captured_ids()
    assert "ok" not in C.refused_ids()


def test_the_counter_increments(_files):
    C.note_attempts(["a", "b"])
    C.note_attempts(["a"])
    counts = C.attempt_counts()
    assert counts["a"] == 2 and counts["b"] == 1


def test_the_cap_is_reached_only_by_real_attempts(_files):
    C.note_attempts(["a"])
    assert C.exhausted_ids() == set()
    C.note_attempts(["a"])
    assert C.exhausted_ids() == {"a"}


def test_a_missing_counter_file_is_not_an_error(_files):
    assert C.attempt_counts() == {}
    assert C.exhausted_ids() == set()


def test_the_counter_never_raises_on_an_unwritable_path(monkeypatch, _files):
    # Losing the counter must not stop a benchmark run. Undercounting costs an extra attempt;
    # raising costs the whole cycle.
    monkeypatch.setattr(C, "ATTEMPTS", "Z:/definitely/not/a/place/attempts.json")
    C.note_attempts(["a"])          # must not raise


def test_a_graded_instance_is_still_never_re_run(_files):
    # The anti-drift rule the module is built around: a measured instance is not measured
    # again, whatever its verdict. Widening the retry must not have widened this.
    io.open(str(_files / "results.json"), "w", encoding="utf-8").write(
        json.dumps({"instance_id": "g1", "verdict": "not"}) + "\n")
    assert "g1" in C.graded_ids()


def test_an_exhausted_instance_is_skipped_even_with_no_patch(_files):
    # An instance that burns its attempts producing nothing must also stop, or a worker that
    # reliably crashes would be retried for ever.
    _attempts(_files, {"crashy": 2})
    assert "crashy" in C.exhausted_ids()
    assert "crashy" not in C.captured_ids()


def test_a_results_file_with_one_row_is_read_as_a_row(_files):
    # ONE JSONL ROW IS ALSO VALID JSON, so it parsed as a dict and was read as an
    # {instance_id: verdict} mapping -- graded_ids() returned the FIELD NAMES
    # {"instance_id", "verdict"} and the instance that had actually been graded was missing,
    # so it got re-run. Every results file looks like this after the first grading.
    io.open(str(_files / "results.json"), "w", encoding="utf-8").write(
        json.dumps({"instance_id": "g1", "verdict": "RESOLVED"}) + "\n")
    got = C.graded_ids()
    assert got == {"g1"}, got


def test_several_rows_still_work(_files):
    io.open(str(_files / "results.json"), "w", encoding="utf-8").write(
        json.dumps({"instance_id": "g1", "verdict": "RESOLVED"}) + "\n"
        + json.dumps({"instance_id": "g2", "verdict": "not"}) + "\n")
    assert C.graded_ids() == {"g1", "g2"}


def test_the_mapping_shape_still_works(_files):
    # The grader writes {instance_id: bool}; both shapes have to keep working, which is the
    # whole reason the reader accepts more than one.
    io.open(str(_files / "results.json"), "w", encoding="utf-8").write(
        json.dumps({"a1": True, "a2": False}))
    assert C.graded_ids() == {"a1", "a2"}

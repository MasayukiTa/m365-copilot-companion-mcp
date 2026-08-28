"""What the recorder refuses to archive.

Every safeguard in this program used to be a printed line, and a printed line is not a
safeguard: the archive is read later by a dashboard and by an evolution loop, neither of which
saw the console. These tests pin the cases where the row must not be written at all.
"""
import json
import os

import pytest

from bench.pro_record_result import _load_preds, _slice_problems, main


def _write(d, name, obj):
    p = os.path.join(str(d), name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return p


def _run(tmp_path, preds, slice_ids, resolved, extra=None):
    d = tmp_path
    args = ["--grade", _write(d, "g.json", {"resolved": resolved}),
            "--preds", _write(d, "preds.json", preds),
            "--slice-file", _write(d, "slice.json", [{"instance_id": i} for i in slice_ids]),
            "--wtmap", os.path.join(str(d), "none.json"),
            "--history", os.path.join(str(d), "none.json"),
            "--run-config", os.path.join(str(d), "none.json")]
    # DRY-RUN ON PURPOSE. The refusal happens BEFORE the commit gate, so these tests reach the
    # behaviour they are about without pointing a write at the real archive and burned
    # registry -- which is what a --commit here would do, since those paths are defaults.
    return main(args + (extra or []))


def test_duplicates_are_deduped_and_reported(tmp_path):
    p = _write(tmp_path, "p.json", [{"instance_id": "a", "patch": "x"},
                                    {"instance_id": "a", "patch": "x"},
                                    {"instance_id": "b", "patch": "x"}])
    ids, _, problems = _load_preds(p)
    assert ids == ["a", "b"]
    assert any("duplicate" in x for x in problems)


def test_a_row_without_an_instance_id_is_reported(tmp_path):
    p = _write(tmp_path, "p.json", [{"patch": "x"}, {"instance_id": "a", "patch": "x"}])
    ids, _, problems = _load_preds(p)
    assert ids == ["a"] and any("no instance_id" in x for x in problems)


def test_a_short_slice_is_named_as_a_problem(tmp_path):
    """A five-instance file with four solves is "pass@1 = 0.80" and reads like a triumph."""
    sl = _write(tmp_path, "s.json", [{"instance_id": "a"}, {"instance_id": "b"},
                                     {"instance_id": "c"}])
    problems, size = _slice_problems(["a"], sl)
    assert size == 3 and any("absent" in x for x in problems)


def test_a_foreign_instance_is_named_as_a_problem(tmp_path):
    sl = _write(tmp_path, "s.json", [{"instance_id": "a"}])
    problems, _ = _slice_problems(["a", "zzz"], sl)
    assert any("not part of this slice" in x for x in problems)


def test_a_missing_canonical_slice_does_not_block(tmp_path):
    """The canonical file is not always beside the recorder, and refusing to record because it
    is absent would be a new way to lose a real result."""
    problems, size = _slice_problems(["a"], os.path.join(str(tmp_path), "nope.json"))
    assert problems == [] and size is None


def test_a_run_that_does_not_check_out_is_refused(tmp_path):
    rc = _run(tmp_path,
              preds=[{"instance_id": "a", "patch": "x"}],
              slice_ids=["a", "b", "c"], resolved=["a"])
    assert rc == 2


def test_force_without_a_reason_is_still_refused(tmp_path):
    """A row recorded over a failed check with no reason is indistinguishable from one that
    passed."""
    rc = _run(tmp_path,
              preds=[{"instance_id": "a", "patch": "x"}],
              slice_ids=["a", "b"], resolved=["a"], extra=["--force"])
    assert rc == 2


def test_a_clean_run_is_not_refused(tmp_path):
    """The checks must not close the door on the case they exist to protect."""
    rc = _run(tmp_path,
              preds=[{"instance_id": "a", "patch": "x"}, {"instance_id": "b", "patch": "x"}],
              slice_ids=["a", "b"], resolved=["a"])
    assert rc == 0

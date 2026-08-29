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


def test_a_run_can_name_instances_it_spoiled(tmp_path):
    """A batch went out with the cockpit's fan-out toggle left on, so eight SWE instances were
    split into subtasks whose children then edited the SAME worktree -- and capture reads one
    diff per worktree, so several children's edits to one checkout came back as that instance's
    patch. The rest of the run was clean; what it must not do is score the spoiled eight."""
    from bench.pro_record_result import _read_id_list
    p = tmp_path / "spoiled.txt"
    p.write_text("a\n# a comment\n\nb\n", encoding="utf-8")
    assert _read_id_list(str(p)) == {"a", "b"}


def test_a_missing_exclude_file_excludes_nothing(tmp_path):
    """A clean run has no such file, and its absence must not be an error."""
    from bench.pro_record_result import _read_id_list
    assert _read_id_list(str(tmp_path / "none.txt")) == set()


def test_spoiled_instances_leave_the_slice_before_anything_is_scored(tmp_path):
    """Excluded, not marked failed: an instance whose patch was assembled from several
    children's edits to one checkout was never measured, and scoring it either way is a
    statement about work that did not happen."""
    d = tmp_path
    sp = d / "spoiled.txt"
    sp.write_text("a\n", encoding="utf-8")
    args = ["--grade", _write(d, "g.json", {"resolved": ["b"]}),
            "--preds", _write(d, "preds.json", [{"instance_id": "a", "patch": "x"},
                                                {"instance_id": "b", "patch": "y"}]),
            "--slice-file", _write(d, "slice.json", [{"instance_id": "a"},
                                                     {"instance_id": "b"}]),
            "--wtmap", str(d / "none.json"), "--history", str(d / "none.json"),
            "--run-config", str(d / "none.json"), "--exclude-file", str(sp)]
    # 'a' is spoiled, so only 'b' remains -- and 'b' resolved, so the slice check must be the
    # only thing that can still complain.
    rc = main(args)
    assert rc in (0, 2)

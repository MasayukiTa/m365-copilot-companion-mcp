"""load_preds() must go through relay.bestofn_run.load_candidate_dir, not a hand-rolled copy
of the same directory-loading loop.

swe_solve_decoupled.py and swe_check.py both write preds_solve/<inst>.json as a one-element
list `[{"instance_id": inst, "model_patch": diff, "model_name_or_path": "companion"}]` --
exactly the shape load_candidate_dir already parses (sorted listing, .json filter, tolerant of
unreadable/malformed files, one-element-list-of-dict capture). This test executes the real
production caller (bench.swe_grade_batch.load_preds, called from main()) against a tempdir of
capture files and would fail if that call were replaced by something that does not go through
load_candidate_dir -- see the mutation note below.
"""
import json
import os
import tempfile

from bench import swe_grade_batch as G


def _write(dpath, inst, patch):
    with open(os.path.join(dpath, inst + ".json"), "w", encoding="utf-8") as f:
        json.dump([{"instance_id": inst, "model_patch": patch,
                   "model_name_or_path": "companion"}], f)


def test_load_preds_reads_every_capture_in_the_directory():
    with tempfile.TemporaryDirectory() as dpath:
        _write(dpath, "repo__a-1", "diff-a")
        _write(dpath, "repo__b-2", "diff-b")
        preds = G.load_preds(dpath)
        assert preds == {"repo__a-1": "diff-a", "repo__b-2": "diff-b"}


def test_load_preds_is_filtered_by_want():
    with tempfile.TemporaryDirectory() as dpath:
        _write(dpath, "repo__a-1", "diff-a")
        _write(dpath, "repo__b-2", "diff-b")
        preds = G.load_preds(dpath, want=["repo__b-2"])
        assert preds == {"repo__b-2": "diff-b"}


def test_load_preds_skips_unreadable_and_malformed_files_without_raising():
    """The old hand-rolled loop caught any exception from a bad file and moved on; this must
    keep that tolerance even though it now goes through load_candidate_dir."""
    with tempfile.TemporaryDirectory() as dpath:
        _write(dpath, "repo__good-1", "diff-good")
        with open(os.path.join(dpath, "broken.json"), "w", encoding="utf-8") as f:
            f.write("{not valid json")
        with open(os.path.join(dpath, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("ignore me")
        # a well-formed JSON record with no "model_patch" key -- the shape load_candidate_dir
        # itself would still capture verbatim, so the KeyError guard in load_preds is exercised.
        with open(os.path.join(dpath, "repo__nopatch-1.json"), "w", encoding="utf-8") as f:
            json.dump([{"instance_id": "repo__nopatch-1"}], f)
        preds = G.load_preds(dpath)
        assert preds == {"repo__good-1": "diff-good"}


def test_load_preds_goes_through_load_candidate_dir_not_a_hand_rolled_copy(monkeypatch):
    """MUTATION CHECK, inline: if load_preds stopped calling load_candidate_dir, this fails."""
    calls = []
    real = G.load_candidate_dir

    def _spy(dir_path):
        calls.append(dir_path)
        return real(dir_path)

    monkeypatch.setattr(G, "load_candidate_dir", _spy)
    with tempfile.TemporaryDirectory() as dpath:
        _write(dpath, "repo__a-1", "diff-a")
        preds = G.load_preds(dpath)
        assert preds == {"repo__a-1": "diff-a"}
    assert calls == [dpath], "load_preds must call load_candidate_dir exactly once, on preds_dir"


def test_missing_preds_dir_returns_empty_not_a_crash():
    with tempfile.TemporaryDirectory() as dpath:
        assert G.load_preds(os.path.join(dpath, "does_not_exist")) == {}

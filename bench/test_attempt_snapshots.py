"""Rescue and regression, and why regression is the half nobody looks for."""
import io
import json
import os

from bench.attempt_snapshots import distinct_attempts, load, summary, transitions


def _snap(tmp, inst, patch_hash, at, patch="x"):
    p = os.path.join(str(tmp), "%s__%s.json" % (inst, at))
    io.open(p, "w", encoding="utf-8").write(json.dumps(
        {"instance_id": inst, "patch": patch, "captured_at": at,
         "patch_sha256_16": patch_hash}))


def test_identical_attempts_do_not_count_as_two(tmp_path):
    """Grading the same patch twice answers nothing, and counting it as two attempts would
    inflate the denominator of every rate computed from these."""
    _snap(tmp_path, "i1", "aaaa", 1.0)
    _snap(tmp_path, "i1", "aaaa", 2.0)
    assert distinct_attempts(load(str(tmp_path))) == 0


def test_rescue_and_regression_are_counted_separately(tmp_path):
    """A policy that rescues some and breaks others is not the net of the two reported as a
    gain. The completion floor cannot tell them apart at all: both attempts say DONE."""
    _snap(tmp_path, "rescued", "h1", 1.0)
    _snap(tmp_path, "rescued", "h2", 2.0)
    _snap(tmp_path, "broke", "h3", 1.0)
    _snap(tmp_path, "broke", "h4", 2.0)
    verdicts = {"h1": False, "h2": True, "h3": True, "h4": False}
    t = transitions(load(str(tmp_path)), verdicts)
    assert t["rescued"] == 1 and t["regressed"] == 1
    assert t["instances_considered"] == 2


def test_an_ungraded_attempt_is_not_a_failure(tmp_path):
    """Absence of a verdict must not become a wrong answer; that would let the size of the
    grading effort move the rate."""
    _snap(tmp_path, "i1", "h1", 1.0)
    _snap(tmp_path, "i1", "h2", 2.0)
    t = transitions(load(str(tmp_path)), {"h1": True})   # h2 never graded
    assert t["ungradable"] == 1
    assert t["rescued"] == 0 and t["regressed"] == 0


def test_the_reading_travels_with_the_numbers(tmp_path):
    t = transitions({}, {})
    assert "not a 30% improvement" in t["reading"]


def test_summary_says_how_many_could_contribute(tmp_path):
    _snap(tmp_path, "i1", "h1", 1.0)
    _snap(tmp_path, "i1", "h1", 2.0)
    _snap(tmp_path, "i2", "h2", 1.0)
    _snap(tmp_path, "i2", "h3", 2.0)
    s = summary(str(tmp_path))
    assert s["instances_with_snapshots"] == 2 and s["snapshots"] == 4
    assert s["instances_with_differing_attempts"] == 1

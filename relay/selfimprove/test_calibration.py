"""Unit tests for the calibrated competence model. Run: python -m relay.selfimprove.test_calibration"""
import json
import os
import tempfile

from relay.selfimprove import calibration as C


def _write_ledger(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_classify_instance():
    assert C.classify_instance("django__django-12345") == "django"
    assert C.classify_instance("psf__requests-1") == "psf"
    assert C.classify_instance("scikit-learn__scikit-learn-2") == "scikit-learn"
    assert C.classify_instance("noseparator") == "noseparator"
    assert C.classify_instance("") == "unknown"
    assert C.classify_instance(None) == "unknown"
    print("ok test_classify_instance")


def test_wilson_bounds():
    # n == 0 -> (0,0)
    assert C._wilson(0, 0) == (0.0, 0.0)
    for (k, n) in [(0, 10), (5, 10), (10, 10), (3, 7), (1, 100), (99, 100)]:
        low, high = C._wilson(k, n)
        point = 100.0 * k / n
        assert 0.0 <= low <= point <= high <= 100.0, (k, n, low, point, high)
    print("ok test_wilson_bounds")


def test_calibration_report_groups_and_evalerr():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "grade_results.jsonl")
        recs = [
            # django: 6 records -> 5 strong (one is EVALERR, excluded) so n=5, resolved=5 -> 100%
            {"instance_id": "django__django-1", "verdict": "RESOLVED", "ts": 100},
            {"instance_id": "django__django-2", "verdict": "RESOLVED", "ts": 100},
            {"instance_id": "django__django-3", "verdict": "RESOLVED", "ts": 100},
            {"instance_id": "django__django-4", "verdict": "RESOLVED", "ts": 100},
            {"instance_id": "django__django-5", "verdict": "RESOLVED", "ts": 100},
            {"instance_id": "django__django-6", "verdict": "EVALERR", "ts": 100},  # excluded
            # sympy: weak well-sampled class -> 1/6 resolved (the dup below counts once)
            {"instance_id": "sympy__sympy-1", "verdict": "not", "ts": 100},
            {"instance_id": "sympy__sympy-2", "verdict": "not", "ts": 100},
            {"instance_id": "sympy__sympy-3", "verdict": "not", "ts": 100},
            {"instance_id": "sympy__sympy-4", "verdict": "not", "ts": 100},
            {"instance_id": "sympy__sympy-5", "verdict": "RESOLVED", "ts": 100},
            # duplicate instance graded twice: latest (ts=300) is "not", earlier (ts=200) "RESOLVED"
            {"instance_id": "sympy__sympy-6", "verdict": "RESOLVED", "ts": 200},
            {"instance_id": "sympy__sympy-6", "verdict": "not", "ts": 300},
        ]
        _write_ledger(path, recs)

        rep = C.calibration_report(path)  # default dedupe="latest"
        assert rep["n_records_read"] == 13
        assert rep["n_evalerr_excluded"] == 1

        dj = rep["by_class"]["django"]
        assert dj["n"] == 5 and dj["resolved"] == 5
        assert abs(dj["pass_at_1"] - 1.0) < 1e-9

        # sympy: 6 unique instances (dup counted once), resolved = sympy-5 only -> 1/6
        sy = rep["by_class"]["sympy"]
        assert sy["n"] == 6, sy
        assert sy["resolved"] == 1, sy            # dup's latest verdict "not" -> not resolved
        assert abs(sy["pass_at_1"] - (1 / 6)) < 1e-9

        # overall: django 5 + sympy 6 = 11, resolved 5 + 1 = 6 (evalerr excluded throughout)
        ov = rep["overall"]
        assert ov["n"] == 11 and ov["resolved"] == 6
        assert 0.0 <= ov["ci_low"] <= ov["pass_at_1"] * 100.0 <= ov["ci_high"] <= 100.0

        # dedupe="none": dup counts as 2 records -> sympy n=7
        rep_none = C.calibration_report(path, dedupe="none")
        assert rep_none["by_class"]["sympy"]["n"] == 7
        assert rep_none["by_class"]["django"]["n"] == 5  # evalerr still excluded
    print("ok test_calibration_report_groups_and_evalerr")


def test_competence_and_recommend_effort():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "grade_results.jsonl")
        recs = []
        # strong well-sampled class: 9/10 resolved
        for i in range(9):
            recs.append({"instance_id": "django__django-%d" % i, "verdict": "RESOLVED", "ts": 100})
        recs.append({"instance_id": "django__django-9", "verdict": "not", "ts": 100})
        # weak well-sampled class: 1/8 resolved
        recs.append({"instance_id": "sphinx__sphinx-0", "verdict": "RESOLVED", "ts": 100})
        for i in range(1, 8):
            recs.append({"instance_id": "sphinx__sphinx-%d" % i, "verdict": "not", "ts": 100})
        # data-poor class: only 2 records
        recs.append({"instance_id": "flask__flask-0", "verdict": "RESOLVED", "ts": 100})
        recs.append({"instance_id": "flask__flask-1", "verdict": "RESOLVED", "ts": 100})
        _write_ledger(path, recs)
        rep = C.calibration_report(path)

        assert C.competence(rep, "django") is not None
        assert C.competence(rep, "does-not-exist") is None

        # strong, well-sampled -> single-shot
        strong = C.recommend_effort(rep, "django")
        assert strong["mode"] == "single-shot", strong
        assert "strong" in strong["reason"]

        # weak, well-sampled -> best-of-N
        weak = C.recommend_effort(rep, "sphinx")
        assert weak["mode"] == "best-of-N", weak
        assert "weak" in weak["reason"]

        # data-poor (n < min_n=5) -> best-of-N, caution default (even though 100% so far)
        poor = C.recommend_effort(rep, "flask")
        assert poor["mode"] == "best-of-N", poor
        assert "insufficient data" in poor["reason"]

        # unknown class -> best-of-N, caution default
        unk = C.recommend_effort(rep, "never-seen")
        assert unk["mode"] == "best-of-N" and "insufficient data" in unk["reason"]
    print("ok test_competence_and_recommend_effort")


def test_render_text_never_raises():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "grade_results.jsonl")
        _write_ledger(path, [
            {"instance_id": "django__django-1", "verdict": "RESOLVED", "ts": 1},
            {"instance_id": "sympy__sympy-1", "verdict": "not", "ts": 1},
        ])
        rep = C.calibration_report(path)
        txt = C.render_text(rep)
        assert "class" in txt and "OVERALL" in txt
    # empty report renders cleanly
    assert C.render_text({"by_class": {}, "overall": C._empty_overall(),
                          "n_records_read": 0, "n_evalerr_excluded": 0}) == "no grade history yet"
    assert C.render_text({}) == "no grade history yet"
    print("ok test_render_text_never_raises")


def test_missing_and_empty_file():
    # missing file -> empty report, no raise
    rep = C.calibration_report(os.path.join(tempfile.gettempdir(), "definitely_missing_xyz.jsonl"))
    assert rep["by_class"] == {}
    assert rep["overall"]["n"] == 0 and rep["overall"]["pass_at_1"] is None
    assert rep["n_records_read"] == 0 and rep["n_evalerr_excluded"] == 0

    with tempfile.TemporaryDirectory() as d:
        # empty file
        empty = os.path.join(d, "empty.jsonl")
        open(empty, "w", encoding="utf-8").close()
        rep_e = C.calibration_report(empty)
        assert rep_e["by_class"] == {} and rep_e["overall"]["n"] == 0

        # garbage / malformed lines -> skipped, no raise
        garbage = os.path.join(d, "garbage.jsonl")
        with open(garbage, "w", encoding="utf-8", newline="\n") as f:
            f.write("not json at all\n")
            f.write("{ broken json\n")
            f.write("\n")
            f.write(json.dumps({"no_instance": "x", "verdict": "RESOLVED"}) + "\n")  # no id -> skip
            f.write(json.dumps({"instance_id": "django__django-1", "verdict": "RESOLVED", "ts": 1}) + "\n")
        rep_g = C.calibration_report(garbage)
        assert rep_g["n_records_read"] == 1  # only the one valid record with an id
        assert rep_g["by_class"]["django"]["n"] == 1
    print("ok test_missing_and_empty_file")


if __name__ == "__main__":
    test_classify_instance()
    test_wilson_bounds()
    test_calibration_report_groups_and_evalerr()
    test_competence_and_recommend_effort()
    test_render_text_never_raises()
    test_missing_and_empty_file()
    print("ALL CALIBRATION TESTS PASSED")

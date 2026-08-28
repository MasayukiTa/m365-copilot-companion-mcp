"""The retry floor, and the two ways this calculation lies if nobody watches it.

Both have already happened here. The first version reported rates above 1 -- "k=3: 1.010
(98/97)" -- because the numerator counted every goal and the denominator only goals with
enough attempts. And the population is goals SOMEBODY RETRIED, which selects for early
failure, so the k=1 point is not what it looks like.
"""
from bench.retry_floor import floor_curve, group_attempts, per_attempt_rate, report


def _g(name, outcomes):
    return [{"goal": name, "outcome": o, "ts": i} for i, o in enumerate(outcomes)]


def test_every_rate_is_a_rate():
    """THE MEASURED DEFECT. Two populations under one fraction produced 98/97 and 100/24."""
    rows = _g("a", ["STUCK", "DONE"]) + _g("b", ["STUCK", "STUCK", "DONE"]) + _g("c", ["DONE"])
    for c in floor_curve(group_attempts(rows), max_k=5):
        assert 0.0 <= c["rate"] <= 1.0, c
        assert c["solved"] <= c["eligible"], c


def test_a_goal_only_counts_toward_k_if_it_had_k_attempts():
    """What makes the curve a measurement rather than an artefact of which goals happened to
    be retried often."""
    rows = _g("a", ["STUCK", "DONE"]) + _g("b", ["STUCK", "STUCK", "STUCK", "DONE"])
    curve = {c["k"]: c for c in floor_curve(group_attempts(rows), max_k=4)}
    assert curve[2]["eligible"] == 2
    assert curve[4]["eligible"] == 1


def test_the_marginal_gain_holds_the_goals_fixed():
    """Subtracting across differing denominators is the same error in another shape."""
    rows = _g("a", ["STUCK", "DONE"]) + _g("b", ["STUCK", "STUCK", "DONE"])
    curve = {c["k"]: c for c in floor_curve(group_attempts(rows), max_k=3)}
    # At k=3 only goal b is eligible; it was unsolved at k=2 and solved at k=3.
    assert curve[3]["eligible"] == 1
    assert curve[3]["marginal"] == 1.0


def test_the_per_attempt_rate_is_not_the_k1_point():
    """One averages over attempts, the other over goals counting first attempts only.
    Reporting either as the other is how a retry-heavy population inflates a single-shot
    number."""
    rows = _g("a", ["STUCK", "DONE", "DONE"])
    per = per_attempt_rate(group_attempts(rows))
    curve = {c["k"]: c for c in floor_curve(group_attempts(rows), max_k=1)}
    assert per["rate"] == 2 / 3
    assert curve[1]["rate"] == 0.0


def test_the_selection_bias_travels_with_the_numbers(tmp_path):
    """The population is goals somebody retried, and a goal is retried because its first
    attempt failed. A caller must not be able to receive the k=1 figure without the warning
    that explains it."""
    import json
    p = tmp_path / "h.json"
    p.write_text(json.dumps(_g("a", ["STUCK", "DONE"])), encoding="utf-8")
    r = report(str(p))
    assert r["k1_is_selection_biased"] is True
    assert "not the general single-attempt rate" in r["read_this_first"]


def test_a_missing_ledger_reports_nothing_rather_than_raising(tmp_path):
    r = report(str(tmp_path / "nope.json"))
    assert r["ledger_rows"] == 0 and r["curve"] == []


def test_goals_attempted_once_are_excluded_from_the_retry_population():
    rows = _g("solo", ["DONE"]) + _g("retried", ["STUCK", "DONE"])
    assert set(group_attempts(rows)) == {"retried"}


def test_the_curve_says_it_counts_completion_not_correctness(tmp_path):
    """REPORTED AS AN ACCURACY FLOOR AND IT IS NOT ONE. DONE is the worker saying it finished;
    nothing external checked the answer, so a mechanism asked to beat 0.931 is being asked to
    beat a claim rather than a result. This project already holds that a self-report nobody
    verifies is worth less than no field at all -- the rule had simply never been applied to
    the loop's own number."""
    import json
    p = tmp_path / "h.json"
    p.write_text(json.dumps(_g("a", ["STUCK", "DONE"])), encoding="utf-8")
    r = report(str(p))
    assert "NOT external correctness" in r["measures"]
    assert "not an accuracy floor" in r["not_an_accuracy_floor"].lower() or \
           "An oracle is required" in r["not_an_accuracy_floor"]

"""pass^k, and the two ways a second headline metric can lie.

The failures these hold shut, both of which have already happened once in this repository:

  * A CORRECTION read as a REPEAT. A corrupted grade of 0.34 was re-run at 0.50, and the trend
    drew a measurement error as a 16-point improvement. Fed to a reliability figure, the same
    pair would read as a flaky scaffold.
  * A number invented from a rate. pass^k derived from pass@1 requires assuming instances are
    independent and equally hard, and the falseness of that assumption is the whole reason to
    want pass^k.
"""
from relay.selfimprove.reliability import live, pass_hat_k, spread, summary, wilson


def _row(eid, ids, resolved, rate=None, run_id=None):
    return {"id": eid, "slice_ids": list(ids), "resolved_ids": list(resolved),
            "pass_at_1": rate if rate is not None else len(resolved) / len(ids),
            "run_id": run_id}


IDS = ["a", "b", "c", "d"]


# ---- correction vs repeat ----------------------------------------------------------------

def test_a_correction_is_not_a_second_measurement():
    """Two rows, same id, both unmarked: the later corrects the earlier. Counting them as two
    runs would report a grading-host artifact as instability."""
    rows = [_row("g1", IDS, ["a"]), _row("g1", IDS, ["a", "b", "c"])]
    assert len(live(rows)) == 1
    assert pass_hat_k(rows)[0]["k"] == 1


def test_a_row_from_another_run_is_a_second_measurement():
    """The distinction the id could not carry. Without it k can never reach 2 and no
    reliability figure is computable from any number of runs."""
    rows = [_row("g1", IDS, ["a", "b"]), _row("g1", IDS, ["a", "c"], run_id="r2")]
    assert len(live(rows)) == 2
    assert pass_hat_k(rows)[0]["k"] == 2


def test_a_repeat_can_itself_be_regraded():
    """Replicate 2 measured twice: the later corrects it, and k stays 2 rather than becoming 3."""
    rows = [_row("g1", IDS, ["a"]),
            _row("g1", IDS, ["b"], run_id="r2"),
            _row("g1", IDS, ["a", "b"], run_id="r2")]
    assert pass_hat_k(rows)[0]["k"] == 2


# ---- what pass^k actually measures --------------------------------------------------------

def test_the_two_scaffolds_pass_at_1_cannot_tell_apart():
    """THE REASON THIS FILE EXISTS. Both score 0.50 per attempt. One solves the same half
    every time; the other solves a different half each run. Opposite findings, one number."""
    steady = [_row("s", IDS, ["a", "b"]), _row("s", IDS, ["a", "b"], run_id="r2")]
    random_ = [_row("r", IDS, ["a", "b"]), _row("r", IDS, ["c", "d"], run_id="r2")]
    assert pass_hat_k(steady)[0]["per_run_pass_at_1"] == pass_hat_k(random_)[0]["per_run_pass_at_1"]
    assert pass_hat_k(steady)[0]["pass_hat_k"] == 0.5
    assert pass_hat_k(random_)[0]["pass_hat_k"] == 0.0


def test_flaky_is_the_gap_between_solved_once_and_solved_always():
    rows = [_row("g", IDS, ["a", "b"]), _row("g", IDS, ["a", "c"], run_id="r2")]
    r = pass_hat_k(rows)[0]
    assert r["pass_hat_k"] == 0.25          # only "a" survived both
    assert r["pass_any"] == 0.75            # a, b, c were each solved once
    assert r["flaky"] == 0.5


def test_a_perfectly_steady_scaffold_has_no_flakiness():
    rows = [_row("g", IDS, ["a", "b"]), _row("g", IDS, ["a", "b"], run_id="r2")]
    assert pass_hat_k(rows)[0]["flaky"] == 0.0


# ---- refusing to invent ---------------------------------------------------------------

def test_a_single_run_is_reported_as_not_enough():
    """pass^1 IS pass@1. Printing it as a reliability number invites the reading it cannot
    support."""
    rows = [_row("g", IDS, ["a", "b"])]
    assert pass_hat_k(rows)[0]["enough"] is False
    assert summary(rows)["measured"] is False


def test_an_empty_archive_says_why_rather_than_showing_a_number():
    s = summary([])
    assert s["measured"] is False and s["pass_hat_k" if False else "why_not"]


def test_rows_without_per_instance_results_are_dropped_not_read_as_zero():
    """A row predating resolved_ids carries None. Read as an empty set it would contribute a
    run in which nothing was solved, dragging pass^k down for every slice with history."""
    old = {"id": "g", "slice_ids": IDS, "resolved_ids": None, "pass_at_1": 0.5,
           "run_id": None}
    new = _row("g", IDS, ["a", "b"], run_id="r2")
    rows = pass_hat_k([old, new])
    assert len(rows) == 1 and rows[0]["k"] == 1


def test_resolved_ids_outside_the_slice_do_not_inflate_the_rate():
    rows = [_row("g", IDS, ["a", "b", "zzz"]), _row("g", IDS, ["a", "b"], run_id="r2")]
    assert pass_hat_k(rows)[0]["pass_hat_k"] == 0.5


# ---- spread ------------------------------------------------------------------------------

def test_spread_needs_two_live_runs():
    """A single run has no spread. None, not 0.0 -- zero spread states a stability never
    observed."""
    assert spread([_row("g", IDS, ["a"])])[0]["spread"] is None


def test_a_corrected_grade_produces_no_spread():
    """The exact pair that once read as a 16-point improvement. It is one measurement."""
    rows = [_row("g", IDS, [], rate=0.34), _row("g", IDS, [], rate=0.50)]
    assert summary(rows)["spread"] == []


def test_two_real_repeats_do_produce_a_spread():
    rows = [_row("g", IDS, [], rate=0.34), _row("g", IDS, [], rate=0.50, run_id="r2")]
    s = summary(rows)["spread"][0]
    assert abs(s["spread"] - 0.16) < 1e-9 and s["k"] == 2


# ---- the interval ------------------------------------------------------------------------

def test_the_interval_is_wide_for_the_sample_sizes_this_will_actually_see():
    """A pass^k over two runs of fifty is a very wide number, and printing it without an
    interval invites the peak-to-peak comparison that a rate already suffered once."""
    lo, hi = wilson(20, 50)
    assert hi - lo > 0.2


def test_the_interval_is_undefined_for_an_empty_slice():
    assert wilson(0, 0) is None

# -*- coding: utf-8 -*-
"""The resolve rate is the denominator decision, so the denominator gets tests.

Nothing read this ledger for reporting, so every number ever quoted from it was computed ad hoc
at a prompt, and each computation re-decided in silence what counted as scored. The two ways
that went wrong are both pinned here: an infrastructure failure arriving as a wrong answer, and
an instance that produced no patch being counted as one that produced a bad one.
"""
import json

from bench import pro_ledger_report as R


def _write(tmp_path, rows):
    p = tmp_path / "ledger.json"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(p)


# -- what belongs in the rate ----------------------------------------------------------------

def test_only_evaluated_instances_are_in_the_denominator(tmp_path):
    """The defect that made every number of the last days unreadable.

    EVALERR is an eval host that could not run anything; NOPATCH is a worker that produced
    nothing. Counting either as a failed patch reports infrastructure as incompetence -- and it
    happened at scale: fourteen EVALERRs written in 87 seconds and read as 0.0%, and nineteen
    NOPATCHes from a run where the fleet gate refused and no worker ever started.
    """
    led = _write(tmp_path, [
        {"instance_id": "a", "verdict": "RESOLVED"},
        {"instance_id": "b", "verdict": "not"},
        {"instance_id": "c", "verdict": "EVALERR"},
        {"instance_id": "d", "verdict": "NOPATCH"},
    ])
    t = R.tally(R.latest_rows(led))
    assert t["evaluated"] == 2
    assert t["rate"] == 0.5
    assert t["unevaluated"] == ["c"] and t["nopatch"] == ["d"]


def test_what_is_excluded_is_printed_rather_than_quietly_dropped(tmp_path):
    """An instance that silently leaves the denominator is the OTHER way to get an unreadable
    number. Shrinking it without saying so is how a rate drifts upward on its own."""
    led = _write(tmp_path, [
        {"instance_id": "a", "verdict": "RESOLVED"},
        {"instance_id": "c", "verdict": "EVALERR"},
        {"instance_id": "d", "verdict": "NOPATCH"},
    ])
    text = R.format_report(R.tally(R.latest_rows(led)))
    assert "never evaluated" in text and "produced no patch" in text
    assert "not in the rate" in text


def test_nothing_evaluated_is_not_a_score_of_zero(tmp_path):
    """0.0% and "nothing was measured" are different statements, and this pipeline has already
    published the first when it meant the second."""
    led = _write(tmp_path, [{"instance_id": "c", "verdict": "EVALERR"}])
    t = R.tally(R.latest_rows(led))
    assert t["rate"] is None
    assert "n/a" in R.format_report(t)


# -- reading the ledger ----------------------------------------------------------------------

def test_a_later_row_corrects_an_earlier_one(tmp_path):
    """Append-only means the last row about an instance is what the ledger says. Reading it any
    other way made sixteen false zeros permanent."""
    led = _write(tmp_path, [
        {"instance_id": "a", "verdict": "not"},
        {"instance_id": "a", "verdict": "RESOLVED"},
    ])
    t = R.tally(R.latest_rows(led))
    assert t["resolved"] == ["a"] and t["evaluated"] == 1


def test_an_unrecognised_verdict_is_reported_not_counted(tmp_path):
    """Fail towards saying so. A verdict this reader does not know must not be quietly folded
    into either side of a rate."""
    led = _write(tmp_path, [{"instance_id": "a", "verdict": "SOMETHING_NEW"}])
    t = R.tally(R.latest_rows(led))
    assert t["evaluated"] == 0 and t["other"] == ["a"]


# -- the rule lives in two files, so it gets a drift guard ------------------------------------

def test_this_reader_agrees_with_the_cycle_about_what_is_not_a_grade():
    """pro_cycle decides what to RE-RUN and this decides what to COUNT, from the same verdicts.
    If they disagree, an instance is either counted without being measured or measured for ever
    without being counted -- and both failures are silent."""
    from bench import pro_cycle
    assert R.NOT_A_MEASUREMENT == pro_cycle._NOT_A_GRADE


# -- instance ids ----------------------------------------------------------------------------

def test_a_repository_is_read_past_hyphens_in_the_owner(tmp_path):
    """Owner and repo both contain hyphens, so only the 40-hex commit reliably ends the name.
    Splitting on "-" put future-architect's instances under "future"."""
    assert R.repo_of("instance_future-architect__vuls-"
                     "50580f6e98eeb36f53f27222f7f4fdfea0b21e8d") == "vuls"
    assert R.repo_of("instance_tutao__tutanota-"
                     "f373ac3808deefce8183dad8d16729839cc330c1-v2939aa9f") == "tutanota"


def test_an_id_that_carries_no_repository_says_so():
    assert R.repo_of("nonsense") == "?"


# -- reporting over ONE population ------------------------------------------------------------

def test_a_slice_reports_only_its_own_population(tmp_path, capsys):
    """A benchmark number is about a stated population.

    This ledger holds every instance ever graded, under every scaffold configuration and several
    overlapping slices. Quoting one run's result from the whole file mixes it with runs that used
    different code -- which is exactly how 70.0% and 59.0% came to be compared as though they had
    measured the same thing.
    """
    led = tmp_path / "led.jsonl"
    led.write_text("\n".join(json.dumps(r) for r in [
        {"instance_id": "mine_a", "verdict": "RESOLVED"},
        {"instance_id": "mine_b", "verdict": "not"},
        {"instance_id": "old_c", "verdict": "not"},
        {"instance_id": "old_d", "verdict": "not"},
    ]) + "\n", encoding="utf-8")
    sl = tmp_path / "slice.json"
    sl.write_text(json.dumps([{"instance_id": "mine_a"}, {"instance_id": "mine_b"}]),
                  encoding="utf-8")

    assert R.main(["--ledger", str(led), "--slice", str(sl)]) == 0
    out = capsys.readouterr().out
    assert "RESOLVED 1 / 2 evaluated = 50.0%" in out, out
    assert "2 of 2 instance(s) have a row" in out


def test_an_ungraded_slice_member_is_named_not_silently_dropped(tmp_path, capsys):
    """Work that has not happened is neither a pass nor a failure, and dropping it from both the
    numerator and the denominator without saying so is how a partial run is quoted as a finished
    one."""
    led = tmp_path / "led.jsonl"
    led.write_text(json.dumps({"instance_id": "a", "verdict": "RESOLVED"}) + "\n",
                   encoding="utf-8")
    sl = tmp_path / "slice.json"
    sl.write_text(json.dumps([{"instance_id": "a"}, {"instance_id": "b"}, {"instance_id": "c"}]),
                  encoding="utf-8")

    assert R.main(["--ledger", str(led), "--slice", str(sl)]) == 0
    out = capsys.readouterr().out
    assert "1 of 3 instance(s) have a row" in out
    assert "2 not yet graded" in out


def test_a_slice_of_bare_ids_is_understood(tmp_path):
    sl = tmp_path / "slice.json"
    sl.write_text(json.dumps(["x", "y"]), encoding="utf-8")
    assert R._slice_ids(str(sl)) == {"x", "y"}


def test_an_unreadable_slice_is_refused_rather_than_treated_as_empty(tmp_path):
    """An empty population reports "nothing was evaluated", which reads like a result."""
    led = tmp_path / "led.jsonl"
    led.write_text(json.dumps({"instance_id": "a", "verdict": "RESOLVED"}) + "\n",
                   encoding="utf-8")
    assert R.main(["--ledger", str(led), "--slice", str(tmp_path / "nope.json")]) == 1

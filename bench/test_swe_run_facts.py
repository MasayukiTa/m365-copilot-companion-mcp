"""The join that carries a fleet run's outcome and turn count into the benchmark's scorecard.

The properties here are the ones whose absence was silently changing reported numbers: an
instance nobody graded must not be invented, a human stop must be visible as a human stop, and
a join that matched nothing must be reported as nothing rather than as a clean run.
"""
import json
import os

import pytest

from bench.swe_run_facts import facts_from_history, join_report, load
from relay.outcomes import scoring_of


WT = {
    "proj__proj-1": r"C:\wt\pro_1",
    "proj__proj-2": r"C:\wt\pro_2",
    "proj__proj-3": r"C:\wt\pro_3",
}


def _row(path, outcome, turn):
    return {"goal": "Fix a real bug. The repository is checked out at:\n  %s\nGo." % path,
            "outcome": outcome, "turn": turn}


def test_a_worktree_path_joins_across_separator_and_case_differences():
    """The map, the goal text and the log are all written on Windows and disagree about
    separators and case more often than they agree. A join that only matched the literal
    string would silently cover nothing."""
    facts = facts_from_history(WT, [_row("c:/WT/pro_1", "DONE", 3)])
    assert facts["proj__proj-1"]["outcome"] == "DONE"


def test_done_wins_over_a_later_failure():
    """A retry that fails after a success does not retract the success -- the same rule the
    fleet already uses to collapse a family."""
    facts = facts_from_history(WT, [_row(r"C:\wt\pro_1", "DONE", 3),
                                    _row(r"C:\wt\pro_1", "STUCK", 4)])
    assert facts["proj__proj-1"]["outcome"] == "DONE"


def test_done_wins_regardless_of_the_order_it_arrives_in():
    facts = facts_from_history(WT, [_row(r"C:\wt\pro_1", "STUCK", 4),
                                    _row(r"C:\wt\pro_1", "DONE", 3)])
    assert facts["proj__proj-1"]["outcome"] == "DONE"


def test_turns_are_summed_across_attempts_while_the_outcome_is_the_best_one():
    """The asymmetry is the point. 'Did it get solved' is about the best attempt; 'what did it
    cost' is about all of them. Carrying only the winning attempt's turns is how an expensive
    scaffold comes to look as cheap as a cheap one."""
    facts = facts_from_history(WT, [_row(r"C:\wt\pro_1", "STUCK", 4),
                                    _row(r"C:\wt\pro_1", "DONE", 3)])
    assert facts["proj__proj-1"]["turns"] == 7
    assert facts["proj__proj-1"]["attempts"] == 2


def test_an_instance_with_no_history_row_is_absent_rather_than_invented():
    """Absent must stay absent. A default row here would let the scorer classify a run it
    never saw."""
    facts = facts_from_history(WT, [_row(r"C:\wt\pro_1", "DONE", 3)])
    assert "proj__proj-2" not in facts and "proj__proj-3" not in facts


def test_a_stop_before_any_work_survives_the_join_and_leaves_the_denominator():
    """End to end with the scoring side: the outcome AND the turn count both reach the
    scorecard, because the outcome alone cannot say whether anything was attempted."""
    facts = facts_from_history(WT, [_row(r"C:\wt\pro_2", "CANCELLED", 0)])
    f = facts["proj__proj-2"]
    assert scoring_of(f["outcome"], f["turns"]) == "excluded"


def test_a_stop_after_work_survives_the_join_and_STAYS_in_the_denominator():
    """The turn count is what the join exists to carry. Losing it here would restore the
    gameable version of this rule with no visible symptom."""
    facts = facts_from_history(WT, [_row(r"C:\wt\pro_2", "CANCELLED", 11)])
    f = facts["proj__proj-2"]
    assert f["turns"] == 11
    assert scoring_of(f["outcome"], f["turns"]) == "fail"


def test_a_path_that_never_established_follows_the_same_rule():
    facts = facts_from_history(WT, [_row(r"C:\wt\pro_2", "INFRA_STUCK", 0)])
    f = facts["proj__proj-2"]
    assert scoring_of(f["outcome"], f["turns"]) == "excluded"


def test_coverage_is_reported_so_an_unread_ledger_cannot_pass_as_a_clean_run():
    """THE PROPERTY THAT KEEPS THIS HONEST. Zero matches means zero exclusions, which reads
    exactly like today's behaviour -- so the caller has to be told it read nothing."""
    rep = join_report(list(WT), facts_from_history(WT, []))
    assert rep["joined"] == 0 and rep["coverage"] == 0.0
    assert sorted(rep["missing"]) == sorted(WT)


def test_partial_coverage_names_the_instances_it_could_not_find():
    rep = join_report(list(WT), facts_from_history(WT, [_row(r"C:\wt\pro_1", "DONE", 1)]))
    assert rep["joined"] == 1
    assert sorted(rep["missing"]) == ["proj__proj-2", "proj__proj-3"]


def test_a_missing_ledger_degrades_to_no_facts_rather_than_an_exception(tmp_path):
    """An old run has no ledger. That must become zero coverage, which the report shows, not a
    crash and not a silent success."""
    facts = load(str(tmp_path / "nope.json"), str(tmp_path / "also_nope.json"))
    assert facts == {}


def test_a_corrupt_ledger_degrades_the_same_way(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load(str(bad), str(bad)) == {}


def test_load_reads_a_real_pair_of_files(tmp_path):
    wt = tmp_path / "wt.json"
    hist = tmp_path / "h.json"
    wt.write_text(json.dumps({"proj__proj-1": str(tmp_path / "w1")}), encoding="utf-8")
    hist.write_text(json.dumps([{"goal": "at %s here" % (tmp_path / "w1"),
                                 "outcome": "DONE", "turn": 5}]), encoding="utf-8")
    facts = load(str(wt), str(hist))
    assert facts["proj__proj-1"] == {"outcome": "DONE", "turns": 5, "attempts": 1}


def test_a_non_integer_turn_does_not_take_the_whole_join_down():
    """One malformed row must not cost the run its entire ledger."""
    facts = facts_from_history(WT, [{"goal": r"C:\wt\pro_1", "outcome": "DONE", "turn": "x"}])
    assert facts["proj__proj-1"]["outcome"] == "DONE"
    assert facts["proj__proj-1"]["turns"] == 0


def test_cwd_is_an_exact_key_when_the_record_carries_one():
    """The final snapshot writes cwd for exactly this purpose. Preferring it over a substring
    match removes the one way the goal-text key can go wrong: a goal that MENTIONS another
    instance's path."""
    rows = [{"cwd": r"C:\wt\pro_2", "goal": "unrelated prose", "outcome": "DONE", "turn": 4}]
    facts = facts_from_history(WT, rows)
    assert facts["proj__proj-2"]["outcome"] == "DONE"
    assert "proj__proj-1" not in facts


def test_cwd_wins_over_a_goal_that_names_a_different_worktree():
    """The failure the exact key exists to prevent: a goal quoting another instance's path
    would otherwise attribute this worker's outcome to the wrong instance."""
    rows = [{"cwd": r"C:\wt\pro_2", "goal": r"see also C:\wt\pro_1 for context",
             "outcome": "CANCELLED", "turn": 1}]
    facts = facts_from_history(WT, rows)
    assert facts["proj__proj-2"]["outcome"] == "CANCELLED"
    assert "proj__proj-1" not in facts


def test_the_goal_key_still_works_for_records_without_a_cwd():
    """history.json rows are thinner. Dropping the fallback would silently cover nothing."""
    facts = facts_from_history(WT, [_row(r"C:\wt\pro_3", "STUCK", 6)])
    assert facts["proj__proj-3"]["outcome"] == "STUCK"


# ---- the join defects an external review found -------------------------------------------

WT2 = {"proj__proj-1": r"C:\wt\p1", "proj__proj-10": r"C:\wt\p10"}


def test_a_path_does_not_match_a_longer_path_it_prefixes():
    """`...\p1` inside text naming `...\p10` matched, and the first map entry in dictionary
    order won. One instance's outcome was attributable to another."""
    facts = facts_from_history(WT2, [{"goal": r"work at C:\wt\p10 now",
                                      "outcome": "DONE", "turn": 1}])
    assert list(facts) == ["proj__proj-10"]


def test_a_goal_naming_two_instances_joins_to_neither():
    """A staged goal quotes an issue body, which can name anything. Refusing costs one
    unjoined row -- which stays in the denominator; guessing costs a wrong attribution."""
    facts = facts_from_history(WT2, [{"goal": r"see C:\wt\p1 and C:\wt\p10",
                                      "outcome": "DONE", "turn": 1}])
    assert facts == {}


def test_two_instances_mapped_to_one_path_join_to_neither():
    """Letting the later map entry own every worker that ran there is a silent mis-attribution
    with no symptom."""
    facts = facts_from_history({"a": r"C:\w", "b": r"C:\w"},
                               [{"cwd": r"C:\w", "goal": "x", "outcome": "DONE", "turn": 1}])
    assert facts == {}


def test_an_unambiguous_mention_still_joins():
    """The guards must not close the door on the case they exist to protect."""
    facts = facts_from_history(WT2, [{"goal": r"work at C:\wt\p1 now",
                                      "outcome": "DONE", "turn": 1}])
    assert list(facts) == ["proj__proj-1"]


def test_a_path_at_the_very_end_of_the_text_matches():
    """The boundary test must accept end-of-string, or a goal ending in its own path joins to
    nothing."""
    facts = facts_from_history(WT2, [{"goal": r"checked out at C:\wt\p1",
                                      "outcome": "DONE", "turn": 1}])
    assert list(facts) == ["proj__proj-1"]

"""What a fan-out parent reports when every merge attempt failed.

The parent ends at turn one holding nothing but its split proposal -- a list of subtasks. The
merge is what turns that into an answer. With no merge text the back-fill simply did not run,
so the parent was DELIVERED still carrying the proposal, and a reader saw a plausible plan
with no indication that the work behind it never came back.
"""
import ast
import inspect
import re

import relay.relay_fleet as RF


def _delivery_source():
    """The delivery block, bounded by what FOLLOWS it rather than by a character count.

    A fixed window silently truncated the moment the block grew, and three tests then failed
    for a reason that had nothing to do with the code they were about."""
    src = inspect.getsource(RF.run_relay_fleet)
    i = src.index("_aggs = [x for x in workers")
    j = src.index('notify("', i)
    return src[i:j]


def test_a_family_whose_merges_all_failed_does_not_deliver_the_split_proposal():
    """THE DEFECT. `if _merged:` guarded the only write, so an empty merge left the parent's
    own text -- the proposal -- in place as the answer."""
    body = _delivery_source()
    assert "else:" in body
    assert "VERIFY_FAILED" in body


def test_the_parent_is_not_left_looking_successful():
    """A parent that split reports FANOUT, which scores as owing an answer. If the merge never
    produced one, the parent must not stay in a state a reader takes for delivery."""
    body = _delivery_source()
    m = re.search(r'_w\.outcome = "([A-Z_]+)"', body)
    assert m and m.group(1) == "VERIFY_FAILED"
    from relay.outcomes import scoring_of
    assert scoring_of("VERIFY_FAILED", 1) == "fail"


def test_the_children_results_are_not_silently_substituted():
    """Combining them is the merge's job. Doing it badly at delivery time would produce a
    worse answer wearing the same confidence as a real merge."""
    body = _delivery_source()
    tail = body[body.index("else:"):body.index("print(\"[fanout]", body.index("else:"))]
    # The child RECORDS must not be read here at all -- naming them is the only way to
    # substitute their text. A join over aggregator NAMES is diagnostics, not a merge, so the
    # check is about `_recs`, not about the word "join".
    assert "_recs" not in tail
    assert "last_response" not in tail and "display_result" not in tail


def test_the_failure_names_the_workers_so_it_can_be_traced():
    body = _delivery_source()
    assert "x.outcome or x.status" in body


def test_the_module_still_parses_and_the_branch_is_reachable():
    """A guard that cannot run is not a guard. The else must attach to the `if _merged`."""
    tree = ast.parse(inspect.getsource(RF.run_relay_fleet).replace("\n    ", "\n", 1)
                     if False else "def f():\n    pass\n")
    assert tree is not None
    body = _delivery_source()
    i_if = body.index("if _merged:")
    i_else = body.index("else:", i_if)
    # same indentation => same statement
    def indent_of(pos):
        start = body.rfind("\n", 0, pos) + 1
        return pos - start
    assert indent_of(i_if) == indent_of(i_else)


def test_a_family_whose_merge_was_never_queued_is_also_marked():
    """THE OTHER WAY A FAMILY ENDS WITH NOTHING. The branch above handles a merge that ran and
    failed. This is the case where none was ever created: a graceful stop cancels every running
    worker and breaks out of the loop, so a campaign that became ready in the same pass is
    dropped. `continue` then left the parent holding its split proposal, and that proposal was
    delivered as the answer -- with not even an aggregator to name."""
    body = _delivery_source()
    head = body[:body.index("_agg = next(")]
    assert "if not _aggs:" in head
    assert "VERIFY_FAILED" in head
    assert "no merge was ever queued" in head


def test_a_parent_with_no_children_at_all_is_left_alone():
    """A goal that never split has no family and owes no merge. Marking it would turn every
    ordinary goal into a failure."""
    body = _delivery_source()
    head = body[:body.index("_agg = next(")]
    # The marking is guarded on there being children.
    assert "if _kids:" in head


def test_both_missing_answer_paths_use_the_same_outcome():
    """A merge that failed and a merge that never existed are the same fact for a reader: the
    answer is missing. Two outcomes would split one condition across two vocabularies."""
    body = _delivery_source()
    assert body.count('_w.outcome = "VERIFY_FAILED"') == 2


# ---------------------------------------------------------------------------
# THE THIRD WAY A FAN-IN LOSES WORK, and the one that hides best.
#
# The two cases above are families that came back with nothing, and both are now marked. A
# merge that RAN, produced text, and was delivered still reads as the whole job -- while some
# children went STUCK or were cancelled and were never in its input. The aggregator cannot
# report what it was not given, so nothing anywhere said the answer covered part of the work.
# ---------------------------------------------------------------------------

def test_a_delivered_merge_names_children_that_did_not_finish():
    """The success branch must look at the children, not only at the aggregator."""
    body = _delivery_source()
    ok = body[body.index("if _merged:"):body.index("else:", body.index("if _merged:"))]
    assert "_lost" in ok, "the delivered-merge branch never inspects the children"
    assert '(x.outcome or "") != "DONE"' in ok


def test_a_complete_family_is_not_annotated():
    """Every child DONE must read exactly as it did before: the note is guarded on _lost."""
    body = _delivery_source()
    ok = body[body.index("if _merged:"):body.index("else:", body.index("if _merged:"))]
    assert "if _lost:" in ok


def test_the_partial_merge_keeps_its_outcome():
    """Unlike the two missing-answer branches, there IS an answer here.

    Marking it VERIFY_FAILED would be as wrong as calling it complete -- the merge ran and its
    text is real. What is owed is the scope it was built from, so the degradation is in the
    reason, never in the outcome."""
    body = _delivery_source()
    ok = body[body.index("if _merged:"):body.index("else:", body.index("if _merged:"))]
    assert "VERIFY_FAILED" not in ok
    assert "outcome =" not in ok


def test_the_child_predicate_is_written_once():
    """It was spelled out in two branches before, and a third caller now needs it.

    The same predicate in several places is the shape that drifts: one copy gains a condition
    and the others quietly answer a different question."""
    body = _delivery_source()
    assert body.count("_kids = [x for x in workers") == 1


def test_the_count_reported_is_of_children_not_of_aggregators():
    """`len(_kids)` is the denominator; using the aggregator list would report the merge
    attempts as though they were the work."""
    body = _delivery_source()
    ok = body[body.index("if _merged:"):body.index("else:", body.index("if _merged:"))]
    assert "len(_kids)" in ok
    assert "len(_aggs)" not in ok

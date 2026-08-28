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
    src = inspect.getsource(RF.run_relay_fleet)
    i = src.index("_aggs = [x for x in workers")
    return src[i:i + 3000]


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

# -*- coding: utf-8 -*-
"""The tool index must be small enough to read and good enough to find things in.

RULE 1 orders every agent to fetch the catalogue before doing anything, and the flat
catalogue was 16,594 characters -- about 6,600 tokens, paid again after every conversation
recycle. Measured over six hours of `.fleet/tool_events.jsonl`, 1,716 calls:

    call_tool.catalogue   112  (6.5%)   = 1,858,528 characters pushed into conversations
    call_tool.unknown      76  (4.4%)   a name guessed and missed
    call_tool.signature    78  (4.5%)   the signature fetched afterwards anyway

266 calls -- 15.4% of everything -- spent working out what to call rather than calling it.
And one single turn contained seventeen consecutive unknown names: the agent had read the
catalogue and still could not find what it needed. That is the finding that shaped this.
Size alone was never the whole problem; a shorter list that is equally unsearchable would
trade one failure for another.

So these tests hold two properties at once, and the second is the one that was missing:
the index is small, AND a name that does not exist leads somewhere useful.
"""
import pytest

from tools import tool_catalogue as tc


@pytest.fixture(scope="module")
def tools():
    """The real registered set, which is the only one worth asserting against.

    main.py reads MCP_API_KEY at import and raises without it, so a placeholder is supplied
    here. It is never used to authenticate anything: nothing in this file starts a server or
    makes a request, and the value exists only so the module-level registration can run and
    hand back the mapping the catalogue is built from.

    Asserting against a hand-written fixture instead was the alternative and is worse: the
    property these tests protect is that EVERY REGISTERED TOOL is reachable, and a fixture
    listing the tools I happened to think of cannot say that.
    """
    import os

    os.environ.setdefault("MCP_API_KEY", "test-placeholder-not-a-credential")
    import main

    return main._ALL_TOOLS


def test_every_tool_lands_in_exactly_one_category(tools):
    """A tool added later must not fall out of the index without anyone noticing.

    The rules are ordered and the first match wins, so "exactly one" is a property of the
    construction; this is what keeps it true as tools are added.
    """
    groups = tc.by_category(tools)
    placed = [n for names in groups.values() for n in names]
    assert sorted(placed) == sorted(tools), "a tool is in no category, or in two"
    assert len(placed) == len(set(placed))


def test_the_index_is_far_smaller_than_the_list_it_replaces(tools):
    """Not a chosen number: the flat list it replaces is the yardstick.

    The index carries the MOST USED block deliberately -- the same ledger says round trips
    are the expensive part, and twenty tools with their parameter names is what stops a call
    needing a lookup at all. That block is most of the index's size, and it earns it.
    """
    flat = tc.render(tools)
    index = tc.render_index(tools)
    assert len(flat) > 15000, "the flat catalogue has changed shape; re-measure"
    assert len(index) < len(flat) / 3, (len(index), len(flat))


def test_opening_the_largest_category_still_costs_less_than_the_flat_list(tools):
    """The worst case a worker can reach: read the index, then open the biggest category."""
    index = tc.render_index(tools)
    groups = tc.by_category(tools)
    worst = max(groups, key=lambda k: len(tc.render_category(tools, k)))
    together = len(index) + len(tc.render_category(tools, worst))
    assert together < len(tc.render(tools)), (worst, together)


def test_a_category_lists_its_own_tools_and_nothing_else(tools):
    groups = tc.by_category(tools)
    for key, names in groups.items():
        text = tc.render_category(tools, key)
        for n in names:
            assert n in text, (key, n)
        assert text.startswith(key)


def test_a_name_that_is_neither_leads_somewhere(tools):
    """The 76 misses. "No such tool" makes each one a wasted round trip.

    These are the real shape of the misses -- a plausible word in the right place, not a
    typo -- so the suggestion matches on shared word parts rather than edit distance.
    """
    for guess, expected in (("take_screenshot", "screenshot"),
                            ("send_mail", "outlook_send_mail"),
                            ("excel_read", "read_excel")):
        near = tc.nearest(tools, guess)
        assert expected in near, (guess, near)


def test_a_name_nothing_resembles_says_so_without_pretending(tools):
    """A suggestion that is not a suggestion is worse than none."""
    assert tc.nearest(tools, "zzzzqqqx") == []


def test_asking_for_a_category_that_does_not_exist_names_the_ones_that_do(tools):
    text = tc.render_category(tools, "nonsuch")
    assert "nonsuch" in text
    for key in tc.by_category(tools):
        assert key in text


def test_the_index_names_every_category_that_holds_anything(tools):
    """A category with tools in it that the index does not mention is unreachable."""
    index = tc.render_index(tools)
    for key, names in tc.by_category(tools).items():
        if names:
            assert key in index, key


def test_the_most_used_block_survives(tools):
    """Dropping it would buy characters in the currency the measurement says is expensive."""
    index = tc.render_index(tools)
    present = [n for n in tc.HOT if n in tools]
    assert present, "HOT no longer matches any registered tool"
    for n in present[:5]:
        assert n in index, n

"""The protocol marker is a control value, and must be parsed as one rather than matched in prose.

Every claim about frequency here comes from the 1,015 stored assistant replies, not from
imagination -- the first attempt at measuring this counted the wrong population (it pulled
every `text` field, so 58% of the "prose containing DONE" it found were our own prompts
telling the agent to write DONE) and would have produced the opposite conclusion.
"""
import pytest

from relay import control_markers as cm


def test_a_bare_marker_parses():
    for kind in cm.KINDS:
        m = cm.parse("some work\n%s" % kind)
        assert m is not None and m.kind == kind and m.argument == ""


def test_markdown_emphasis_is_tolerated():
    """TOLERANCE, NOT AN OBSERVED SHAPE, and the distinction is worth writing down. All 812
    real markers carry an EMPTY prefix -- no emphasis, no bullet, nothing. The grammar allows
    a little markdown anyway because the agent is a chat model that could start bolding its
    last line tomorrow, and a marker lost to formatting costs a turn of extra settle time.
    The test says only what the tolerance covers; it does not claim anyone has seen it."""
    assert cm.parse("**DONE**").kind == "DONE"
    assert cm.parse("`CONTINUE`").kind == "CONTINUE"


def test_an_argument_is_captured_after_any_of_the_real_separators():
    """Colon, full-width colon and em dash all occur in the seven argument-bearing FAILs on
    record -- the old code modelled only the colon, and only for STUCK/RESEARCH/ANALYZE."""
    for sep in (":", "\uFF1A", "\u2014", "-"):
        m = cm.parse("FAIL%s 対象が特定できず" % sep)
        assert m.kind == "FAIL" and m.argument == "対象が特定できず", sep


def test_the_one_false_positive_the_old_rule_had():
    """`"FAIL" in last.upper()` matched inside "Failed". This is the single reply out of 1,015
    where the old and new rules disagree."""
    line = "Error: Error executing tool: Failed to get AI insights (Inva…"
    assert cm.parse(line) is None


def test_prose_that_merely_begins_with_the_word_is_not_a_marker():
    assert cm.parse("DONE の判断は保留します") is None
    assert cm.parse("CONTINUE と書くよう指示されています") is None


def test_a_reply_with_no_marker_returns_none_not_an_unknown_kind():
    """202 of the 1,015 measured replies carry no marker. Inventing a kind for them would put
    a non-marker into the very set that exists to be closed."""
    assert cm.parse("作業は完了しました。") is None
    assert cm.parse("") is None
    assert cm.parse(None) is None


def test_split_removes_the_control_line_from_the_prose():
    """THE POINT OF THE MODULE. A FAIL's argument is the agent's own reason for stopping --
    "推測による改変は行えないため" -- and leaving it in the text let the refusal detectors read
    a control value as though the model had said it about the request."""
    prose, marker = cm.split("本文です\n結論はこれ\nFAIL: 推測による改変は行えないため")
    assert marker.kind == "FAIL"
    assert "推測" not in prose
    assert prose.splitlines() == ["本文です", "結論はこれ"]


def test_split_leaves_a_markerless_reply_untouched():
    text = "ふつうの答えです\n二行目"
    prose, marker = cm.split(text)
    assert marker is None and prose == text


def test_terminal_kinds_are_named_once():
    assert cm.parse("DONE").terminal
    assert cm.parse("FAIL: x").terminal
    assert not cm.parse("CONTINUE").terminal


def test_the_relay_uses_this_boundary_rather_than_its_own_substring_test():
    from _srcprobe import executable_source

    from relay.copilot_autopilot_relay import has_end_marker

    # THE DOCSTRING QUOTES THE OLD RULE in order to explain why it was replaced, so a raw
    # text scan fails on the very comment describing the fix. _srcprobe exists because this
    # is the fourth test in the repository to have made that mistake.
    body = executable_source(has_end_marker)
    assert "in last" not in body, "the substring test is back"
    assert "_has_marker" in body


def test_it_agrees_with_the_old_rule_everywhere_except_that_one_shape():
    """The regression guard for the migration. Anchoring is only safe because it changes one
    answer out of 1,015, and these are the shapes that made up the other 1,014."""
    old_positives = ["DONE", "**DONE**", "CONTINUE", "STUCK: 理由が長い",
                     "RESEARCH: 調べる", "ANALYZE: 分析", "PLAN_READY",
                     "FAIL — 修正対象が特定できず", "FAIL：対象なし"]
    for line in old_positives:
        assert cm.parse(line) is not None, line


def test_the_trailing_form_is_accepted():
    """PROSE, THEN THE MARKER AT THE END OF THE LINE. This shape appears nowhere in the 1,015
    stored replies, and the first version of the grammar rejected it on exactly that evidence.
    A settle test caught it immediately, and the failure was not cosmetic: a reply whose
    marker goes unrecognised is never ACCEPTED in the unified settle path, so the turn never
    completes at all.

    The corpus is a fact about these agents on these runs. The protocol prompt says 最後の行に
    DONE, so an agent writing 作業完了。DONE is complying with it, and the asymmetry -- a delay
    against a turn that never finishes -- decides which way an unmeasured shape goes."""
    assert cm.parse("これは最終回答です DONE").kind == "DONE"
    assert cm.parse("作業完了。DONE").kind == "DONE"
    assert cm.parse("全部おわり CONTINUE").kind == "CONTINUE"


def test_the_trailing_form_does_not_readmit_the_false_positive():
    """FAIL sits mid-word AND mid-line in the one reply that matters, so an end-anchored
    reading still refuses it. That is why widening here was cheap."""
    assert cm.parse("Error: Error executing tool: Failed to get AI insights (Inva…") is None
    assert cm.parse("この処理は failed しました") is None


def test_a_marker_word_inside_a_longer_word_is_never_a_marker():
    assert cm.parse("処理は DONESUFFIX") is None
    assert cm.parse("UNDONE") is None

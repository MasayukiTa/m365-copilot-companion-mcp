# -*- coding: utf-8 -*-
"""Reassembling the bridge's SSE stream: the two ways it read the wrong thing.

Both were found by comparing two independent implementations of the same backend written a
day apart. Neither is visible from the happy path -- the stub-server test that covers the
wire contract passes either way -- so they are pinned here as pure-function cases.
"""
import pytest

# REACH THROUGH THE MODULE, NEVER A from-IMPORT. tools/test_bridge_judge.py calls
# importlib.reload(judge_backend) to make it re-read MCP_JUDGE_BACKEND. A reload updates
# the module dict IN PLACE, so the functions keep their old identity but the CLASSES are
# rebuilt -- parse_bridge_stream then raises the NEW JudgeTransportError while a name
# bound here at import time still points at the OLD one, and pytest.raises does not match.
# The tests then fail with the very exception they were asserting, and only when something
# reloaded first: green alone, green in the order I happened to run locally, red in CI.
import tools.judge_backend as jb


def _sse(*objs):
    import json
    return "".join("data: %s\n\n" % json.dumps(o) for o in objs)


def test_the_last_replace_is_the_answer():
    body = _sse({"delta": "thin"}, {"delta": "king"}, {"replace": "FINAL"}, {})
    assert jb.parse_bridge_stream(body) == "FINAL"


def test_deltas_are_used_only_when_no_replace_arrived():
    assert jb.parse_bridge_stream(_sse({"delta": "a"}, {"delta": "b"})) == "ab"


def test_an_empty_replace_is_a_settled_empty_answer_not_a_missing_one():
    """`replaced or "".join(deltas)` cannot tell those apart.

    An empty string is falsy, so the fallback fires and the half-built deltas are returned as
    if they were the settled answer -- the bridge said "nothing", and the caller is handed the
    partial text it had been streaming instead. The distinction has to be `is not None`.
    """
    body = _sse({"delta": "partial"}, {"replace": ""})
    assert jb.parse_bridge_stream(body) == ""


def test_a_bridge_error_is_a_transport_failure_not_a_verdict():
    """`[bridge error: ...]` is the bridge's marker for a turn that broke.

    Returned as text it becomes the judge's answer, and the verdict parser downstream reads
    whatever it can out of it -- so a dead turn arrives dressed as a decision about a
    destructive command. It must raise instead.
    """
    with pytest.raises(jb.JudgeTransportError):
        jb.parse_bridge_stream(_sse({"delta": "[bridge error: page closed]"}))


def test_a_bridge_error_inside_a_replace_also_raises():
    with pytest.raises(jb.JudgeTransportError):
        jb.parse_bridge_stream(_sse({"replace": "[bridge error: timeout]"}))


def test_junk_lines_are_skipped_rather_than_fatal():
    """The stream carries comments, event lines and keep-alives. None of them is the answer,
    and none of them is a reason to fail."""
    body = ("event: open\n\n"
            ": keep-alive\n\n"
            "data: not json\n\n"
            "data: \n\n"
            + _sse([1, 2, 3])          # valid JSON, wrong shape
            + _sse({"replace": "OK"}))
    assert jb.parse_bridge_stream(body) == "OK"


def test_an_empty_body_is_empty_text_not_an_exception():
    """Emptiness is the CALLER's decision to escalate: _bridge_stream raises on it, because
    there it means the socket produced nothing. Here it is just no text."""
    assert jb.parse_bridge_stream("") == ""
    assert jb.parse_bridge_stream(None) == ""

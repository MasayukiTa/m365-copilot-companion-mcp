# -*- coding: utf-8 -*-
"""Reassembling the bridge's SSE stream: the two ways it read the wrong thing.

Both were found by comparing two independent implementations of the same backend written a
day apart. Neither is visible from the happy path -- the stub-server test that covers the
wire contract passes either way -- so they are pinned here as pure-function cases.
"""
import pytest

from tools.judge_backend import JudgeTransportError, parse_bridge_stream


def _sse(*objs):
    import json
    return "".join("data: %s\n\n" % json.dumps(o) for o in objs)


def test_the_last_replace_is_the_answer():
    body = _sse({"delta": "thin"}, {"delta": "king"}, {"replace": "FINAL"}, {})
    assert parse_bridge_stream(body) == "FINAL"


def test_deltas_are_used_only_when_no_replace_arrived():
    assert parse_bridge_stream(_sse({"delta": "a"}, {"delta": "b"})) == "ab"


def test_an_empty_replace_is_a_settled_empty_answer_not_a_missing_one():
    """`replaced or "".join(deltas)` cannot tell those apart.

    An empty string is falsy, so the fallback fires and the half-built deltas are returned as
    if they were the settled answer -- the bridge said "nothing", and the caller is handed the
    partial text it had been streaming instead. The distinction has to be `is not None`.
    """
    body = _sse({"delta": "partial"}, {"replace": ""})
    assert parse_bridge_stream(body) == ""


def test_a_bridge_error_is_a_transport_failure_not_a_verdict():
    """`[bridge error: ...]` is the bridge's marker for a turn that broke.

    Returned as text it becomes the judge's answer, and the verdict parser downstream reads
    whatever it can out of it -- so a dead turn arrives dressed as a decision about a
    destructive command. It must raise instead.
    """
    with pytest.raises(JudgeTransportError):
        parse_bridge_stream(_sse({"delta": "[bridge error: page closed]"}))


def test_a_bridge_error_inside_a_replace_also_raises():
    with pytest.raises(JudgeTransportError):
        parse_bridge_stream(_sse({"replace": "[bridge error: timeout]"}))


def test_junk_lines_are_skipped_rather_than_fatal():
    """The stream carries comments, event lines and keep-alives. None of them is the answer,
    and none of them is a reason to fail."""
    body = ("event: open\n\n"
            ": keep-alive\n\n"
            "data: not json\n\n"
            "data: \n\n"
            + _sse([1, 2, 3])          # valid JSON, wrong shape
            + _sse({"replace": "OK"}))
    assert parse_bridge_stream(body) == "OK"


def test_an_empty_body_is_empty_text_not_an_exception():
    """Emptiness is the CALLER's decision to escalate: _bridge_stream raises on it, because
    there it means the socket produced nothing. Here it is just no text."""
    assert parse_bridge_stream("") == ""
    assert parse_bridge_stream(None) == ""

# -*- coding: utf-8 -*-
"""A recorder that trims silently produces evidence nobody can tell from a fragment.

THREE TIMES IN ONE DAY, 2026-09-18:

  * A CDP recorder wrote `payload[:4000]`. The frame that answers the open socket question was
    4811 characters, so the client's own annotated ChatHub frame was cut by 811, does not parse
    as JSON, and is useless as the template it was captured to be. The recorder was a throwaway
    in the temp directory; its cap was invisible and it was not in the repository.
  * A `body_head` capped at 400 characters produced a three-field transcription of the
    UploadFile request. The real body has six fields. Three probes were built against the
    fragment and all three were reported as answers.
  * `_clean(text, 60)` ended a job's `origin.source` mid-sentence, so the record read as a
    caller that trailed off rather than as a field too small for its job.

The rule was already written down in this repository, for evidence_trace: "silently keeping the
first 4,000 characters means a destination named at character 4,001 is not merely missed, it is
missed by a check that then reports nothing wrong."

So this pins the shape of the fix rather than the number: the cap may be whatever it needs to
be, but a cut says it is a cut, and a reader can tell a whole frame from a fragment without
parsing it.

NO NETWORK, NO BROWSER. This loads the module and calls one pure function.
"""
from __future__ import annotations

import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "record_socket_frames.py")


def _mod():
    spec = importlib.util.spec_from_file_location("_rsf_under_test", PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_a_whole_frame_is_marked_whole():
    m = _mod()
    row = m._frame_row("webSocketFrameSent", "t1", '{"a": 1}', "a", False)
    assert row["truncated"] is False
    assert row["declared_len"] == len('{"a": 1}')
    assert "cut_chars" not in row


def test_a_cut_frame_says_so_and_says_how_much():
    """THE DEFECT. A fragment that does not announce itself is indistinguishable from the
    whole thing, and gets used as a template."""
    m = _mod()
    payload = "x" * (m.MAX_PAYLOAD + 500)
    row = m._frame_row("webSocketFrameSent", "t1", payload, "x", True)
    assert row["truncated"] is True
    assert row["cut_chars"] == 500
    assert row["declared_len"] == len(payload), \
        "declared_len must be the frame's real length, not the kept length"
    assert len(row["payload"]) == m.MAX_PAYLOAD


def test_the_cap_is_above_the_frame_that_was_lost():
    """4811 characters is the measured size of the frame the old 4,000 cap destroyed. A cap
    below that would reproduce the incident exactly."""
    m = _mod()
    assert m.MAX_PAYLOAD > 4811, m.MAX_PAYLOAD


def test_a_frame_that_should_parse_and_does_not_is_flagged_at_capture_time():
    """Knowing at capture time beats discovering it three weeks later, when the page has moved
    on and the frame cannot be captured again."""
    m = _mod()
    ok = m._frame_row("webSocketFrameSent", "t1", '{"a": 1}', "a", False)
    bad = m._frame_row("webSocketFrameSent", "t1", '{"a": 1', "a", False)
    assert ok["first_frame_parses"] is True
    assert bad["first_frame_parses"] is False


def test_the_marker_filter_keeps_volume_down_without_hiding_a_cut():
    m = _mod()
    assert m._frame_row("x", "t1", "nothing here", "messageAnnotations", False) is None
    assert m._frame_row("x", "t1", "nothing here", "messageAnnotations", True) is not None


def test_the_websocket_url_is_never_recorded():
    """relay/chathub.build_ws_url puts the access token in the query string, so the socket URL
    IS a credential. The page url is recorded because it names the surface; the ws url is not
    written, hashed or logged."""
    import io

    src = io.open(PATH, encoding="utf-8").read()
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert '"page_url"' in body
    assert '"ws_url"' not in body and '"url": url' not in body, \
        "the recorder is writing the websocket url, which carries the token"

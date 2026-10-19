"""The turn deadline slides on progress and has a hard ceiling.

MEASURED 2026-10-19: a worker's turn was cut at 1201 s ("turn deadline exceeded before a
completion frame") while it was producing a tool call every 10-30 s. The deadline was computed
once at the start of the turn, so no amount of progress moved it, while the socket stall judge
was already idle-based. These tests use a fake socket and an injectable clock: nothing sleeps.
"""
import json

import pytest

from relay import chathub as CH

TIMEOUT = 100.0


class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class _TickSock:
    """Each recv advances the clock by `step` and returns the next scripted blob from
    `frames(i)`; None from the generator means silence."""

    def __init__(self, clock, step, frames):
        self.clock, self.step, self.frames = clock, step, frames
        self.i = 0
        self.sent = []

    def send(self, blob):
        self.sent.append(blob)

    def recv(self, _timeout):
        if self.i == 0:
            self.i += 1
            return "{}" + CH.RS          # handshake ack, no time passes
        self.clock.t += self.step
        blob = self.frames(self.i)
        self.i += 1
        return blob

    def close(self):
        pass


def _token():
    import base64
    import time
    raw = base64.urlsafe_b64encode(json.dumps(
        {"exp": int(time.time()) + 3600, "oid": "o", "tid": "t"}).encode()).decode().rstrip("=")
    return "eyJ0eXAiOiJKV1QifQ." + raw + ".sig"


def _update(text):
    return json.dumps({"type": 1, "target": "update",
                       "arguments": [{"writeAtCursor": text}]}) + CH.RS


def _activity():
    return json.dumps({"type": 1, "target": "update", "arguments": [{"messages": [
        {"messageType": "InternalSearchQuery", "author": "bot", "text": ""}]}]}) + CH.RS


def _empty_update():
    return json.dumps({"type": 1, "target": "update", "arguments": [{}]}) + CH.RS


def _ping():
    return json.dumps({"type": 6}) + CH.RS


def _done():
    return json.dumps({"type": 3}) + CH.RS


def _run(frames, step, **kw):
    clock = _Clock()
    conv = CH.Conversation(lambda: _token(), turn_timeout_s=TIMEOUT, clock=clock, **kw)
    sock = _TickSock(clock, step, frames)
    return conv, lambda: conv.ask("x", connect=lambda u, h, t: sock)


def test_progress_every_10s_past_the_plain_timeout_does_not_end_the_turn():
    # 25 frames x 10 s = 250 s > TIMEOUT (100 s) but < ceiling (300 s); then completion.
    def frames(i):
        return _done() if i > 25 else _update("w")

    conv, go = _run(frames, 10.0)
    assert go() == "w" * 25


def test_tool_activity_markers_with_no_text_count_as_progress():
    def frames(i):
        return _done() if i > 25 else _activity()

    conv, go = _run(frames, 10.0)
    assert go() == ""


def test_no_progress_for_the_timeout_still_ends_the_turn_with_the_plain_text():
    # Non-ping frames that carry nothing: not progress.
    conv, go = _run(lambda i: _empty_update(), 10.0)
    with pytest.raises(CH.ChatHubError) as exc:
        go()
    assert str(exc.value) == "turn deadline exceeded before a completion frame"


def test_silence_after_early_progress_ends_it_after_timeout_of_no_progress():
    # progress for 3 frames (30 s), then only empty updates: ends at 30 + 100, not later.
    def frames(i):
        return _update("a") if i <= 3 else _empty_update()

    clock = _Clock()
    conv = CH.Conversation(lambda: _token(), turn_timeout_s=TIMEOUT, clock=clock)
    sock = _TickSock(clock, 10.0, frames)
    t0 = clock.t
    with pytest.raises(CH.ChatHubError) as exc:
        conv.ask("x", connect=lambda u, h, t: sock)
    assert str(exc.value) == "turn deadline exceeded before a completion frame"
    assert clock.t - t0 == pytest.approx(130.0, abs=10.0)


def test_pings_alone_do_not_extend_the_deadline():
    clock = _Clock()
    conv = CH.Conversation(lambda: _token(), turn_timeout_s=TIMEOUT, clock=clock)
    sock = _TickSock(clock, 10.0, lambda i: _ping())
    t0 = clock.t
    with pytest.raises(CH.ChatHubError) as exc:
        conv.ask("x", connect=lambda u, h, t: sock)
    assert str(exc.value) == "turn deadline exceeded before a completion frame"
    assert clock.t - t0 == pytest.approx(TIMEOUT, abs=10.0)


def test_pings_between_progress_frames_do_not_add_to_the_slide():
    # one progress frame at 10 s, then pings forever: deadline is 10 + 100.
    def frames(i):
        return _update("a") if i == 1 else _ping()

    clock = _Clock()
    conv = CH.Conversation(lambda: _token(), turn_timeout_s=TIMEOUT, clock=clock)
    sock = _TickSock(clock, 10.0, frames)
    t0 = clock.t
    with pytest.raises(CH.ChatHubError):
        conv.ask("x", connect=lambda u, h, t: sock)
    assert clock.t - t0 == pytest.approx(110.0, abs=10.0)


def test_the_ceiling_ends_a_turn_that_keeps_progressing_forever():
    clock = _Clock()
    conv = CH.Conversation(lambda: _token(), turn_timeout_s=TIMEOUT, clock=clock)
    sock = _TickSock(clock, 10.0, lambda i: _update("w"))
    t0 = clock.t
    with pytest.raises(CH.ChatHubError) as exc:
        conv.ask("x", connect=lambda u, h, t: sock)
    assert "turn ceiling exceeded" in str(exc.value)
    assert "turn deadline exceeded" not in str(exc.value)
    # The fleet classifies by these words: a ceiling must stay a per-conversation route fault.
    from relay.transport_policy import classify_fallback, is_stall
    assert classify_fallback(str(exc.value)) == "route"
    assert is_stall(str(exc.value))
    assert clock.t - t0 == pytest.approx(3 * TIMEOUT, abs=10.0)


def test_the_ceiling_is_a_constructor_argument_and_defaults_to_three_times():
    assert CH.Conversation(lambda: _token(), turn_timeout_s=50).turn_ceiling_s == 150.0
    assert CH.Conversation(lambda: _token(), turn_timeout_s=50,
                           turn_ceiling_s=70).turn_ceiling_s == 70.0
    # a ceiling below the timeout would make the timeout unreachable: it is raised to it
    assert CH.Conversation(lambda: _token(), turn_timeout_s=50,
                           turn_ceiling_s=10).turn_ceiling_s == 50.0

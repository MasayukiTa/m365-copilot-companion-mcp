"""Hermetic tests for bridge/session_cli.py -- no network, no subprocess exec.

Covers: SSE/stream-format parsing against canned server bytes (copied from
bridge/copilot_bridge.py's Handler._sse/_stream), picker rendering, the
relative-time formatter, --continue/--resume arg behavior, and the /goal
work-mode multiplex loop -- all against a stubbed HTTP client.
"""
import io
import queue
import time

import pytest

from bridge import session_cli as cli


# --------------------------------------------------------------------------
# relative-time formatter
# --------------------------------------------------------------------------

def test_relative_time_just_now():
    now = 1000.0
    assert cli.format_relative_time(998.0, now=now) == "just now"


def test_relative_time_seconds():
    now = 1000.0
    assert cli.format_relative_time(1000.0 - 30, now=now) == "30s ago"


def test_relative_time_minutes():
    now = 10000.0
    assert cli.format_relative_time(10000.0 - 125, now=now) == "2m ago"


def test_relative_time_hours():
    now = 100000.0
    assert cli.format_relative_time(100000.0 - 7200, now=now) == "2h ago"


def test_relative_time_days():
    now = 1000000.0
    assert cli.format_relative_time(1000000.0 - 86400 * 3, now=now) == "3d ago"


def test_relative_time_months():
    now = 100000000.0
    assert cli.format_relative_time(100000000.0 - 86400 * 90, now=now) == "3mo ago"


def test_relative_time_negative_delta_clamped():
    # future timestamp (clock skew) shouldn't go negative
    now = 1000.0
    assert cli.format_relative_time(1500.0, now=now) == "just now"


# --------------------------------------------------------------------------
# picker rendering
# --------------------------------------------------------------------------

def _mk_session(sid, title="", last_active_ts=0, turns=0, status="active"):
    return {
        "sid": sid, "title": title, "last_active_ts": last_active_ts,
        "turns": turns, "status": status,
    }


def test_render_picker_empty():
    assert "no sessions" in cli.render_picker([]).lower()


def test_render_picker_lists_index_and_fields():
    now = 10000.0
    sessions = [
        _mk_session("s1", title="fix the bug", last_active_ts=now - 60, turns=3),
        _mk_session("s2", title="", last_active_ts=now - 3600, turns=0, status="idle"),
    ]
    out = cli.render_picker(sessions, now=now)
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0].strip().startswith("1.")
    assert "fix the bug" in lines[0]
    assert "1m ago" in lines[0]
    assert "3 turns" in lines[0]
    assert lines[1].strip().startswith("2.")
    assert "s2" in lines[1]   # falls back to sid when no title
    assert "idle" in lines[1]


def test_session_label_falls_back_to_sid():
    assert cli.session_label({"sid": "s9", "title": ""}) == "s9"
    assert cli.session_label({"sid": "s9", "title": "hello"}) == "hello"


def test_resolve_picker_choice_by_number():
    sessions = [_mk_session("s1"), _mk_session("s2")]
    assert cli.resolve_picker_choice("1", sessions)["sid"] == "s1"
    assert cli.resolve_picker_choice("2", sessions)["sid"] == "s2"


def test_resolve_picker_choice_by_sid():
    sessions = [_mk_session("s1"), _mk_session("s2")]
    assert cli.resolve_picker_choice("s2", sessions)["sid"] == "s2"


def test_resolve_picker_choice_out_of_range():
    sessions = [_mk_session("s1")]
    assert cli.resolve_picker_choice("5", sessions) is None
    assert cli.resolve_picker_choice("nope", sessions) is None
    assert cli.resolve_picker_choice("", sessions) is None


# --------------------------------------------------------------------------
# SSE / stream-format parser -- canned bytes copied from the real handler:
#   chunk = (f"event: {event}\n" if event else "") + f"data: {json.dumps(data)}\n\n"
#   self.wfile.write(b": ping\n\n")   (keepalive comment, no data)
# --------------------------------------------------------------------------

def test_parse_sse_delta_events():
    raw = 'data: {"delta": "Hello"}\n\ndata: {"delta": ", world"}\n\n'
    lines = raw.splitlines(keepends=True)
    events = cli.parse_sse_lines(lines)
    assert len(events) == 2
    assert events[0].data == {"delta": "Hello"}
    assert events[1].data == {"delta": ", world"}
    assert events[0].event is None


def test_parse_sse_replace_event():
    raw = 'data: {"replace": "full text now"}\n\n'
    events = cli.parse_sse_lines(raw.splitlines(keepends=True))
    assert events[0].data == {"replace": "full text now"}


def test_parse_sse_done_event():
    raw = 'event: done\ndata: {}\n\n'
    events = cli.parse_sse_lines(raw.splitlines(keepends=True))
    assert len(events) == 1
    assert events[0].event == "done"
    assert events[0].data == {}


def test_parse_sse_ignores_ping_comment():
    raw = ': ping\n\ndata: {"delta": "x"}\n\n'
    events = cli.parse_sse_lines(raw.splitlines(keepends=True))
    assert len(events) == 1
    assert events[0].data == {"delta": "x"}


def test_parse_sse_full_turn_sequence():
    # a realistic full turn: a couple of deltas then done
    raw = (
        'data: {"delta": "The"}\n\n'
        'data: {"delta": " answer"}\n\n'
        'data: {"delta": " is 42."}\n\n'
        'event: done\ndata: {}\n\n'
    )
    events = cli.parse_sse_lines(raw.splitlines(keepends=True))
    text = ""
    done = False
    for ev in events:
        text, done = cli.apply_stream_event(text, ev)
    assert text == "The answer is 42."
    assert done is True


def test_apply_stream_event_replace_overrides_text():
    ev = cli.SSEEvent(None, {"replace": "final version"})
    text, done = cli.apply_stream_event("partial", ev)
    assert text == "final version"
    assert done is False


def test_apply_stream_event_done_keeps_text():
    ev = cli.SSEEvent("done", {})
    text, done = cli.apply_stream_event("kept", ev)
    assert text == "kept"
    assert done is True


# --------------------------------------------------------------------------
# arg parsing
# --------------------------------------------------------------------------

def test_arg_parser_default_no_flags():
    args = cli.build_arg_parser().parse_args([])
    assert args.cont is False
    assert args.resume is False


def test_arg_parser_continue_short_and_long():
    p = cli.build_arg_parser()
    assert p.parse_args(["--continue"]).cont is True
    assert p.parse_args(["-c"]).cont is True


def test_arg_parser_resume_short_and_long():
    p = cli.build_arg_parser()
    assert p.parse_args(["--resume"]).resume is True
    assert p.parse_args(["-r"]).resume is True


def test_arg_parser_continue_and_resume_mutually_exclusive():
    p = cli.build_arg_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["-c", "-r"])


def test_help_exits_zero(capsys):
    p = cli.build_arg_parser()
    with pytest.raises(SystemExit) as exc:
        p.parse_args(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage" in out.lower()


# --------------------------------------------------------------------------
# --continue / --resume behavior against a stubbed HTTP client
# --------------------------------------------------------------------------

class FakeClient:
    """Stand-in for BridgeClient; records calls, returns canned data."""

    def __init__(self, sessions=None, resume_ok=True, goal_lines=None):
        self._sessions = sessions if sessions is not None else []
        self.resume_calls = []
        self.new_calls = []
        self.send_calls = []
        self.stop_calls = 0
        self._resume_ok = resume_ok
        self._goal_lines = goal_lines or []

    def is_up(self):
        return True

    def list_sessions(self):
        return list(self._sessions)

    def resume(self, sid):
        self.resume_calls.append(sid)
        return {"ok": self._resume_ok, "sid": sid}

    def new_session(self, title=""):
        self.new_calls.append(title)
        return {"ok": True}

    def goal(self, text):
        for line in self._goal_lines:
            yield line

    def send(self, sid, msg):
        self.send_calls.append((sid, msg))
        return {"ok": True}

    def stop(self):
        self.stop_calls += 1
        return {"ok": True}


def test_do_resume_picks_by_number_and_calls_client(capsys):
    sessions = [_mk_session("s1", title="alpha"), _mk_session("s2", title="beta")]
    client = FakeClient(sessions=sessions)
    cli._do_resume(client, "2", [])
    assert client.resume_calls == ["s2"]
    out = capsys.readouterr().out
    assert "beta" in out


def test_do_resume_unknown_token_no_call(capsys):
    sessions = [_mk_session("s1", title="alpha")]
    client = FakeClient(sessions=sessions)
    cli._do_resume(client, "99", [])
    assert client.resume_calls == []
    out = capsys.readouterr().out
    assert "no such session" in out.lower()


def test_continue_auto_resumes_most_recent(capsys):
    sessions = [_mk_session("s1", title="most recent", last_active_ts=time.time())]
    client = FakeClient(sessions=sessions)
    cli._print_banner_and_maybe_resume(client, auto_continue=True)
    assert client.resume_calls == ["s1"]


def test_no_continue_flag_just_prints_banner(capsys):
    sessions = [_mk_session("s1", title="most recent", last_active_ts=time.time())]
    client = FakeClient(sessions=sessions)
    cli._print_banner_and_maybe_resume(client, auto_continue=False)
    assert client.resume_calls == []
    out = capsys.readouterr().out
    assert "most recent" in out


def test_no_sessions_prints_fresh_banner(capsys):
    client = FakeClient(sessions=[])
    cli._print_banner_and_maybe_resume(client, auto_continue=True)
    out = capsys.readouterr().out
    assert "fresh" in out.lower()
    assert client.resume_calls == []


def test_do_new_reports_success(capsys):
    client = FakeClient()
    cli._do_new(client, "my title")
    assert client.new_calls == ["my title"]
    out = capsys.readouterr().out
    assert "new session started" in out.lower()


def test_do_sessions_renders_picker(capsys):
    sessions = [_mk_session("s1", title="alpha")]
    client = FakeClient(sessions=sessions)
    result = cli._do_sessions(client)
    assert result == sessions
    out = capsys.readouterr().out
    assert "alpha" in out


# --------------------------------------------------------------------------
# /goal work mode -- canned SSE per the frozen contract:
# {"turn_done": n, "text": ...}, {"steered": ...},
# {"goal_done": true, "outcome": ..., "turns": n}, then event: done
# --------------------------------------------------------------------------

GOAL_LINES = [
    'data: {"delta": "Investigating"}\n',
    '\n',
    'data: {"delta": " the failure."}\n',
    '\n',
    'data: {"turn_done": 1, "text": "Investigating the failure."}\n',
    '\n',
    'data: {"steered": "focus on the parser"}\n',
    '\n',
    'data: {"delta": "Fixed."}\n',
    '\n',
    'data: {"turn_done": 2, "text": "Fixed."}\n',
    '\n',
    'data: {"goal_done": true, "outcome": "done", "turns": 2}\n',
    '\n',
    'event: done\n',
    'data: {}\n',
    '\n',
]


def _run_goal(client, kbd_items=()):
    kbd_q = queue.Queue()
    for item in kbd_items:
        kbd_q.put(item)
    out = io.StringIO()
    cli.run_goal(client, "fix it", kbd_q, out=out, stop_grace=1.0)
    return out.getvalue()


def test_run_goal_renders_turns_steering_and_summary():
    client = FakeClient(sessions=[_mk_session("s1")], goal_lines=GOAL_LINES)
    out = _run_goal(client)
    assert "Investigating the failure." in out
    assert "--- turn 1 ---" in out
    assert "[steered: focus on the parser]" in out
    assert "Fixed." in out
    assert "--- turn 2 ---" in out
    assert "goal done (2 turns)" in out


def test_run_goal_keyboard_line_sends_steering():
    client = FakeClient(sessions=[_mk_session("s7")], goal_lines=GOAL_LINES)
    out = _run_goal(client, kbd_items=["also check the tests"])
    assert client.send_calls == [("s7", "also check the tests")]
    assert "[queued for next turn]" in out


def test_run_goal_blank_keyboard_lines_not_sent():
    client = FakeClient(sessions=[_mk_session("s7")], goal_lines=GOAL_LINES)
    _run_goal(client, kbd_items=["", "   "])
    assert client.send_calls == []


def test_run_goal_no_sessions_still_streams():
    client = FakeClient(sessions=[], goal_lines=GOAL_LINES)
    out = _run_goal(client)
    assert "goal done (2 turns)" in out


def test_goal_summary_outcomes():
    assert cli.goal_summary({"outcome": "done", "turns": 5}) == "goal done (5 turns)"
    assert cli.goal_summary({"outcome": "stopped", "turns": 3}) == "stopped after 3 turns"
    assert cli.goal_summary({"outcome": "max_turns", "turns": 8}) == "max turns reached (8 turns)"
    assert "error" in cli.goal_summary({"outcome": "error", "turns": 1})


def test_run_goal_stopped_outcome_summary():
    lines = [
        'data: {"delta": "partial"}\n', '\n',
        'data: {"goal_done": true, "outcome": "stopped", "turns": 3}\n', '\n',
        'event: done\n', 'data: {}\n', '\n',
    ]
    client = FakeClient(sessions=[_mk_session("s1")], goal_lines=lines)
    out = _run_goal(client)
    assert "stopped after 3 turns" in out


def test_help_text_mentions_goal():
    assert "/goal" in cli.HELP_TEXT

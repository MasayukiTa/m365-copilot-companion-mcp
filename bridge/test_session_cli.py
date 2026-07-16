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


def _mk_fleet(conv_url="", title="", last_active_ts=0, turns=None, status="active"):
    return {
        "sid": "", "source": "fleet", "conv_url": conv_url, "title": title,
        "last_active_ts": last_active_ts, "turns": turns, "status": status,
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
# unified (chat + fleet) picker rendering
# --------------------------------------------------------------------------

def test_is_adoptable_chat_session_always_true():
    assert cli.is_adoptable(_mk_session("s1")) is True


def test_is_adoptable_fleet_with_url_true():
    assert cli.is_adoptable(_mk_fleet(conv_url="https://example/conv/1")) is True


def test_is_adoptable_fleet_without_url_false():
    assert cli.is_adoptable(_mk_fleet(conv_url="")) is False


def test_render_picker_tags_source_and_flags_not_adoptable():
    now = 10000.0
    sessions = [
        _mk_session("s1", title="chat one", last_active_ts=now - 60, turns=3),
        _mk_fleet(conv_url="https://x/conv/9", title="fleet one", last_active_ts=now - 120, turns=None),
        _mk_fleet(conv_url="", title="fleet no url", last_active_ts=now - 200),
    ]
    out = cli.render_picker(sessions, now=now)
    lines = out.splitlines()
    assert len(lines) == 3
    assert "[chat]" not in lines[0]  # chat rows are untagged (source may be absent)
    assert "chat one" in lines[0]
    assert lines[1].strip().startswith("2.")
    assert "[fleet]" in lines[1]
    assert "fleet one" in lines[1]
    assert "? turns" in lines[1]   # turns may be null for fleet rows
    assert "not adoptable" not in lines[1]
    assert lines[2].strip().startswith("3.")
    assert "[fleet]" in lines[2]
    assert "fleet no url" in lines[2]
    assert "(not adoptable)" in lines[2]


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


def test_arg_parser_chat_only_default_false_and_flag():
    p = cli.build_arg_parser()
    assert p.parse_args([]).chat_only is False
    assert p.parse_args(["--chat-only"]).chat_only is True


def test_arg_parser_chat_only_combines_with_resume():
    p = cli.build_arg_parser()
    args = p.parse_args(["--resume", "--chat-only"])
    assert args.resume is True
    assert args.chat_only is True


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

    def __init__(self, sessions=None, resume_ok=True, goal_lines=None,
                 all_sessions=None, adopt_result=None):
        self._sessions = sessions if sessions is not None else []
        # unified (chat+fleet) listing for list_sessions_all(); defaults to
        # the plain chat sessions if the test doesn't care about fleet rows.
        self._all_sessions = all_sessions if all_sessions is not None else list(self._sessions)
        self.resume_calls = []
        self.new_calls = []
        self.send_calls = []
        self.adopt_calls = []
        self.goal_calls = []
        self.stop_calls = 0
        self._resume_ok = resume_ok
        self._goal_lines = goal_lines or []
        self._adopt_result = adopt_result if adopt_result is not None else {"ok": True, "sid": "adopted-sid", "ref_kind": "guid"}

    def is_up(self):
        return True

    def list_sessions(self):
        return list(self._sessions)

    def list_sessions_all(self):
        return list(self._all_sessions)

    def resume(self, sid):
        self.resume_calls.append(sid)
        return {"ok": self._resume_ok, "sid": sid}

    def adopt(self, url, title=""):
        self.adopt_calls.append((url, title))
        return dict(self._adopt_result)

    def new_session(self, title=""):
        self.new_calls.append(title)
        return {"ok": True}

    def goal(self, text, ac="", max_loops=None):
        self.goal_calls.append((text, ac, max_loops))
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
# unified picker: /sessions and /resume against list_sessions_all(), adopt
# flow for [fleet] rows, --chat-only fallback to plain list_sessions()
# --------------------------------------------------------------------------

def test_do_sessions_default_uses_unified_listing(capsys):
    chat = [_mk_session("s1", title="chat one")]
    unified = chat + [_mk_fleet(conv_url="https://x/1", title="fleet one")]
    client = FakeClient(sessions=chat, all_sessions=unified)
    result = cli._do_sessions(client)
    assert result == unified
    out = capsys.readouterr().out
    assert "fleet one" in out
    assert "[fleet]" in out


def test_do_sessions_chat_only_uses_plain_listing(capsys):
    chat = [_mk_session("s1", title="chat one")]
    unified = chat + [_mk_fleet(conv_url="https://x/1", title="fleet one")]
    client = FakeClient(sessions=chat, all_sessions=unified)
    result = cli._do_sessions(client, chat_only=True)
    assert result == chat
    out = capsys.readouterr().out
    assert "fleet one" not in out


def test_do_resume_fleet_row_calls_adopt_not_resume(capsys):
    sessions = [_mk_fleet(conv_url="https://x/conv/42", title="fleet convo")]
    client = FakeClient(sessions=sessions)
    cli._do_resume(client, "1", sessions)
    assert client.resume_calls == []
    assert client.adopt_calls == [("https://x/conv/42", "fleet convo")]
    out = capsys.readouterr().out
    assert "adopted fleet convo" in out.lower()


def test_do_resume_fleet_row_not_adoptable_reprompts_with_reason(capsys):
    sessions = [_mk_fleet(conv_url="", title="no url yet")]
    client = FakeClient(sessions=sessions)
    cli._do_resume(client, "1", sessions)
    assert client.resume_calls == []
    assert client.adopt_calls == []
    out = capsys.readouterr().out.lower()
    assert "not adoptable" in out
    assert "no url yet" in out


def test_do_resume_adopt_failure_reports_error(capsys):
    sessions = [_mk_fleet(conv_url="https://x/conv/1", title="fleet convo")]
    client = FakeClient(sessions=sessions, adopt_result={"ok": False, "error": "boom"})
    cli._do_resume(client, "1", sessions)
    out = capsys.readouterr().out.lower()
    assert "adopt failed" in out
    assert "boom" in out


def test_do_resume_chat_row_still_uses_resume(capsys):
    sessions = [_mk_session("s1", title="alpha")]
    client = FakeClient(sessions=sessions)
    cli._do_resume(client, "1", sessions)
    assert client.resume_calls == ["s1"]
    assert client.adopt_calls == []
    out = capsys.readouterr().out.lower()
    assert "resumed alpha" in out


def test_do_resume_empty_cache_populates_from_unified_listing():
    chat = []
    unified = [_mk_fleet(conv_url="https://x/1", title="fleet one")]
    client = FakeClient(sessions=chat, all_sessions=unified)
    cache = []
    cli._do_resume(client, "1", cache)
    assert cache == unified
    assert client.adopt_calls == [("https://x/1", "fleet one")]


def test_do_resume_empty_cache_chat_only_populates_from_plain_listing():
    chat = [_mk_session("s1", title="alpha")]
    unified = chat + [_mk_fleet(conv_url="https://x/1", title="fleet one")]
    client = FakeClient(sessions=chat, all_sessions=unified)
    cache = []
    cli._do_resume(client, "1", cache, chat_only=True)
    assert cache == chat
    assert client.resume_calls == ["s1"]


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


def test_skill_admin_create_and_approve_flow_is_local(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(tmp_path / "skills.sqlite3"))
    project = tmp_path / "project"
    assert cli._skill_admin(
        "/skill-create my-local | Local workflow | Do the local thing.", str(project)
    )
    out = capsys.readouterr().out
    assert "created and trusted local Skill /my-local" in out
    assert (project / ".claude" / "skills" / "my-local" / "SKILL.md").is_file()


def test_unknown_slash_command_is_forwarded_for_dynamic_skills(capsys):
    class Reader:
        q = queue.Queue()
        lines = iter(["/my-dynamic target.py", "/quit"])

        def readline(self, _prompt=""):
            return next(self.lines)

    class Client(FakeClient):
        def stream(self, msg):
            assert msg == "/my-dynamic target.py"
            yield 'data: {"delta": "dynamic skill ran"}\n'
            yield 'event: done\n'
            yield 'data: {}\n'
            yield '\n'

    client = Client(sessions=[])
    cli.repl(client, reader=Reader())
    assert "dynamic skill ran" in capsys.readouterr().out


# --------------------------------------------------------------------------
# VERIFICATION phase -- {"verify_start": n} / {"verdict": {...}} payloads,
# and the three new goal_done outcomes: done_verified / verify_failed /
# escalate_oscillation.
# --------------------------------------------------------------------------

VERIFIED_FLOW_LINES = [
    'data: {"delta": "Implementing the fix."}\n', '\n',
    'data: {"turn_done": 1, "text": "Implementing the fix."}\n', '\n',
    'data: {"verify_start": 1}\n', '\n',
    'data: {"verdict": {"pass": true, "failed_ac": [], "reasons": [], "loop": 1}}\n', '\n',
    'data: {"goal_done": true, "outcome": "done_verified", "turns": 1}\n', '\n',
    'event: done\n', 'data: {}\n', '\n',
]


def test_run_goal_renders_full_verified_flow():
    client = FakeClient(sessions=[_mk_session("s1")], goal_lines=VERIFIED_FLOW_LINES)
    out = _run_goal(client)
    assert "Implementing the fix." in out
    assert "--- turn 1 ---" in out
    assert "--- verify (loop 1) ---" in out
    assert "verdict: PASS" in out
    assert "goal done + verified (1 turns)" in out


FAIL_THEN_PASS_LINES = [
    'data: {"delta": "First attempt."}\n', '\n',
    'data: {"turn_done": 1, "text": "First attempt."}\n', '\n',
    'data: {"verify_start": 1}\n', '\n',
    'data: {"verdict": {"pass": false, "failed_ac": ["AC-1", "AC-2"], "reasons": ["missing edge case", "off by one"], "loop": 1}}\n', '\n',
    'data: {"delta": "Second attempt."}\n', '\n',
    'data: {"turn_done": 2, "text": "Second attempt."}\n', '\n',
    'data: {"verify_start": 2}\n', '\n',
    'data: {"verdict": {"pass": true, "failed_ac": [], "reasons": [], "loop": 2}}\n', '\n',
    'data: {"goal_done": true, "outcome": "done_verified", "turns": 2}\n', '\n',
    'event: done\n', 'data: {}\n', '\n',
]


def test_run_goal_renders_fail_then_continuation_then_pass():
    client = FakeClient(sessions=[_mk_session("s1")], goal_lines=FAIL_THEN_PASS_LINES)
    out = _run_goal(client)
    assert "--- verify (loop 1) ---" in out
    assert "verdict: FAIL [AC-1, AC-2] missing edge case; off by one" in out
    assert "Second attempt." in out
    assert "--- turn 2 ---" in out
    assert "--- verify (loop 2) ---" in out
    assert "verdict: PASS" in out
    assert "goal done + verified (2 turns)" in out
    # PASS should come after the FAIL in the rendered order
    assert out.index("verdict: FAIL") < out.index("verdict: PASS")


def test_render_verdict_truncates_long_reasons():
    verdict = {
        "pass": False,
        "failed_ac": ["AC-1"],
        "reasons": ["x" * 200],
        "loop": 1,
    }
    rendered = cli.render_verdict(verdict)
    assert rendered.startswith("verdict: FAIL [AC-1] ")
    # reason portion truncated to ~120 chars
    reason_part = rendered.split("] ", 1)[1]
    assert len(reason_part) <= 120


def test_render_verdict_pass_ignores_failed_ac_and_reasons():
    assert cli.render_verdict({"pass": True}) == "verdict: PASS"


def test_goal_summary_new_verification_outcomes():
    assert cli.goal_summary({"outcome": "done_verified", "turns": 4}) == "goal done + verified (4 turns)"
    assert cli.goal_summary({"outcome": "verify_failed", "turns": 6}) == (
        "verification FAILED after max loops (6 turns)"
    )
    assert cli.goal_summary({"outcome": "escalate_oscillation", "turns": 9}) == (
        "escalated: same ACs failing repeatedly - human review needed (9 turns)"
    )


def test_run_goal_verify_failed_outcome_summary():
    lines = [
        'data: {"delta": "partial"}\n', '\n',
        'data: {"verify_start": 3}\n', '\n',
        'data: {"verdict": {"pass": false, "failed_ac": ["AC-1"], "reasons": ["still broken"], "loop": 3}}\n', '\n',
        'data: {"goal_done": true, "outcome": "verify_failed", "turns": 6}\n', '\n',
        'event: done\n', 'data: {}\n', '\n',
    ]
    client = FakeClient(sessions=[_mk_session("s1")], goal_lines=lines)
    out = _run_goal(client)
    assert "verification FAILED after max loops (6 turns)" in out


def test_run_goal_escalate_oscillation_outcome_summary():
    lines = [
        'data: {"verdict": {"pass": false, "failed_ac": ["AC-1"], "reasons": ["same failure"], "loop": 2}}\n', '\n',
        'data: {"goal_done": true, "outcome": "escalate_oscillation", "turns": 9}\n', '\n',
        'event: done\n', 'data: {}\n', '\n',
    ]
    client = FakeClient(sessions=[_mk_session("s1")], goal_lines=lines)
    out = _run_goal(client)
    assert "escalated: same ACs failing repeatedly - human review needed (9 turns)" in out


def test_run_goal_passes_ac_and_max_loops_to_client():
    client = FakeClient(sessions=[_mk_session("s1")], goal_lines=GOAL_LINES)
    kbd_q = queue.Queue()
    out = io.StringIO()
    cli.run_goal(client, "fix it", kbd_q, out=out, stop_grace=1.0, ac="tests pass", max_loops=5)
    assert client.goal_calls == [("fix it", "tests pass", 5)]


def test_run_goal_without_ac_passes_empty_defaults():
    client = FakeClient(sessions=[_mk_session("s1")], goal_lines=GOAL_LINES)
    _run_goal(client)
    assert client.goal_calls == [("fix it", "", None)]


def test_run_goal_verify_events_do_not_break_parser():
    # verify_start / verdict payloads interleaved with plain deltas must not
    # raise or corrupt the running text accumulator.
    lines = [
        'data: {"delta": "before"}\n', '\n',
        'data: {"verify_start": 1}\n', '\n',
        'data: {"verdict": {"pass": true, "failed_ac": [], "reasons": [], "loop": 1}}\n', '\n',
        'data: {"delta": "after"}\n', '\n',
        'data: {"goal_done": true, "outcome": "done_verified", "turns": 1}\n', '\n',
        'event: done\n', 'data: {}\n', '\n',
    ]
    client = FakeClient(sessions=[_mk_session("s1")], goal_lines=lines)
    out = _run_goal(client)
    assert "before" in out
    assert "after" in out
    assert "goal done + verified (1 turns)" in out


# --------------------------------------------------------------------------
# /goal <text> :: <acceptance criteria> arg splitting
# --------------------------------------------------------------------------

def test_split_goal_arg_no_separator_returns_empty_ac():
    text, ac = cli.split_goal_arg("fix the parser bug")
    assert text == "fix the parser bug"
    assert ac == ""


def test_split_goal_arg_splits_on_double_colon():
    text, ac = cli.split_goal_arg("fix the parser bug :: pytest -q is green")
    assert text == "fix the parser bug"
    assert ac == "pytest -q is green"


def test_split_goal_arg_trailing_separator_empty_ac():
    text, ac = cli.split_goal_arg("fix the parser bug :: ")
    assert text == "fix the parser bug"
    assert ac == ""


def test_split_goal_arg_only_separator_both_empty():
    text, ac = cli.split_goal_arg(" :: ")
    assert text == ""
    assert ac == ""


def test_split_goal_arg_further_double_colons_kept_in_ac():
    text, ac = cli.split_goal_arg("do the thing :: AC-1 done :: AC-2 done")
    assert text == "do the thing"
    assert ac == "AC-1 done :: AC-2 done"


def test_split_goal_arg_strips_whitespace_both_sides():
    text, ac = cli.split_goal_arg("  fix it   ::   tests pass  ")
    assert text == "fix it"
    assert ac == "tests pass"

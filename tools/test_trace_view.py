# -*- coding: utf-8 -*-
"""tools/trace_view: per-session timelines from synthetic ledgers (no real log is read).

Covers: gap detection around the threshold, a lock refusal followed by fleet_submit, sessions
that connected and never called, redaction in every output format, tolerance of damaged and
missing files, time-window filtering (including the bisect path on a large file), HTML and
Excel round trips, the CLI, and the unlock gate on the MCP tool.
"""
import hashlib
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.trace_view import build as B          # noqa: E402
from tools.trace_view import cli                  # noqa: E402
from tools.trace_view import load as LD           # noqa: E402
from tools.trace_view import redact as R          # noqa: E402
from tools.trace_view import report as RP         # noqa: E402
from tools.trace_view import timeutil as T        # noqa: E402
from tools.trace_view.render_html import render_html          # noqa: E402
from tools.trace_view.render_md import render_markdown        # noqa: E402

BASE = 1_800_000_000.0
TZ = T.parse_tz("JST")


def _spec(text):
    text = str(text)
    return {"text": text[:2000], "len": len(text),
            "sha16": hashlib.sha256(text.encode()).hexdigest()[:16], "truncated": len(text) > 2000}


def _call(cid, t, tool, session="sess-a", args=None):
    return {"schema": 1, "event": "call", "id": cid, "ts": BASE + t, "tool": tool, "task": "",
            "worker": "", "turn": None, "session": session,
            "args": {k: (v if isinstance(v, dict) else _spec(v)) for k, v in (args or {}).items()}}


def _out(cid, t, dur=0.1, ok=True, text="fine", error=""):
    return {"schema": 1, "event": "outcome", "id": cid, "ts": BASE + t + dur, "ok": ok,
            "duration_s": dur, "error": error, "result": _spec(text), "ts_start": BASE + t,
            "ts_end": BASE + t + dur}


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")


def _state(tmp_path, events=(), inbound=(), lock=(), submit=()):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    if events:
        _write(d / "tool_events.jsonl", events)
    if inbound:
        _write(d / "mcp_inbound.jsonl", inbound)
    if lock:
        _write(d / "lock_refusals.jsonl", lock)
    if submit:
        _write(d / "fleet_submit_refusals.jsonl", submit)
    return str(d)


def _report(state, t0=-10, t1=100000, session=None, gap_s=120.0):
    return RP.make_report(state, BASE + t0, BASE + t1, session, gap_s)


def _pair(cid, t, tool, session="sess-a", dur=0.1, text="fine", **kw):
    return [_call(cid, t, tool, session, kw.pop("args", None)), _out(cid, t, dur, text=text, **kw)]


# -- (a) gaps ---------------------------------------------------------------------------------

def test_a_gap_is_found_only_at_or_over_the_threshold(tmp_path):
    ev = []
    ev += _pair("c1", 0, "read_file", dur=1.0)          # ends at 1
    ev += _pair("c2", 100, "read_file", dur=1.0)        # idle 99 s: below 120
    ev += _pair("c3", 221, "read_file", dur=1.0)        # idle 120 s exactly: a gap
    ev += _pair("c4", 600, "glob", dur=1.0)             # idle 378 s: a gap
    rep = _report(_state(tmp_path, ev))
    gaps = rep["sessions"][0]["gaps"]
    assert [round(g["seconds"]) for g in gaps] == [120, 378]
    assert gaps[1]["after_tool"] == "read_file" and gaps[1]["before_tool"] == "glob"
    # a lower threshold finds the 99 s one as well; a higher one finds only the longest
    assert len(_report(_state(tmp_path, ev), gap_s=60)["sessions"][0]["gaps"]) == 3
    assert len(_report(_state(tmp_path, ev), gap_s=200)["sessions"][0]["gaps"]) == 1


def test_idle_time_is_measured_from_the_end_of_a_long_call(tmp_path):
    ev = _pair("c1", 0, "run_python", dur=300.0) + _pair("c2", 310, "read_file")
    assert _report(_state(tmp_path, ev))["sessions"][0]["gaps"] == []


def test_the_gap_label_agrees_with_the_clock_times_beside_it(tmp_path):
    # 17 min 9.4 s of idle time between clock readings 18:38:08 and 18:55:18 is shown as 17m10s
    ev = _pair("c1", 0.9, "skill_match", dur=0.08) + _pair("c2", 1030.3, "unlock")
    gap = _report(_state(tmp_path, ev))["sessions"][0]["gaps"][0]
    assert gap["shown_s"] == int(BASE + 1030.3) - int(BASE + 0.98)
    md = render_markdown(_report(_state(tmp_path, ev)), TZ, "ja")
    assert "17分10秒" in md


def test_parallel_and_serial_are_told_apart(tmp_path):
    serial = _pair("c1", 0, "a") + _pair("c2", 5, "b")
    par = [_call("p1", 0, "a", "sess-p"), _call("p2", 0.5, "b", "sess-p"),
           _out("p1", 0, 2.0), _out("p2", 0.5, 2.0)]
    rep = _report(_state(tmp_path, serial + par))
    modes = {s["id"]: s["concurrency"] for s in rep["sessions"]}
    assert modes["sess-a"]["mode"] == "serial"
    assert modes["sess-p"]["mode"] == "parallel" and modes["sess-p"]["max_overlap"] == 2


# -- (b) refusals and fleet_submit ------------------------------------------------------------

LOCKED_TEXT = "[locked: no valid unlock token] Session not unlocked. Call unlock again."
QUEUED_TEXT = "queued abc123def456 -- the goal is in this machine's queue. 1 job(s) waiting."


def test_a_lock_refusal_then_a_queued_fleet_submit(tmp_path):
    ev = []
    ev += [_call("c1", 0, "run_python", args={"code": "print(1)"}),
           _out("c1", 0, 0.1, ok=False, text=LOCKED_TEXT, error="refused (locked)")]
    ev += [_call("c2", 20, "fleet_submit", args={"goal": "do the thing", "source": "tester"}),
           _out("c2", 20, 0.2, text=QUEUED_TEXT)]
    lock = [{"ts": BASE + 0.1, "event": "refused", "session": "sess-a", "site": "x:1:require_unlocked",
             "detail": LOCKED_TEXT}]
    sess = _report(_state(tmp_path, ev, lock=lock))["sessions"][0]
    assert [(s["status"], s["reason"]) for s in sess["steps"]] == [("refused", "locked"), ("ok", "")]
    assert sess["steps"][0]["lock_logged"] is True and sess["events"] == []
    assert sess["submits"][0]["result"] == "queued" and sess["submits"][0]["job"] == "abc123def456"
    assert not [f for f in sess["findings"] if f["kind"].startswith("locked")]


def test_a_lock_refusal_with_no_fleet_submit_is_a_finding(tmp_path):
    ev = [_call("c1", 0, "run_python"), _out("c1", 0, 0.1, ok=False, text=LOCKED_TEXT, error="refused (locked)")]
    ev += _pair("c2", 30, "read_file")
    sess = _report(_state(tmp_path, ev))["sessions"][0]
    assert any(f["kind"] == "locked_without_submit" for f in sess["findings"])
    assert sess["stats"]["reasons"] == {"locked": 1}


def test_refusal_reasons_are_classified(tmp_path):
    cases = [
        ("read_file", "[read_file error: PermissionError: Refusing to touch the server's own authorisation state (.env). x]", "env_protected"),
        ("read_file", "[read_file error: PermissionError: Refusing to touch the tool-call trace (x). y]", "protected_path"),
        ("call_tool.unknown", "[call_tool: no tool or category named 'zzz'. Closest tools: a]", "unknown_tool"),
        ("fleet_submit", "[call_tool fleet_submit: missing required 'goal'. The accepted form is x]", "missing_args"),
        ("list_directory", "[call_tool list_directory: does not accept 'name'. x]", "unexpected_args"),
        ("fleet_submit", "[fleet_submit: refused -- this reads as the goal already queued or running (abc)]", "duplicate"),
        ("fleet_submit", "[fleet_submit: refused -- 50 jobs are already waiting and nothing is draining]", "limit_exceeded"),
        ("fleet_submit", "[fleet_submit: refused -- the goal is 9000 characters, over the 8000 limit.]", "limit_exceeded"),
    ]
    ev = []
    for i, (tool, text, _reason) in enumerate(cases):
        ev += [_call("k%d" % i, i * 5, tool), _out("k%d" % i, i * 5, 0.01, text=text)]
    steps = _report(_state(tmp_path, ev))["sessions"][0]["steps"]
    assert [s["reason"] for s in steps] == [c[2] for c in cases]
    assert all(s["status"] == "refused" for s in steps)
    sub = [s["submit"] for s in steps if s["submit"]]
    assert [x["result"] for x in sub] == ["refused"] * len(sub) and sub[0]["reason"] == "missing_args"


def test_a_refused_submit_takes_its_reason_from_the_refusal_log_when_the_text_is_unfamiliar(tmp_path):
    ev = [_call("c1", 0, "fleet_submit"), _out("c1", 0, 0.1, text="[fleet_submit: refused -- something new]")]
    log = [{"ts": BASE + 0.2, "reason": "similar", "goal_len": 12, "goal_head": "x"}]
    st = _report(_state(tmp_path, ev, submit=log))["sessions"][0]["steps"][0]
    assert st["submit"]["logged"] == "similar"
    assert st["reason"] == "duplicate" or st["status"] == "error"


def test_a_call_with_no_outcome_is_visible(tmp_path):
    ev = [_call("c1", 0, "run_python")]
    sess = _report(_state(tmp_path, ev))["sessions"][0]
    assert sess["steps"][0]["status"] == "no_outcome"
    assert any(f["kind"] == "call_without_outcome" for f in sess["findings"])


# -- (c) connect without a call ---------------------------------------------------------------

def test_a_session_that_connected_and_never_called_is_found(tmp_path):
    ip, ua = "192.0.2.10", "TestClient/1.0"
    inbound = [
        {"ts": BASE + 1, "at": "x", "method": "initialize", "session": "", "client_ip": ip, "ua": ua},
        {"ts": BASE + 1.2, "at": "x", "method": "notifications/initialized", "session": "idle-1", "client_ip": ip, "ua": ua},
        {"ts": BASE + 1.4, "at": "x", "method": "tools/list", "session": "idle-1", "client_ip": ip, "ua": ua},
        {"ts": BASE + 5, "at": "x", "method": "initialize", "session": "", "client_ip": ip, "ua": ua},
        {"ts": BASE + 5.2, "at": "x", "method": "tools/list", "session": "busy-1", "client_ip": ip, "ua": ua},
        {"ts": BASE + 6, "at": "x", "method": "tools/call", "session": "busy-1", "client_ip": ip, "ua": ua, "tool": "call_tool"},
    ]
    rep = _report(_state(tmp_path, _pair("c1", 6, "read_file", "busy-1"), inbound=inbound))
    by = {s["id"]: s for s in rep["sessions"]}
    assert by["idle-1"]["connect_only"] is True
    assert "initialize" in by["idle-1"]["connect"]["methods"]          # attributed to the session
    assert by["busy-1"]["connect_only"] is False
    kinds = [(f["session"], f["kind"]) for f in rep["findings"]]
    assert ("idle-1", "connect_without_call") in kinds and ("busy-1", "connect_without_call") not in kinds


# -- (d) redaction in every output ------------------------------------------------------------

PLANTED = ["PlantedPassword!Value91", "FAKEtoken0123456789abcdefABCDEF", "PlantedEnvBody123",
           "PlantedApiKeyValue7788", "PlantedBearerValue4455667788"]


def _planted_state(tmp_path):
    ev = []
    ev += _pair("c1", 0, "unlock", args={"password": PLANTED[0]}, text="Unlocked. token " + PLANTED[1])
    ev += _pair("c2", 5, "write_file", args={"path": "out.txt", "unlock_token": PLANTED[1],
                                              "content": "password=" + PLANTED[0] + " more"})
    ev += _pair("c3", 10, "shell_exec", args={"command": "type notes.txt"},
                text="MCP_API_KEY=%s\nMCP_UNLOCK_PASSWORD=%s\nAuthorization: Bearer %s" % (
                    PLANTED[3], PLANTED[0], PLANTED[4]))
    ev += _pair("c4", 15, "read_file", args={"path": "C:/repo/.env"}, text="X=1\nSECRET_THING=" + PLANTED[2])
    ev += _pair("c5", 20, "run_python", args={"code": "password = '%s'" % PLANTED[0]},
                ok=False, error="failed: token=%s" % PLANTED[1], text="boom api_key: " + PLANTED[3])
    ev += _pair("c6", 25, "call_tool", args={"name": "unlock", "arguments": {"password": PLANTED[0],
                                                                             "unlock_token": PLANTED[1]}})
    return _state(tmp_path, ev)


def _xlsx_text(path):
    from openpyxl import load_workbook
    wb = load_workbook(path)
    parts = []
    for ws in wb.worksheets:
        parts.append(ws.title)
        for row in ws.iter_rows(values_only=True):
            parts.extend(str(c) for c in row if c is not None)
    return "\n".join(parts)


def test_no_planted_secret_appears_in_any_output(tmp_path):
    rep = _report(_planted_state(tmp_path))
    out = tmp_path / "out"
    paths = RP.write_outputs(rep, str(out), ["md", "html", "xlsx"], TZ, "ja")
    texts = {}
    for p in paths:
        if p.endswith(".xlsx"):
            texts[p] = _xlsx_text(p)
        else:
            texts[p] = open(p, encoding="utf-8").read()
    texts["json"] = json.dumps(rep, ensure_ascii=False, default=str)
    texts["en"] = render_markdown(rep, TZ, "en")
    for name, text in texts.items():
        for secret in PLANTED:
            assert secret not in text, "%s leaked in %s" % (secret, name)
    assert "[redacted]" in texts["json"]


def test_argument_summaries_show_a_sliver_a_length_and_a_hash():
    spec = {"text": "import subprocess, json", "len": 910, "sha16": "ab12cd34ef56ab12", "truncated": True}
    assert R.summarize_value("code", spec) == "code=import s\u2026 (len 910, #ab12cd34)"
    assert R.summarize_value("password", spec) == "password=[redacted]"
    assert R.summarize_value("max_entries", _spec("200")) == "max_entries=200"
    short = R.summarize_value("note", _spec("hunter2"))          # a short value is never shown whole
    assert "hunter2" not in short and "len 7" in short
    assert R.summarize_args({"arguments": {"unlock_token": _spec("x" * 30)}}) == ["arguments.unlock_token=[redacted]"]


def test_scrub_leaves_ordinary_text_alone():
    s = "list_directory of C:/work/project: 12 files; skill_match passed 3 tests"
    assert R.scrub(s) == s


# -- (e) damaged and missing files ------------------------------------------------------------

def test_damaged_lines_and_missing_files_are_tolerated(tmp_path):
    good = _pair("c1", 0, "read_file")
    ev = ["not json at all", "{\"ts\": 1800000001, \"event\": ", "[1, 2, 3]", json.dumps(good[0]), "", "{}",
          json.dumps({"event": "call", "id": 7, "ts": BASE + 2}), json.dumps(good[1]),
          json.dumps({"event": "outcome", "id": "ghost", "ts": BASE + 3, "ok": True})]
    state = _state(tmp_path, ev, inbound=["garbage", "{\"ts\": \"yesterday\"}"])
    loaded = LD.load(state, BASE - 10, BASE + 100)
    assert loaded["health"]["tool_events.jsonl"]["bad"] >= 3
    assert loaded["health"]["fleet_submit_refusals.jsonl"]["present"] is False
    rep = B.build(loaded, BASE - 10, BASE + 100)
    assert rep["meta"]["n_steps"] == 1 and rep["meta"]["orphan_outcomes"] == 1
    render_markdown(rep, TZ, "ja")


def test_an_empty_or_nonexistent_state_directory_gives_an_empty_report(tmp_path):
    for state in (str(tmp_path / "nope"), _state(tmp_path)):
        rep = RP.make_report(state, BASE, BASE + 10)
        assert rep["sessions"] == []
        assert "セッションはありません" in render_markdown(rep, TZ, "ja")
        assert "<html" in render_html(rep, TZ, "ja")


# -- (f) time window --------------------------------------------------------------------------

def test_only_rows_inside_the_window_are_read(tmp_path):
    ev = []
    for i in range(10):
        ev += _pair("w%d" % i, i * 100, "read_file", dur=1.0)
    state = _state(tmp_path, ev)
    rep = _report(state, t0=250, t1=650)
    assert [s["id"] for s in rep["sessions"][0]["steps"]] == ["w3", "w4", "w5", "w6"]
    # an outcome a little after the window's end still joins its call
    assert all(s["status"] == "ok" for s in rep["sessions"][0]["steps"])
    assert RP.make_report(state, BASE + 5000, BASE + 6000)["sessions"] == []
    assert len(_report(state, t0=300, t1=300)["sessions"][0]["steps"]) == 1       # inclusive bounds


def test_a_large_file_is_entered_by_bisection_and_gives_the_same_answer(tmp_path):
    pad = "x" * 400
    rows = []
    for i in range(3000):
        rows.append(_call("L%04d" % i, i * 2.0, "read_file", args={"path": "f%d" % i}))
        o = _out("L%04d" % i, i * 2.0, 0.05, text=pad)
        rows.append(o)
    state = _state(tmp_path, rows)
    assert os.path.getsize(os.path.join(state, "tool_events.jsonl")) > 1_000_000
    loaded = LD.load(state, BASE + 3000, BASE + 3100)
    ids = sorted(loaded["calls"])
    assert ids[0] == "L1500" and ids[-1] == "L1550" and len(ids) == 51
    assert all(k in loaded["outcomes"] for k in ids)


def test_a_rotated_generation_is_read_too(tmp_path):
    state = _state(tmp_path, _pair("new", 100, "glob"))
    _write(os.path.join(state, "tool_events.jsonl.1"), _pair("old", 0, "read_file"))
    assert sorted(LD.load(state, BASE - 1, BASE + 200)["calls"]) == ["new", "old"]


def test_session_filter_is_a_prefix_match(tmp_path):
    ev = _pair("a1", 0, "glob", "abc123") + _pair("b1", 1, "glob", "abd999")
    rep = _report(_state(tmp_path, ev), session="abc")
    assert [s["id"] for s in rep["sessions"]] == ["abc123"] and rep["meta"]["n_steps"] == 1


def test_time_parsing_and_zones():
    assert T.parse_time("2026-10-09 18:00", T.parse_tz("JST")) == T.parse_time("2026-10-09 09:00", T.parse_tz("UTC"))
    assert T.parse_time("2026-10-09T18:00:30", T.parse_tz("+09:00")) - T.parse_time("2026-10-09 18:00", T.parse_tz("JST")) == 30
    assert T.parse_time(1800000000, T.parse_tz("JST")) == 1800000000.0
    assert T.fmt_duration(1030, "ja") == "17分10秒" and T.fmt_duration(1030, "en") == "17m10s"
    with pytest.raises(ValueError):
        T.parse_time("last tuesday", T.parse_tz("JST"))
    with pytest.raises(ValueError):
        T.parse_tz("Not/AZone")


# -- (g) HTML / Excel / CLI -------------------------------------------------------------------

def _rich_state(tmp_path):
    ev = []
    ev += _pair("c1", 0, "read_file", args={"path": "a.txt"})
    ev += [_call("c2", 5, "run_python"), _out("c2", 5, 0.1, ok=False, text=LOCKED_TEXT, error="refused (locked)")]
    ev += _pair("c3", 600, "fleet_submit", text=QUEUED_TEXT)
    ev += [_call("c4", 610, "glob"), _out("c4", 610, 1.0, ok=False, text="[glob error: boom]", error="returned its own error report")]
    return _state(tmp_path, ev)


def test_html_is_self_contained_and_marks_the_gap(tmp_path):
    rep = _report(_rich_state(tmp_path))
    page = render_html(rep, TZ, "ja")
    assert page.startswith("<!doctype html>") and "gapbox" in page and 'class="bar refused"' in page
    low = page.lower()
    assert "<script" not in low and "<link" not in low and "src=" not in low and "http://" not in low and "https://" not in low
    assert "セッション一覧" in page and "空白" in page
    assert render_html(rep, TZ, "en").count("<html lang=\"en\"") == 1


def test_html_escapes_what_it_prints(tmp_path):
    ev = _pair("c1", 0, "<script>alert(1)</script>", args={"path": "<img src=x onerror=1>"})
    page = render_html(_report(_state(tmp_path, ev)), TZ, "ja")
    assert "<script>alert" not in page and "<img src=x" not in page


def test_excel_round_trip_with_conditional_formatting(tmp_path):
    from openpyxl import load_workbook
    rep = _report(_rich_state(tmp_path))
    path = tmp_path / "t.xlsx"
    from tools.trace_view.render_xlsx import render_xlsx
    render_xlsx(rep, TZ, str(path), "ja")
    wb = load_workbook(str(path))
    assert wb.sheetnames == ["セッション一覧", "ステップ", "空白", "検出事項", "ツール別"]
    steps = wb["ステップ"]
    rows = list(steps.iter_rows(values_only=True))
    assert [r[4] for r in rows[1:]] == ["read_file", "run_python", "fleet_submit", "glob"]
    assert [r[6] for r in rows[1:]] == ["ok", "refused", "ok", "error"]
    assert rows[2][7] == "拒否 / ロック拒否" and rows[3][11] == "queued"
    assert len(steps.conditional_formatting) >= 1 and steps.freeze_panes == "A2"
    gaps = list(wb["空白"].iter_rows(values_only=True))
    assert len(gaps) == 2 and gaps[1][5] == "run_python"


def test_excel_never_turns_text_into_a_formula(tmp_path):
    from openpyxl import load_workbook
    from tools.trace_view.render_xlsx import render_xlsx
    ev = _pair("c1", 0, "=cmd|' /c calc'!A1", args={"path": "=1+1"})
    path = tmp_path / "f.xlsx"
    render_xlsx(_report(_state(tmp_path, ev)), TZ, str(path), "en")
    for ws in load_workbook(str(path)).worksheets:
        for row in ws.iter_rows():
            for c in row:
                assert c.data_type != "f", (ws.title, c.coordinate, c.value)


def test_markdown_shows_the_gap_the_refusal_and_the_missing_submit(tmp_path):
    md = render_markdown(_report(_rich_state(tmp_path)), TZ, "ja")
    assert "**空白 9分55秒**" in md
    assert "拒否 / ロック拒否" in md and "fleet_submit #" in md
    en = render_markdown(_report(_rich_state(tmp_path)), TZ, "en")
    assert "GAP" in en and "locked" in en


def test_cli_list_and_report(tmp_path, capsys):
    state = _rich_state(tmp_path)
    t0 = T.fmt_dt(BASE - 10, TZ)
    t1 = T.fmt_dt(BASE + 100000, TZ)
    assert cli.main(["list", "--since", t0, "--until", t1, "--state-dir", state]) == 0
    assert "sess-a" in capsys.readouterr().out
    out = tmp_path / "o"
    assert cli.main(["--since", t0, "--until", t1, "--state-dir", state, "--out", str(out),
                     "--format", "md,html,xlsx", "--lang", "en"]) == 0
    assert sorted(p.suffix for p in out.iterdir()) == [".html", ".md", ".xlsx"]
    assert cli.main(["--since", t0, "--until", t1, "--state-dir", state, "--format", "html"]) == 2
    assert cli.main(["--since", "garbage"]) == 2
    assert cli.main(["--since", t1, "--until", t0]) == 2
    assert cli.main(["--since", t0, "--until", t1, "--state-dir", state, "--out", str(out), "--format", "pdf"]) == 2


# -- (h) the MCP tool: gate and registration --------------------------------------------------

def test_the_tool_refuses_when_not_unlocked(tmp_path, monkeypatch):
    from tools.trace_view import tool
    monkeypatch.setenv("TRACE_VIEW_STATE_DIR", _rich_state(tmp_path))
    out = tool.trace_session("2020-01-01 00:00", "2030-01-01 00:00")
    assert out.startswith("[locked")
    assert "sess-a" not in out


def test_the_tool_returns_a_summary_when_unlocked(tmp_path, monkeypatch):
    from tools.trace_view import tool
    monkeypatch.setenv("TRACE_VIEW_STATE_DIR", _rich_state(tmp_path))
    monkeypatch.setattr(tool, "require_unlocked", lambda: None)
    start = T.fmt_dt(BASE - 10, TZ)
    end = T.fmt_dt(BASE + 5000, TZ)
    out = tool.trace_session(start, end)
    assert "sess-a" in out and "ロック拒否" in out
    assert "sess-a" not in tool.trace_session(start, end, session="zzz")
    assert tool.trace_session("bad", end).startswith("[trace_session error")
    assert tool.trace_session(end, start).startswith("[trace_session error")
    assert tool.trace_session("2020-01-01", "2021-01-01").startswith("[trace_session error")


def test_the_tool_is_registered_and_in_the_catalogue():
    os.environ.setdefault("MCP_API_KEY", "test-only")
    os.environ.setdefault("MCP_TOOL_MAP", "1")
    try:
        import importlib
        allt = importlib.import_module("main")._ALL_TOOLS
    except Exception as exc:                       # pragma: no cover - env-dependent
        pytest.skip("main.py not importable here: %s" % exc)
    assert "trace_session" in allt
    from tools import tool_catalogue as C
    got = C.render(allt)
    assert ("\n  trace_session(" in got) or ("\n  trace_session -- " in got)
    from relay import fleet_toolset
    assert not fleet_toolset.is_allowed("trace_session")
    assert "trace_session" in fleet_toolset.DELIBERATELY_EXCLUDED


def test_the_package_reads_but_never_writes_the_state_directory(tmp_path):
    state = _rich_state(tmp_path)
    before = {n: (os.path.getsize(os.path.join(state, n)), os.path.getmtime(os.path.join(state, n)))
              for n in os.listdir(state)}
    rep = _report(state)
    RP.write_outputs(rep, str(tmp_path / "elsewhere"), ["md", "html", "xlsx"], TZ)
    after = {n: (os.path.getsize(os.path.join(state, n)), os.path.getmtime(os.path.join(state, n)))
             for n in os.listdir(state)}
    assert before == after

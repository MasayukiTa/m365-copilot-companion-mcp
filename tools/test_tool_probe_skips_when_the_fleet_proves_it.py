# -*- coding: utf-8 -*-
"""The bridge's tool-call probe is the FALLBACK, not the schedule.

Copilot Studio counts sessions and every probe message opens one. Measured 2026-09-28..10-05 the
probe sent 118-158 messages a day at a fixed 10-minute cadence, a third to a half of them within 30
minutes of a REAL tool call that had already proved what the probe exists to prove. These tests pin
the replacement behaviour:

  * a real, successful, non-probe call within the interval -> NO message, and the health check is
    recorded as passed BY THAT CALL at ITS timestamp (never green without an event);
  * nothing proved the path for a whole interval -> the probe is sent, as before;
  * the setting 0 (or MCP_TOOL_PROBE_SEC<=0) -> no message and no green;
  * a failed probe backs off (doubling, capped) and leaves a mechanism row;
  * the decision state is exported ("probe" in the bridge /status, .fleet/tool_probe_state.json);
  * the cockpit has a control for it in the settings popup (never the header).

GOLDEN: with nothing configured, the FIRST probe after startup is exactly as before (the 30 s
startup delay, due immediately because nothing was ever sent). What changed is everything after
it: 30 min instead of 10 between idle probes, none while real calls keep landing, and a longer wait
after a failure.
"""
import io
import json
import os
import re
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import bridge.copilot_bridge as B                      # noqa: E402
from tools import fleet_tool_health as FTH             # noqa: E402
from tools import settings_keys as SK                  # noqa: E402
from tools import settings_path as SP                  # noqa: E402
from tools import tool_probe                           # noqa: E402


def _read(*parts):
    return io.open(os.path.join(REPO, *parts), encoding="utf-8-sig").read()


# ---------------------------------------------------------------- the ledger evidence reader
def _write_ledger(path, rows):
    with io.open(str(path), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _call(cid, ts, tool, args=None):
    return {"event": "call", "id": cid, "ts": ts, "tool": tool, "args": args or {}}


def _out(cid, ts, ok=True, text="listed 3 entries"):
    return {"event": "outcome", "id": cid, "ts": ts, "ok": ok, "result": {"text": text}}


def test_only_a_real_successful_call_is_evidence(monkeypatch, tmp_path):
    ledger = tmp_path / "tool_events.jsonl"
    monkeypatch.setattr(FTH, "LEDGER", ledger)
    now = 100000.0
    _write_ledger(ledger, [
        # the probe's own call: names the challenge directory
        _call("p1", now - 100, "list_directory", {"path": "C:/x/.fleet/probe_challenge/probe_abc123abc123"}),
        _out("p1", now - 99),
        # discovery chatter
        _call("d1", now - 90, "call_tool.catalogue"), _out("d1", now - 89),
        # a refusal that returned normally
        _call("r1", now - 80, "write_file"), _out("r1", now - 79, ok=False, text="error"),
        # outside the window
        _call("o1", now - 5000, "read_file"), _out("o1", now - 4999),
    ])
    assert FTH.last_real_success(now, 1800.0) is None
    # ... and the real one, which is the only thing that counts, at the CALL's own time
    _write_ledger(ledger, [
        _call("p1", now - 100, "list_directory", {"path": "C:/x/.fleet/probe_challenge/probe_abc123abc123"}),
        _out("p1", now - 99),
        _call("g1", now - 700, "read_file"), _out("g1", now - 650),
    ])
    assert FTH.last_real_success(now, 1800.0) == (now - 700, "read_file")


def test_no_ledger_is_no_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(FTH, "LEDGER", tmp_path / "missing.jsonl")
    assert FTH.last_real_success(1000.0, 1800.0) is None


# ---------------------------------------------------------------- never green without evidence
def test_evidence_is_recorded_at_the_real_calls_time_and_never_moves_backwards(monkeypatch, tmp_path):
    pf = tmp_path / "tool_probe.json"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", pf)
    tool_probe._reset_summary_cache()
    # nothing recorded: nothing to claim
    assert tool_probe.get_summary(now=5000.0)["tool_ok"] is None
    tool_probe.record_probe(False, "timeout", ts=1000.0)         # an older, failed probe
    assert tool_probe.record_evidence(4000.0, "read_file", now=5000.0) is True
    tool_probe._reset_summary_cache()
    s = tool_probe.get_summary(now=5000.0)
    assert s["tool_ok"] is True and s["tool_ts"] == 4000.0       # the CALL's time, not 5000
    assert s["tool_age_s"] == 1000.0
    raw = json.loads(pf.read_text(encoding="utf-8"))
    assert raw["evidence"] == "real_call" and raw["tool"] == "read_file"
    assert raw["totals"]["probes"] == 1 and raw["totals"]["failures"] == 1   # nothing was PROBED
    # an older call (or the same one) does not overwrite a newer record
    assert tool_probe.record_evidence(3000.0, "x", now=5000.0) is False
    assert tool_probe.record_evidence(4000.0, "x", now=5000.0) is False
    # a timestamp from the future is a clock problem, not evidence
    assert tool_probe.record_evidence(9999999.0, "x", now=5000.0) is False


# ---------------------------------------------------------------- the setting and the pure rules
def _settings(monkeypatch, tmp_path, text):
    p = tmp_path / "settings.txt"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(SP, "NEW_PATH", str(p))
    return str(p)


@pytest.mark.parametrize("text,expected", [
    ("", 30), ("tool_probe_idle_min=15\n", 15), ("tool_probe_idle_min=60\n", 60),
    ("tool_probe_idle_min=0\n", 0), ("tool_probe_idle_min=junk\n", 30),
    ("tool_probe_idle_min=2\n", 30), ("tool_probe_idle_min=99999\n", 30),
    ("tool_probe_idle_min=45\n", 45),
])
def test_the_setting_reads_with_a_default_of_30(monkeypatch, tmp_path, text, expected):
    _settings(monkeypatch, tmp_path, text)
    assert tool_probe.idle_min_setting() == expected


def test_the_declared_default_is_the_one_the_code_and_the_panel_use():
    assert tool_probe.IDLE_MIN_DEFAULT == SK.default("tool_probe_idle_min") == 30
    assert SK.effect("tool_probe_idle_min") == SK.EACH_GATE
    cs = _read("ui", "EffortPolicy.cs")
    assert re.search(r'public const string Default = "30";', cs[cs.index("class ToolProbeView"):])
    m = re.search(r"class ToolProbeView.*?Choices = \{([^}]*)\}", cs, re.S)
    assert [c.strip().strip('"') for c in m.group(1).split(",")] == \
        [str(c) for c in tool_probe.IDLE_MIN_CHOICES]


def test_the_environment_variable_stays_the_override_of_last_resort():
    assert tool_probe.probe_interval(None, 30) == (1800.0, "setting")
    assert tool_probe.probe_interval(600.0, 30) == (600.0, "env")
    assert tool_probe.probe_interval(0.0, 30)[0] <= 0            # env 0 = off, whatever the setting
    assert tool_probe.probe_interval(None, 0)[0] <= 0            # setting 0 = off


def test_backoff_doubles_and_is_capped():
    assert tool_probe.backoff_s(1800.0, 0) == 1800.0
    assert tool_probe.backoff_s(1800.0, 1) == 3600.0
    assert tool_probe.backoff_s(1800.0, 2) == 7200.0
    assert tool_probe.backoff_s(1800.0, 9) == 7200.0             # the cap
    assert tool_probe.backoff_s(600.0, 3) == 4800.0
    assert tool_probe.backoff_s(10800.0, 3) == 10800.0           # never below the interval itself
    assert tool_probe.backoff_s(1800.0, "junk") == 1800.0


def test_derived_windows_follow_the_configured_interval(monkeypatch, tmp_path):
    _settings(monkeypatch, tmp_path, "tool_probe_idle_min=60\n")
    assert tool_probe.configured_interval_s({}) == 3600.0
    assert tool_probe.configured_interval_s({"MCP_TOOL_PROBE_SEC": "600"}) == 600.0
    _settings(monkeypatch, tmp_path, "tool_probe_idle_min=0\n")
    assert tool_probe.configured_interval_s({}) == 1800.0         # 'no probe' does not shrink a window


# ---------------------------------------------------------------- the bridge: decisions
@pytest.fixture
def bridge(monkeypatch, tmp_path):
    """The bridge with its probe state fresh, no env override, a settings file of our own and
    nothing that touches a browser."""
    monkeypatch.setattr(B, "MCP_TOOL_PROBE_SEC", None)
    monkeypatch.setattr(B, "_PROBE_RT", {"anchor": 0.0, "defer": 0.0, "fails": 0, "empty": 0})
    monkeypatch.setattr(B, "_PROBE_STATE", {"last_sent": None, "last_skipped_reason": None,
                                            "last_skipped_ts": None, "skipped_since_start": 0,
                                            "evidence_ts": None, "evidence_tool": None})
    monkeypatch.setattr(B, "_PROBE_STATE_WRITTEN", [None])
    monkeypatch.setattr(B, "_LAST_USER_TURN_TS", 0.0)
    monkeypatch.setattr(B, "PAGE", object())
    monkeypatch.setattr(B, "AGENT_URL", "https://example.invalid/agent")
    monkeypatch.setattr(B, "_PAGE_UNREACHABLE_STREAK", 0)
    monkeypatch.setattr(B, "_schedule_force_rehide", lambda *a, **kw: None)
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", tmp_path / "tool_probe.json")
    monkeypatch.setattr(tool_probe, "_STATE_PATH", tmp_path / "tool_probe_state.json")
    monkeypatch.setattr(tool_probe, "PROBE_FAILURE_JOURNAL", tmp_path / "failures.jsonl")
    monkeypatch.setattr(FTH, "LEDGER", tmp_path / "tool_events.jsonl")
    _settings(monkeypatch, tmp_path, "")
    tool_probe._reset_summary_cache()
    sent = []
    results = []

    def _fake_call(fn, *a, **kw):
        if fn is B._bridge_auto_consent:
            return False
        sent.append(1)
        return results.pop(0)

    monkeypatch.setattr(B, "_run_bounded_page_probe_call", _fake_call)
    monkeypatch.setattr(B.tool_probe, "new_probe_challenge",
                        lambda *a, **kw: ("test challenge", "TESTTOKEN"))
    monkeypatch.setattr(B, "_recycle_long_conversation", lambda *a, **kw: None)
    monkeypatch.setattr(B, "_report_recycle_memory_effect", lambda *a, **kw: None)
    rows = []
    import relay.mechanism_telemetry as MT
    monkeypatch.setattr(MT, "record", lambda mech, **kw: rows.append((mech, kw)) or {})
    return type("Ctx", (), {"sent": sent, "results": results, "rows": rows, "tmp": tmp_path})


def _state(ctx):
    return json.loads((ctx.tmp / "tool_probe_state.json").read_text(encoding="utf-8"))


def test_a_recent_real_call_skips_the_probe_and_is_the_evidence(monkeypatch, bridge):
    now = time.time()
    _write_ledger(bridge.tmp / "tool_events.jsonl", [
        _call("g1", now - 120, "read_file"), _out("g1", now - 119)])
    bridge.results.append((True, "", False))                       # would be the probe turn
    assert B._run_tool_probe() is not None
    assert bridge.sent == [], "a probe message was sent although a real call had just succeeded"
    raw = json.loads((bridge.tmp / "tool_probe.json").read_text(encoding="utf-8"))
    assert raw["ok"] is True and abs(raw["ts"] - (now - 120)) < 1.0    # the CALL's time
    assert raw["evidence"] == "real_call"
    st = _state(bridge)
    assert st["last_skipped_reason"] == "fleet_evidence"
    assert st["skipped_since_start"] == 1 and st["enabled"] is True
    assert st["interval_min"] == 30.0 and st["last_sent"] is None
    assert abs(st["evidence_ts"] - (now - 120)) < 1.0


def test_idle_past_the_interval_sends_a_probe(monkeypatch, bridge):
    """Golden for the first probe: nothing configured, nothing ever sent -> due immediately."""
    assert B._probe_not_due_yet() is None
    bridge.results.append((True, "found probe_TESTTOKEN", False))
    monkeypatch.setattr(B.tool_probe, "probe_arrived", lambda *a, **kw: True)
    assert B._run_tool_probe() is None
    assert bridge.sent == [1]
    summary = tool_probe.get_summary()
    assert summary["tool_ok"] is True and summary["tool_kind"] == "answer"
    st = _state(bridge)
    assert st["last_sent"] is not None and st["backoff_failures"] == 0
    # ... and the next one is a whole interval away, not 10 minutes
    wait = B._probe_not_due_yet()
    assert wait is not None and wait <= B.PROBE_POLL_SEC
    assert B._probe_due_at(1800.0) - time.time() > 1700.0


def test_a_stale_real_call_does_not_excuse_the_probe(monkeypatch, bridge):
    now = time.time()
    _write_ledger(bridge.tmp / "tool_events.jsonl", [
        _call("g1", now - 4000, "read_file"), _out("g1", now - 3999)])     # older than 30 min
    bridge.results.append((True, "x", False))
    monkeypatch.setattr(B.tool_probe, "probe_arrived", lambda *a, **kw: True)
    B._run_tool_probe()
    assert bridge.sent == [1]


def test_the_probes_own_call_is_not_evidence_for_the_next_probe(monkeypatch, bridge):
    now = time.time()
    _write_ledger(bridge.tmp / "tool_events.jsonl", [
        _call("p1", now - 60, "list_directory", {"path": "C:/r/.fleet/probe_challenge/probe_0123456789ab"}),
        _out("p1", now - 59)])
    bridge.results.append((True, "x", False))
    monkeypatch.setattr(B.tool_probe, "probe_arrived", lambda *a, **kw: True)
    B._run_tool_probe()
    assert bridge.sent == [1]


def test_never_green_without_an_event(monkeypatch, bridge):
    """Nothing in the ledger and no probe sent yet: the check says nothing at all."""
    assert tool_probe.get_summary()["tool_ok"] is None
    bridge.results.append((True, "", True))                          # a probe that timed out
    B._run_tool_probe()
    s = tool_probe.get_summary()
    assert s["tool_ok"] is False and s["tool_kind"] == "timeout"


def test_the_setting_zero_never_probes_and_never_claims(monkeypatch, bridge):
    _settings(monkeypatch, bridge.tmp, "tool_probe_idle_min=0\n")
    bridge.results.append((True, "x", False))
    assert B._run_tool_probe() is None
    assert bridge.sent == []
    assert tool_probe.get_summary()["tool_ok"] is None               # unknown, not green
    st = _state(bridge)
    assert st["enabled"] is False and st["last_skipped_reason"] == "disabled_setting"
    assert st["skipped_since_start"] == 0                            # off is not a skipped probe
    assert B._probe_not_due_yet() is None and B._probe_wait_s() == B.PROBE_POLL_SEC
    # switching it back on is noticed without a restart
    _settings(monkeypatch, bridge.tmp, "tool_probe_idle_min=15\n")
    assert B._probe_interval_s() == (900.0, "setting")


def test_the_environment_override_still_wins_over_the_setting(monkeypatch, bridge):
    _settings(monkeypatch, bridge.tmp, "tool_probe_idle_min=0\n")
    monkeypatch.setattr(B, "MCP_TOOL_PROBE_SEC", 600.0)
    assert B._probe_interval_s() == (600.0, "env")
    monkeypatch.setattr(B, "MCP_TOOL_PROBE_SEC", 0.0)
    _settings(monkeypatch, bridge.tmp, "tool_probe_idle_min=15\n")
    assert B._run_tool_probe() is None
    assert _state(bridge)["last_skipped_reason"] == "disabled_env"


def test_a_failed_probe_backs_off_instead_of_asking_again(monkeypatch, bridge):
    monkeypatch.setattr(B.tool_probe, "probe_arrived", lambda *a, **kw: False)
    monkeypatch.setattr(B, "PROBE_STUCK_CONVERSATION_FAILURES", 99)
    waits = []
    for _ in range(4):
        bridge.results.append((True, "something else", False))        # answers, call never arrived
        # the next probe is only ever due once the previous backoff has passed
        B._PROBE_RT["anchor"] = 0.0
        B._run_tool_probe()
        waits.append(B._probe_due_at(1800.0) - time.time())
    assert B._PROBE_RT["fails"] == 4
    assert waits[0] == pytest.approx(3600.0, abs=5)                   # 1 failure: x2
    assert waits[1] == pytest.approx(7200.0, abs=5)                   # 2: x4 ... capped at 2 h
    assert waits[3] == pytest.approx(7200.0, abs=5)
    mech = [r for r in bridge.rows if r[0] == "tool_probe_backoff"]
    assert len(mech) == 4 and mech[0][1]["extra"]["consecutive_failures"] == 1
    assert mech[0][1]["extra"]["next_probe_in_s"] == 3600
    assert _state(bridge)["backoff_failures"] == 4
    # the first success clears it
    bridge.results.append((True, "ok", False))
    monkeypatch.setattr(B.tool_probe, "probe_arrived", lambda *a, **kw: True)
    B._PROBE_RT["anchor"] = 0.0
    B._run_tool_probe()
    assert B._PROBE_RT["fails"] == 0


def test_an_empty_turn_retry_backs_off_too(monkeypatch, bridge):
    monkeypatch.setattr(B.tool_probe, "probe_arrived", lambda *a, **kw: False)
    out = []
    for _ in range(3):
        bridge.results.append((True, "", False))                      # came back empty, instantly
        out.append(B._run_tool_probe())
    assert out == [B.PROBE_EMPTY_TURN_RETRY_SEC, 2 * B.PROBE_EMPTY_TURN_RETRY_SEC,
                   4 * B.PROBE_EMPTY_TURN_RETRY_SEC]


def test_a_numeric_return_replaces_the_cadence_via_the_timer_tick(monkeypatch, bridge):
    """The tick turns 'run me again in N s' into a real deferral (anchor cleared)."""
    armed = []
    monkeypatch.setattr(B, "_run_tool_probe", lambda: 17.0)
    monkeypatch.setattr(B.threading, "Timer", lambda wait, fn: type(
        "T", (), {"daemon": False, "start": lambda self: armed.append((wait, fn))})())
    B._schedule_tool_probe(delay=0.0)
    wait, tick = armed.pop()
    tick()
    assert armed and armed[-1][0] == pytest.approx(17.0, abs=0.5)
    assert B._PROBE_RT["anchor"] == 0.0 and B._PROBE_RT["defer"] > time.time()


def test_the_status_route_exports_the_probe_state(bridge):
    snap = B._probe_state_snapshot()
    for key in ("enabled", "interval_min", "last_sent", "last_skipped_reason",
                "skipped_since_start"):
        assert key in snap, key
    assert snap["enabled"] is True and snap["interval_min"] == 30.0
    src = _read("bridge", "copilot_bridge.py")
    i = src.index('if parsed.path == "/status":')
    assert '"probe": _probe_state_snapshot()' in src[i:i + 6000]
    assert "/status" in src[src.index("BRIDGE_OPEN_ROUTES ="):][:120]   # still the open route


# ---------------------------------------------------------------- the cockpit
def test_the_cockpit_has_the_control_in_the_settings_popup_and_not_the_header():
    cs = _read("ui", "FleetCockpit.cs")
    assert len(re.findall(r"SaveKey\(ToolProbeView\.Key, _tpVal\)", cs)) == 1
    assert re.search(r'case "tool_probe_idle_min":', cs)             # the timing switch names it
    assert "col.Children.Add(ToolProbeControl());" in cs
    recovery = cs.index('SectionHeader(L("復旧 / Recovery", "Recovery"))')
    assert recovery < cs.index("col.Children.Add(ToolProbeControl());") < recovery + 800
    m = re.search(r"UIElement ToolProbeControl\(\).*?\n    }\n", cs, re.S)
    assert m and "SelectionChanged" in m.group(0) and "sel == _tpVal" in m.group(0)  # no re-fire
    m = re.search(r"void PaintToolProbe\(\).*?\n    }\n", cs, re.S)
    assert m and "!Equals(ComboVal(_tpBox), _tpVal)" in m.group(0)       # paint guard
    m = re.search(r"void PaintToolProbeInEffect\(\).*?\n    }\n", cs, re.S)
    assert m and "ReadToolProbeState()" in m.group(0)
    assert "PaintToolProbeInEffect();" in cs[cs.index("PaintAutoResumeInEffect(root);"):][:400]


def test_the_health_dot_follows_the_interval_and_goes_grey_when_off():
    cs = _read("ui", "FleetCockpit.cs")
    i = cs.index("void PollToolProbeOnce(DateTime now)")
    body = cs[i:cs.index("string AgeMinutesText(double ageMin)", i)]
    assert "ageMin >= ToolProbeStaleAfterMin()" in body
    assert "ageMin >= 20.0" not in body                                # no fixed 20 minutes any more
    off = body.index("ToolProbeReportedOff())\n")
    assert off < body.index("ageMin >= ToolProbeStaleAfterMin()")
    assert "HealthState.Gray" in body[off:off + 600]
    cs2 = _read("ui", "EffortPolicy.cs")
    assert re.search(r"Math\.Max\(20\.0, intervalMin \+ 10\.0\)", cs2)

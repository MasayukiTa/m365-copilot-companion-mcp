"""Hermetic tests for tools/tool_probe.py -- the tool-call self-probe tracker added so a
bridge-side MCP connector going stale (consent lapsed / CDP session dead) is visible to
/health instead of silently failing behind an all-green FleetCockpit health strip (see
tool_probe.py's module docstring for the full incident context).

Covers: the pure classify_probe_reply() classifier for every (ok, kind) branch, the
record_probe()/get_summary() roundtrip for every kind, corrupt/missing-file tolerance,
deterministic age computation via injected `now`, atomic-write (no partial .tmp left
behind), and never-raises behavior when the sidecar path is unwritable.

Run: pytest -q tools\\test_tool_probe.py
"""
from __future__ import annotations

import json

from tools import tool_probe


# ===========================================================================
# 1. classify_probe_reply: pure classifier, every branch
# ===========================================================================


def test_classify_agent_unreachable_wins_even_with_ok_token():
    # agent_loaded=False must short-circuit before any text inspection.
    ok, kind = tool_probe.classify_probe_reply(
        tool_probe.PROBE_OK_TOKEN, agent_loaded=False)
    assert (ok, kind) == (False, "agent_unreachable")


def test_classify_agent_unreachable_on_empty_reply():
    ok, kind = tool_probe.classify_probe_reply("", agent_loaded=False)
    assert (ok, kind) == (False, "agent_unreachable")


def test_classify_consent_card_japanese_marker():
    ok, kind = tool_probe.classify_probe_reply(
        "接続マネージャーを開く からご確認ください。", agent_loaded=True)
    assert (ok, kind) == (False, "consent_card")


def test_classify_consent_card_english_marker():
    ok, kind = tool_probe.classify_probe_reply(
        "Please open the connection manager to continue.", agent_loaded=True)
    assert (ok, kind) == (False, "consent_card")


def test_classify_canned_fallback_marker():
    ok, kind = tool_probe.classify_probe_reply(
        "申し訳ございません、ローカル操作は実行不可です。", agent_loaded=True)
    assert (ok, kind) == (False, "canned_fallback")


def test_classify_canned_fallback_no_connector_marker():
    ok, kind = tool_probe.classify_probe_reply(
        "コネクタがありませんのでお手伝いできません。", agent_loaded=True)
    assert (ok, kind) == (False, "canned_fallback")


def test_classify_answer_when_token_present():
    reply = "Desktop 直下は 12個です。\n" + tool_probe.PROBE_OK_TOKEN
    ok, kind = tool_probe.classify_probe_reply(reply, agent_loaded=True)
    assert (ok, kind) == (True, "answer")


def test_classify_error_when_no_marker_and_no_token():
    ok, kind = tool_probe.classify_probe_reply(
        "すみません、よくわかりませんでした。", agent_loaded=True)
    assert (ok, kind) == (False, "error")


def test_classify_error_on_none_reply_when_agent_loaded():
    ok, kind = tool_probe.classify_probe_reply(None, agent_loaded=True)
    assert (ok, kind) == (False, "error")


def test_classify_consent_marker_precedes_ok_token_check():
    # If a reply somehow contains both a consent marker AND the ok token (shouldn't happen
    # in practice), consent_card must win -- a partial/garbled turn is never "ok".
    reply = "接続マネージャーを開く\n" + tool_probe.PROBE_OK_TOKEN
    ok, kind = tool_probe.classify_probe_reply(reply, agent_loaded=True)
    assert (ok, kind) == (False, "consent_card")


# ===========================================================================
# 2. record_probe / get_summary: roundtrip for every kind
# ===========================================================================


def test_record_then_get_summary_roundtrip_for_every_kind(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    for kind in tool_probe.PROBE_KINDS:
        ok = (kind == "answer")
        tool_probe.record_probe(ok, kind, detail="d-%s" % kind, ts=1000.0)
        summary = tool_probe.get_summary(now=1005.0)
        assert summary["tool_ok"] is ok
        assert summary["tool_kind"] == kind
        assert summary["tool_ts"] == 1000.0
        assert summary["tool_age_s"] == 5.0


def test_record_probe_writes_expected_json_shape(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    tool_probe.record_probe(True, "answer", detail="12個", ts=42.0)
    assert probe_file.is_file()
    on_disk = json.loads(probe_file.read_text(encoding="utf-8"))
    assert on_disk == {"ts": 42.0, "ok": True, "kind": "answer", "detail": "12個"}


def test_record_probe_defaults_ts_to_wallclock(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    import time as _time
    before = _time.time()
    tool_probe.record_probe(True, "answer")
    after = _time.time()
    on_disk = json.loads(probe_file.read_text(encoding="utf-8"))
    assert before <= on_disk["ts"] <= after


# ===========================================================================
# 3. get_summary: missing / corrupt file tolerance
# ===========================================================================


def test_get_summary_missing_file_returns_null_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", tmp_path / "does_not_exist.json")
    summary = tool_probe.get_summary(now=1000.0)
    assert summary == {"tool_ok": None, "tool_kind": None, "tool_ts": None, "tool_age_s": None}


def test_get_summary_corrupt_json_returns_null_shape(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    probe_file.write_text("{not valid json::", encoding="utf-8")
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    summary = tool_probe.get_summary(now=1000.0)
    assert summary == {"tool_ok": None, "tool_kind": None, "tool_ts": None, "tool_age_s": None}


def test_get_summary_missing_ts_key_returns_null_shape(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    probe_file.write_text(json.dumps({"ok": True, "kind": "answer"}), encoding="utf-8")
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    summary = tool_probe.get_summary(now=1000.0)
    assert summary == {"tool_ok": None, "tool_kind": None, "tool_ts": None, "tool_age_s": None}


def test_get_summary_non_numeric_ts_returns_null_shape(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    probe_file.write_text(json.dumps({"ts": "not-a-number", "ok": True, "kind": "answer"}),
                           encoding="utf-8")
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    summary = tool_probe.get_summary(now=1000.0)
    assert summary == {"tool_ok": None, "tool_kind": None, "tool_ts": None, "tool_age_s": None}


# ===========================================================================
# 4. Deterministic age computation
# ===========================================================================


def test_age_computation_uses_injected_now(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    tool_probe.record_probe(True, "answer", ts=500.0)
    assert tool_probe.get_summary(now=500.0)["tool_age_s"] == 0.0
    assert tool_probe.get_summary(now=530.0)["tool_age_s"] == 30.0


def test_age_never_negative_even_if_now_before_ts(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    tool_probe.record_probe(True, "answer", ts=1000.0)
    # now < ts (clock skew across threads/processes) -- must clamp to 0, not go negative.
    assert tool_probe.get_summary(now=990.0)["tool_age_s"] == 0.0


# ===========================================================================
# 5. Atomic write
# ===========================================================================


def test_record_probe_is_atomic_no_tmp_left_behind(monkeypatch, tmp_path):
    probe_file = tmp_path / "nested" / "tool_probe.json"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    tool_probe.record_probe(True, "answer", ts=1.0)
    assert probe_file.is_file()
    tmp_sibling = probe_file.parent / (probe_file.name + ".tmp")
    assert not tmp_sibling.exists(), "temp file must be replaced away, not left behind"


def test_record_probe_overwrites_previous_snapshot(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    tool_probe.record_probe(False, "consent_card", ts=1.0)
    tool_probe.record_probe(True, "answer", ts=2.0)
    on_disk = json.loads(probe_file.read_text(encoding="utf-8"))
    assert on_disk["kind"] == "answer"
    assert on_disk["ts"] == 2.0


# ===========================================================================
# 6. Never-raises on an unwritable path
# ===========================================================================


def test_record_probe_never_raises_on_unwritable_path(monkeypatch):
    from pathlib import Path
    # A path with a NUL byte is invalid on every OS -- guaranteed unwritable.
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", Path("\x00bad\x00path\x00tool_probe.json"))
    tool_probe.record_probe(True, "answer")  # must not raise


def test_get_summary_never_raises_on_unreadable_path(monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", Path("\x00bad\x00path\x00tool_probe.json"))
    summary = tool_probe.get_summary()  # must not raise
    assert summary == {"tool_ok": None, "tool_kind": None, "tool_ts": None, "tool_age_s": None}

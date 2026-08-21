"""The main chat must auto-unlock too, not only fleet runs.

Write/exec MCP tools are locked per client IP. The relay has injected an unlock turn
for fleet runs for a long time; the bridge had no such code at all, so the identical
lock in the main chat surfaced as the agent asking a human to paste a password that
was already in .env on the same machine.

These are source-level checks: bridge/copilot_bridge.py imports Playwright at module
scope and cannot be imported on a CI runner, so the wiring is asserted on the file the
way ui/test_fleet_cockpit_approval_center.py does for the C# UI.

Run: pytest -q bridge/test_bridge_unlock.py
"""
from pathlib import Path

SOURCE = (Path(__file__).with_name("copilot_bridge.py")).read_text(encoding="utf-8")


def test_bridge_has_an_auto_unlock_path_at_all():
    assert "_bridge_should_auto_unlock" in SOURCE
    assert "BRIDGE_UNLOCK_PREFIX" in SOURCE
    assert "MAX_BRIDGE_UNLOCK_ATTEMPTS" in SOURCE


def test_detection_asks_the_server_record_not_the_agent_wording():
    """Reading the reply's wording is what broke the relay's version: the injected
    operator discipline makes the agent paraphrase the tool error, so the server's
    literal marker never appears."""
    body = SOURCE[SOURCE.index("def _bridge_should_auto_unlock"):]
    body = body[:body.index("\ndef ")]
    # STILL THE SERVER'S RECORD, asked more precisely. matching_record returns WHICH refusal
    # locked_since agreed to, so the reader can check whether it was even about a caller like
    # itself -- a context-less refusal is always somebody else's. The property this test
    # protects is that detection reads the record and not the agent's prose; the call it
    # pinned by name has moved.
    assert "lock_state.matching_record(sent_at)" in body
    assert "locked client ip" not in body.lower()


def test_the_unlock_turn_is_wired_into_the_shared_turn_helper():
    """_run_one_turn is what both the single-turn path and the work loop call, so the
    retry has to live there to cover both."""
    body = SOURCE[SOURCE.index("def _run_one_turn"):]
    body = body[:body.index("\n    def _stream_text")]
    assert "_bridge_should_auto_unlock(_turn_sent_at)" in body
    assert "_send_and_stream_once" in body
    assert "BRIDGE_UNLOCK_PREFIX % pw" in body


def test_first_turn_proactively_unlocks_before_tool_discovery():
    body = SOURCE[SOURCE.index("def _run_one_turn"):]
    body = body[:body.index("\n    def _stream_text")]
    assert "_BRIDGE_UNLOCK_PREFLIGHT_DONE" in body
    assert "turn_payload = (BRIDGE_UNLOCK_PREFIX % pw) + msg" in body
    assert "_send_and_stream_once(turn_payload" in body


def test_retries_are_capped_so_a_rotating_ip_cannot_loop():
    body = SOURCE[SOURCE.index("def _bridge_should_auto_unlock"):]
    body = body[:body.index("\ndef ")]
    assert "MAX_BRIDGE_UNLOCK_ATTEMPTS" in body
    assert "_BRIDGE_UNLOCK_ATTEMPTS +=" in SOURCE


def test_password_is_read_locally_and_never_persisted_into_the_agent():
    assert "unlock_password_local" in SOURCE
    # The prefix instructs the agent to stop echoing it once the unlock succeeded.
    assert "password を二度と出力しないこと" in SOURCE


def test_relay_and_bridge_share_one_password_reader():
    """The env-then-dotenv fallback used to exist only in the relay, which is precisely
    why the bridge had nothing to fall back to."""
    relay = (Path(__file__).parent.parent / "relay" / "relay_fleet.py").read_text(encoding="utf-8")
    assert "unlock_password_local" in relay
    assert "load_dotenv" not in relay.split("def _unlock_password")[1].split("\ndef ")[0]


def test_the_check_is_scoped_to_the_turn_not_to_recent_history():
    """A refusal from an unrelated earlier call must not mark this turn as locked --
    CI caught exactly that contamination in the relay's version."""
    assert "_bridge_should_auto_unlock(_turn_sent_at)" in SOURCE
    assert "locked_recently" not in SOURCE


# ---- 誰の拒否かを尋ねる（2026-08-21、艦隊側と同じ欠陥がここにも生きていた） -------------------
#
# 実測: relay が毎ターン、HTTP コンテキストの無い拒否を複数回生産していた。
# 「最近誰か拒否されたか」しか尋ねない読み手は、ロックされていないターンに
# unlock を注入する。艦隊側は直したが、bridge には同じ読み手が残っていた。

def _lock_tmp(tmp_path, monkeypatch):
    from pathlib import Path

    import tools.lock_state as LS
    monkeypatch.setattr(LS, "_LOG_FILE", Path(str(tmp_path / "refusals.jsonl")))
    monkeypatch.setattr(LS, "_STATE_FILE", Path(str(tmp_path / "state.json")))
    return LS


def test_a_context_less_refusal_does_not_trigger_the_bridge(tmp_path, monkeypatch):
    import bridge.copilot_bridge as B
    LS = _lock_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(B, "_BRIDGE_UNLOCK_ATTEMPTS", 0, raising=False)
    LS.record_locked("", "[locked: no HTTP request context] Call unlock(...) first.", ts=100.0)
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 101.0)
    assert B._bridge_should_auto_unlock(99.0) is False


def test_a_real_refusal_still_triggers_the_bridge(tmp_path, monkeypatch):
    """フィルタは『誰のものか分からない拒否』を外すのであって、拒否を無視しない。"""
    import bridge.copilot_bridge as B
    LS = _lock_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(B, "_BRIDGE_UNLOCK_ATTEMPTS", 0, raising=False)
    LS.record_locked("203.0.113.7", "[locked client IP: '203.0.113.7'] ...", ts=100.0)
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 101.0)
    assert B._bridge_should_auto_unlock(99.0) is True


def test_a_blank_ip_from_a_real_request_still_triggers_the_bridge(tmp_path, monkeypatch):
    """空 IP でも『[locked client IP』で始まるなら実リクエスト由来。
    見える失敗の側に倒す -- 静かに素通りさせない。"""
    import bridge.copilot_bridge as B
    LS = _lock_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(B, "_BRIDGE_UNLOCK_ATTEMPTS", 0, raising=False)
    LS.record_locked("", "[locked client IP: ''] ...", ts=100.0)
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 101.0)
    assert B._bridge_should_auto_unlock(99.0) is True


def test_an_old_refusal_still_does_not_count(tmp_path, monkeypatch):
    import bridge.copilot_bridge as B
    LS = _lock_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(B, "_BRIDGE_UNLOCK_ATTEMPTS", 0, raising=False)
    LS.record_locked("203.0.113.7", "[locked client IP: ...] ...", ts=50.0)
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 101.0)
    assert B._bridge_should_auto_unlock(99.0) is False

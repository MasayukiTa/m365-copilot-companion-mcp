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
    assert "lock_state.locked_since(sent_at)" in body
    assert "locked client ip" not in body.lower()


def test_the_unlock_turn_is_wired_into_the_shared_turn_helper():
    """_run_one_turn is what both the single-turn path and the work loop call, so the
    retry has to live there to cover both."""
    body = SOURCE[SOURCE.index("def _run_one_turn"):]
    body = body[:body.index("\n    def _stream_text")]
    assert "_bridge_should_auto_unlock(_turn_sent_at)" in body
    assert "_send_and_stream_once" in body
    assert "BRIDGE_UNLOCK_PREFIX % pw" in body


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

# -*- coding: utf-8 -*-
"""Regression tests for the 2026-09-25/26 unlock/retry incident.

The production failure was not a bad password: unlock grants succeeded, but the harness raced
in-flight turns, carried a stale "model must remember unlock_token forever" contract, and then
re-queued deterministic terminal results into fresh locked conversations.
"""
import time

from relay import relay_fleet as RF


LOCKED = ("[locked: no valid unlock token for '203.0.113.7'] "
          "This conversation is not unlocked. Call unlock(password='<password>') again now.")


def test_recovery_contract_uses_session_auth_not_model_memory():
    text = RF.UNLOCK_PREFIX
    assert "MCP session" in text
    assert "unlock_token" in text
    assert "毎回再添付する必要はありません" in text
    assert ".env" in text and "読まない" in text


def test_a_grant_already_in_the_server_ledger_cancels_reunlock(monkeypatch):
    monkeypatch.setenv("MCP_UNLOCK_PASSWORD", "pw-for-test")
    monkeypatch.setattr(RF, "_worker_recently_granted", lambda _name, **_kw: True)
    monkeypatch.setattr(RF, "_exclusively_refused", lambda *_a, **_k: False)
    w = RF.RelayWorker("write a file", "w-granted")
    w._decide(LOCKED)
    assert w._unlock_attempts == 0
    assert w.status == "ready"
    assert "pw-for-test" not in (w.job or "")
    assert "grant confirmed" in (w.reason or "")


def test_exclusive_classification_records_the_consumed_refusal(monkeypatch):
    """An exact turn-window attribution must preserve the refusal row it consumed.

    Before this regression fix, exclusive-attribution wrote consumed=None, so the fallback sweep
    could not tell which refusal had already been handled and had to use a broad timestamp guess.
    """
    from relay import turn_windows as TW
    from tools import lock_state as LS

    rec = {
        "ts": 105.0,
        "event": "refused",
        "detail": "[locked: no valid unlock token for '203.0.113.7']",
        "session": "session-exact",
    }
    seen = {}

    monkeypatch.setattr(LS, "matching_records", lambda since, now=None: [rec])

    def _record(branch, *, resp_len, since, consumed=None, attribution=None):
        seen.update(
            branch=branch,
            resp_len=resp_len,
            since=since,
            consumed=consumed,
            attribution=attribution,
        )

    monkeypatch.setattr(LS, "record_classification", _record)
    TW.reset()
    TW.open_turn("w0", 100.0)
    try:
        assert RF._looks_locked("ordinary reply with no lock marker", since=100.0, worker="w0")
    finally:
        TW.reset()

    assert seen["branch"] == "exclusive-attribution"
    assert seen["consumed"] == rec
    assert seen["attribution"]["worker"] == "w0"
    assert seen["attribution"]["exclusive"] is True
    assert seen["attribution"]["session"] == "session-exact"


def test_recent_grant_joins_on_consumed_session_even_when_grant_time_is_ambiguous(monkeypatch):
    """The refusal may be exclusively attributable while the later grant occurs under concurrency.

    The session id is the stable join. Requiring the grant timestamp itself to be exclusive loses
    a real success signal exactly when several workers overlap.
    """
    from relay import turn_windows as TW
    from tools import lock_state as LS

    now = time.time()
    refusal_ts = now - 10.0
    monkeypatch.setattr(
        LS,
        "classifications",
        lambda since, now=None: [{
            "ts": refusal_ts + 1.0,
            "event": "classified_locked",
            "consumed": {"ts": refusal_ts, "session": "session-joined"},
            "attribution": {
                "worker": "w0",
                "exclusive": True,
                "session": "session-joined",
            },
        }],
    )
    monkeypatch.setattr(
        LS,
        "granted_records",
        lambda since, now=None: [{
            "ts": refusal_ts + 5.0,
            "event": "granted",
            "session": "session-joined",
            "via": "password",
        }],
    )
    monkeypatch.setattr(TW, "belongs_to", lambda *_a, **_k: False)

    assert RF._worker_recently_granted("w0", since=now - 20.0)


def test_a_grant_older_than_the_refusal_does_not_cancel_current_recovery(monkeypatch):
    from relay import turn_windows as TW
    from tools import lock_state as LS

    now = time.time()
    refusal_ts = now - 10.0
    monkeypatch.setattr(
        LS,
        "classifications",
        lambda since, now=None: [{
            "ts": refusal_ts + 1.0,
            "event": "classified_locked",
            "consumed": {"ts": refusal_ts, "session": "session-reused"},
            "attribution": {
                "worker": "w0",
                "exclusive": True,
                "session": "session-reused",
            },
        }],
    )
    monkeypatch.setattr(
        LS,
        "granted_records",
        lambda since, now=None: [{
            "ts": refusal_ts - 5.0,
            "event": "granted",
            "session": "session-reused",
            "via": "password",
        }],
    )
    monkeypatch.setattr(TW, "belongs_to", lambda *_a, **_k: False)

    assert not RF._worker_recently_granted("w0", since=now - 20.0)




def test_agent_rule8_is_session_first_not_token_memory():
    """The always-visible agent instruction must agree with the server's actual auth model."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    start = src.index('"RULE 8:')
    end = src.index('"Relative user-folder names', start)
    rule = src[start:end]
    assert "same conversation" in rule
    assert "authorizes this MCP session automatically" in rule
    assert "Do NOT try to remember" in rule
    assert "only a fallback" in rule
    assert "pass it on every" not in rule
    assert "KEEP that" not in rule


def test_session_auth_refusal_stays_inside_relays_lock_dominance_budget(monkeypatch):
    """The security message is itself part of the relay protocol.

    If it grows to >= LOCKED_DOMINANCE_MAX_CHARS, the marker branch intentionally treats it as
    long prose rather than a raw tool refusal and automatic unlock can disappear. Use a long
    IPv6 identity so the test keeps real headroom instead of only passing for short IPv4 text.
    """
    from types import SimpleNamespace
    from tools import security as sec

    ip = "ffff:ffff:ffff:ffff:ffff:ffff:255.255.255.255"

    class Headers:
        def get(self, key, default=""):
            return ip if key.lower() == "x-forwarded-for" else default

    req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers=Headers())
    state = {
        ip: {
            "expires_at": time.time() + 3600.0,
            "token_hashes": [],
            "sessions": {},
        }
    }
    monkeypatch.setattr(sec, "get_http_request", lambda: req)
    monkeypatch.setattr(sec, "_load_state", lambda: state)
    monkeypatch.setattr(sec, "_current_session_fingerprint", lambda: "session-for-length-test")
    monkeypatch.setattr(sec, "presented_token", lambda: "")
    monkeypatch.setattr(sec, "session_auth_enabled", lambda: True)
    monkeypatch.setattr(sec, "enforce_unlock_token", lambda: True)
    monkeypatch.setattr(sec.lock_state, "record_locked", lambda *_a, **_k: None)

    result = sec.require_unlocked()
    assert result.startswith(RF.TOKEN_MISSING_REFUSAL)
    assert "Session auth makes unlock_token optional" in result
    assert len(result) < RF.LOCKED_DOMINANCE_MAX_CHARS, (
        len(result), RF.LOCKED_DOMINANCE_MAX_CHARS, result
    )


def test_deterministic_stuck_marks_worker_nonretryable(monkeypatch):
    monkeypatch.setattr(RF.RelayWorker, "_raise_stuck_gate", lambda *_a, **_k: False)
    w = RF.RelayWorker("g", "w-stuck")
    # Seed the previous STUCK reason so the next equivalent one hits convergence.
    w._last_stuck_reason = "the required external value does not exist and cannot be inferred"
    reply = "STUCK: the required external value does not exist and cannot be inferred"
    w._decide(reply)
    assert w.outcome == "STUCK"
    assert w.retryable_override is False

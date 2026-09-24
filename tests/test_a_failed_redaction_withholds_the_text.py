# -*- coding: utf-8 -*-
"""A redactor that fails must withhold the text, not write it (SEC-18).

WHAT WAS WRONG. Three places persisted turn text through the secret redactor and all three
FAILED OPEN: relay/relay_fleet._redact_unlock_password and bridge/copilot_bridge's copy
returned the ORIGINAL text if importing or calling the redactor raised, and
tools/secret_store.redact_secrets swallowed its own failure and returned the text as far as it
had got. The turn that carries the injected unlock password is exactly the text they were
redacting, so a broken redactor wrote the password into transcripts and the session ledger.

A fourth hole sat beside them and is pinned here too: selection was by variable NAME, and on an
install that stores the password DPAPI-protected (MCP_UNLOCK_PASSWORD_PROTECTED) the name
matched the CIPHERTEXT, while the turn carries the decrypted PLAINTEXT. The redactor never
matched the one value it exists for.

WHAT THESE TESTS DO. Break the redactor at each seam at RUNTIME (monkeypatch it to raise, make
its import fail), drive the real persisting code with a canary secret, and read back what was
persisted. They also drive the live composition of a turn with the redactor broken, so the fix
cannot pass by breaking the unlock itself.
"""
import json
import logging
import os
import sys

import pytest

CANARY = "canary-UNLOCK-7f3e9a1c55"
MARKER = "[redaction failed: content withheld]"


def _boom(*a, **k):
    raise RuntimeError("redactor exploded while holding %s" % CANARY)


@pytest.fixture
def no_db(monkeypatch):
    """The transcript mirrors every line into bridge.session_store's real database. Capture
    those rows instead, so the test reads what WOULD have been stored and touches nothing."""
    import bridge.session_store as store
    rows = []
    monkeypatch.setattr(store, "record_fleet_turn",
                        lambda key, obj, name="", goal="": rows.append(obj))
    return rows


# ------------------------------------------------------------------ the shared redactor

def test_the_markers_are_one_literal():
    import tools.secret_store as ss
    import relay.relay_fleet as rf
    import bridge.copilot_bridge as cb
    assert ss.REDACTION_FAILED_MARKER == MARKER
    assert rf._REDACTION_FAILED_MARKER == ss.REDACTION_FAILED_MARKER
    assert cb._REDACTION_FAILED_MARKER == ss.REDACTION_FAILED_MARKER


def test_redact_secrets_withholds_the_whole_text_when_it_cannot_collect(monkeypatch, caplog):
    import tools.secret_store as ss
    monkeypatch.setattr(ss, "secret_values", _boom)
    with caplog.at_level(logging.WARNING, logger=ss.__name__):
        out = ss.redact_secrets("unlock with %s please" % CANARY)
    assert out == MARKER
    assert CANARY not in caplog.text, "the failure log carried the secret"
    assert "redact_secrets failed (RuntimeError)" in caplog.text, "the failure was not logged"


def test_redact_secrets_withholds_when_replacement_itself_fails(monkeypatch):
    import tools.secret_store as ss

    class Evil(str):
        def replace(self, *a, **k):
            raise MemoryError("mid-replacement")

    monkeypatch.setattr(ss, "secret_values", lambda environ=None: [CANARY])
    assert ss.redact_secrets(Evil("x %s y" % CANARY)) == MARKER


def test_an_env_file_that_cannot_be_read_is_a_failure_not_an_empty_list(monkeypatch):
    """A secret that lives only in .env was silently missed when .env could not be read."""
    import dotenv
    import tools.secret_store as ss
    real_isfile = os.path.isfile
    monkeypatch.setattr(os.path, "isfile",
                        lambda p: True if str(p).endswith(".env") else real_isfile(p))
    monkeypatch.setattr(dotenv, "dotenv_values", _boom)
    assert ss.redact_secrets("text %s" % CANARY, environ={}) == MARKER


def test_redaction_still_redacts_normally(monkeypatch):
    import tools.secret_store as ss
    monkeypatch.setattr(ss, "secret_values", lambda environ=None: [CANARY])
    assert ss.redact_secrets("a %s b" % CANARY) == "a %s b" % ss.REDACTION_MARKER


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_a_dpapi_protected_password_is_redacted_by_its_plaintext(monkeypatch):
    import tools.secret_store as ss
    protected = ss.protect_secret(CANARY)
    env = {ss.UNLOCK_PASSWORD_PROTECTED_VAR: protected}
    # What the injector would put in the turn is the plaintext:
    assert ss.unlock_password_from_env(env) == CANARY
    out = ss.redact_secrets("unlock %s now" % CANARY, environ=env)
    assert CANARY not in out, "the decrypted unlock password reached the record"
    assert ss.REDACTION_MARKER in out


# ------------------------------------------------------------------ the fleet transcript

def _transcript(tmp_path):
    import relay.relay_fleet as rf
    return rf._Transcript(str(tmp_path / "tr"), "run_w0", "w0", "a goal")


def _persisted(t, rows):
    with open(t.path, encoding="utf-8") as fh:
        return fh.read() + json.dumps(rows, ensure_ascii=False)


@pytest.mark.parametrize("how", ["collect_raises", "redactor_raises", "import_fails"])
def test_the_fleet_transcript_never_holds_the_canary(how, monkeypatch, tmp_path, no_db, capsys):
    import tools.secret_store as ss
    if how == "collect_raises":
        monkeypatch.setattr(ss, "secret_values", _boom)
    elif how == "redactor_raises":
        monkeypatch.setattr(ss, "redact_secrets", _boom)
    else:
        monkeypatch.setitem(sys.modules, "tools.secret_store", None)   # import raises
    t = _transcript(tmp_path)
    t.user(1, "UNLOCK %s then do the work" % CANARY)
    t.assistant(1, "ok, I was given %s" % CANARY)
    stored = _persisted(t, no_db)
    assert CANARY not in stored, "the secret reached the persisted transcript (%s)" % how
    assert stored.count(MARKER) >= 4, "both turns, file and database, must carry the marker"
    if how != "collect_raises":
        err = capsys.readouterr().err
        assert "[transcript] redaction failed" in err and CANARY not in err


def test_the_live_turn_still_carries_the_password_with_the_redactor_broken(monkeypatch):
    """Withholding the RECORD must not withhold the TURN: the unlock only works if the real
    password reaches the agent."""
    import relay.relay_fleet as rf
    import tools.secret_store as ss
    monkeypatch.setattr(ss, "secret_values", _boom)
    monkeypatch.setattr(ss, "redact_secrets", _boom)
    monkeypatch.setattr(rf, "_unlock_password", lambda: CANARY)
    body, did = rf._initial_job_with_unlock("the goal")
    assert did is True and CANARY in body and "the goal" in body


# ------------------------------------------------------------------ the bridge ledger

class _FakeStore:
    def __init__(self):
        self.turns = []

    def append_turn(self, sid, role, text):
        self.turns.append((sid, role, text))

    def load(self, sid):
        return {"conv_url": ""}

    def __getattr__(self, name):          # anything else _persist_exchange touches: a no-op
        return lambda *a, **k: None


@pytest.mark.parametrize("how", ["collect_raises", "redactor_raises", "import_fails"])
def test_the_bridge_ledger_never_holds_the_canary(how, monkeypatch, caplog):
    import bridge.copilot_bridge as cb
    import tools.secret_store as ss
    store = _FakeStore()
    monkeypatch.setattr(cb, "S", store)
    if how == "collect_raises":
        monkeypatch.setattr(ss, "secret_values", _boom)
    elif how == "redactor_raises":
        monkeypatch.setattr(ss, "redact_secrets", _boom)
    else:
        monkeypatch.setitem(sys.modules, "tools.secret_store", None)
    with caplog.at_level(logging.WARNING):
        cb._persist_exchange("sid-1", "UNLOCK %s then go" % CANARY, "echo %s" % CANARY)
    assert len(store.turns) >= 2, "the exchange was not persisted at all"
    blob = json.dumps(store.turns, ensure_ascii=False)
    assert CANARY not in blob, "the secret reached the session ledger (%s)" % how
    assert [t[2] for t in store.turns[:2]] == [MARKER, MARKER]
    assert CANARY not in caplog.text
    if how != "collect_raises":
        assert "ledger redaction failed" in caplog.text

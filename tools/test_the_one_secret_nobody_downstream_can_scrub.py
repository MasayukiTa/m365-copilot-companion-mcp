# -*- coding: utf-8 -*-
"""repair_unlock_password invents a password. This is what stops the invention being printed.

WHY THE EXISTING GUARD MISSED IT. scripts/test_repair_unlock_output.py reads the source of
scripts/repair_unlock.py and requires that anything interpolated into a print has been through
_scrub. That check passed throughout, and it was answering a different question: whether the
caller scrubs, not whether the scrub can reach the value. _scrub removes the values it finds in
the parsed .env, and repair_unlock_password's entire purpose is to mint a password that is in
no .env anyone parsed. The one value most in need of removal was the one value the sanitizer
could never see.

Reported by CodeQL as py/clear-text-logging-sensitive-data (alert #32), at
`print("noop:%s" % reason)`. Taint tracking was right and the human reading of it -- "reason is
scrubbed, so this is a false positive" -- was wrong.

The redaction now happens where the value is known exactly: inside the function that minted it.
These tests drive the real failure path rather than reading the source, because reading the
source is what already passed.
"""
from __future__ import annotations

import pytest

import tools.env_portability as EP


FRESH_RE = r"[0-9a-f]{16}"    # binascii.hexlify(os.urandom(8))


@pytest.fixture()
def failing_protect(monkeypatch, tmp_path):
    """A .env that exists, no readable password, an undecryptable stored one -- the exact narrow
    condition repair_unlock_password acts on -- and a protect_secret that fails the way the ones
    that fail do: by quoting its argument."""
    import tools.secret_store as SS

    monkeypatch.setattr(SS, "unlock_password_from_env", lambda env=None: "")
    monkeypatch.setattr(SS, "unlock_password_problem",
                        lambda *a, **k: SS.PROBLEM_UNDECRYPTABLE)

    def _boom(value):
        raise ValueError("DPAPI refused to protect %r on this machine" % value)

    monkeypatch.setattr(SS, "protect_secret", _boom)
    env_file = tmp_path / ".env"
    env_file.write_text("MCP_UNLOCK_PASSWORD_PROTECTED=unreadable\n", encoding="utf-8")
    return str(env_file)


def test_a_protect_failure_does_not_carry_the_new_password_out(failing_protect):
    """THE DEFECT. protect_secret(fresh) raises with `fresh` in its message, that message
    becomes the reason string, and the reason string is printed."""
    import re

    result = EP.repair_unlock_password(failing_protect, {})
    reason = str(result.get("reason") or "")
    assert reason, "the diagnosis was dropped instead of redacted; that is the other failure"
    assert "cannot protect a new value here" in reason
    assert "<redacted:new unlock password>" in reason, reason
    assert not re.search(FRESH_RE, reason), (
        "a 16-hex-digit value survived in the reason string: %r" % reason)


def test_the_diagnosis_itself_survives_the_redaction(failing_protect):
    """Silencing the message would also clear the alert, and would cost the only account this
    path gives of why a machine cannot be repaired. What the exception SAID must still be there
    -- everything except the value."""
    result = EP.repair_unlock_password(failing_protect, {})
    reason = str(result["reason"])
    assert "DPAPI refused to protect" in reason
    assert "on this machine" in reason


def test_the_caller_redacts_what_comes_back_even_so(tmp_path):
    """Neither half is trusted alone. scripts/repair_unlock._scrub now takes the returned
    password as a known secret, redacted at any length -- the length floor is a guess about
    which of .env's values are worth removing, and a value we have been TOLD is the secret is
    not a guess."""
    import scripts.repair_unlock as R

    short = "ab"          # below the floor the blanket sweep uses, and deliberately so
    text = "it failed with %s in the middle" % short
    assert short not in R._scrub(text, {}, known_secrets=(short,))
    assert "<redacted:secret>" in R._scrub(text, {}, known_secrets=(short,))
    # ...and the floor still applies to the blanket sweep, which is guessing.
    assert short in R._scrub(text, {"SOME_VAR": short})


def test_a_missing_password_field_is_not_an_error_for_the_caller():
    """The noop paths return no `password` key at all. Passing None as a known secret must not
    redact the empty string -- which would replace every position in the message."""
    import scripts.repair_unlock as R

    assert R._scrub("plain text", {}, known_secrets=(None,)) == "plain text"
    assert R._scrub("plain text", {}, known_secrets=("",)) == "plain text"

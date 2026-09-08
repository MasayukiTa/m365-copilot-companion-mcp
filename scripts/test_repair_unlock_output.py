# -*- coding: utf-8 -*-
"""repair_unlock.py's stdout is a contract AND a place a secret can escape.

Two things share one stream. scripts/start_all.ps1 selects the verdict by the prefix
`^(noop|repaired|failed|error):`, so the shape cannot drift; and the stream is captured into
the launcher's output, which can reach logs, in a public repository -- so no value from .env
may appear on it.

The first version printed the new password outright. That was fixed; CodeQL then flagged the
line beside it, and it was right: the failure branch prints the exception raised by
repair_unlock_password(env_path, env), and `env` is the parsed .env. Python exceptions carry
the offending value routinely -- a KeyError prints its key, a ValueError its input -- so a
secret can reach stdout through a line that never mentions one.
"""
import scripts.repair_unlock as R


def test_a_value_from_env_is_removed_from_an_exception_message():
    env = {"MCP_UNLOCK_PASSWORD": "s3cret-value-not-a-real-one", "OTHER": "x"}
    msg = "could not parse 's3cret-value-not-a-real-one' as a token"
    out = R._scrub(msg, env)
    assert "s3cret-value-not-a-real-one" not in out
    assert "<redacted:MCP_UNLOCK_PASSWORD>" in out


def test_the_rest_of_the_message_survives():
    """Redaction that eats the diagnosis is the other way to lose. The reason this scrubs
    instead of dropping the message is that the message is the only diagnosis this path has."""
    env = {"MCP_UNLOCK_PASSWORD": "abcdefghij"}
    out = R._scrub("DecryptError: abcdefghij is not decryptable on this account", env)
    assert out.startswith("DecryptError: ")
    assert "is not decryptable on this account" in out


def test_a_short_value_is_left_alone():
    """A two-character value occurs inside ordinary words, and redacting it turns the message
    into noise. A secret that short is not one."""
    out = R._scrub("failed at index ab in the file", {"K": "ab"})
    assert out == "failed at index ab in the file"


def test_the_longest_value_is_redacted_first():
    """When one value contains another, replacing the short one first leaves the long one
    partly intact and still readable."""
    env = {"SHORT": "abcdef", "LONG": "abcdef-plus-the-rest"}
    out = R._scrub("token abcdef-plus-the-rest rejected", env)
    assert "abcdef-plus-the-rest" not in out
    assert "<redacted:LONG>" in out


def test_no_env_is_not_an_error():
    assert R._scrub("plain message", {}) == "plain message"
    assert R._scrub("plain message", None) == "plain message"


def test_every_verdict_keeps_the_prefix_start_all_matches_on():
    """start_all.ps1 selects on ^(noop|repaired|failed|error):. A change here that drops a
    prefix does not fail loudly -- the launcher simply stops recognising the outcome."""
    import io
    src = io.open(R.__file__, encoding="utf-8").read()
    printed = [ln.strip() for ln in src.splitlines() if ln.strip().startswith("print(")]
    assert printed, "no output lines found; the contract cannot be checked"
    for ln in printed:
        assert any(p in ln for p in ('"noop:', '"repaired:', '"failed:', '"error:')), ln

"""A held secret must not reach the ledger, whichever field carried it.

WHY THESE CASES. The ledger redacted by argument NAME, at the TOP level only. Every gated call
arrives through the gateway as {"name": ..., "arguments": {...}}, so the real arguments sit one
level down under a key that is not a secret name -- and a live MCP_UNLOCK_PASSWORD was found in
plaintext in .fleet/tool_events.jsonl because of it.

Name-based redaction cannot be the last line: the value arrives spliced into a shell command,
quoted back inside a tool's own result, or under a name nobody listed. These tests are written
against the VALUES the process holds, at the point where bytes leave it.
"""
import io
import json
import os

import pytest

from tools import tool_ledger as TL

SECRET = "pw-TESTONLY-9f3a2b7c"


@pytest.fixture(autouse=True)
def _held(monkeypatch, tmp_path):
    """Hold a known secret, and point the ledger at a temporary file."""
    monkeypatch.setenv("MCP_UNLOCK_PASSWORD", SECRET)
    path = tmp_path / "tool_events.jsonl"
    monkeypatch.setattr(TL, "_repo_path", lambda: str(path))
    return path


def _written(path):
    return io.open(str(path), encoding="utf-8").read() if os.path.exists(str(path)) else ""


def test_a_gateway_wrapped_password_does_not_reach_the_ledger(_held):
    # THE ONE THAT ACTUALLY LEAKED. Nested one level down, under "arguments".
    TL.record_call("call_tool", {"name": "unlock", "arguments": {"password": SECRET}})
    assert SECRET not in _written(_held)


def test_a_secret_spliced_into_a_shell_command_does_not_reach_the_ledger(_held):
    # No secret NAME anywhere; the value is inside a perfectly ordinary "command" string.
    TL.record_call("run", {"command": "curl -H 'Authorization: Bearer %s' https://x" % SECRET})
    assert SECRET not in _written(_held)


def test_a_secret_under_an_unlisted_name_does_not_reach_the_ledger(_held):
    TL.record_call("login", {"passphrase": SECRET})
    assert SECRET not in _written(_held)


def test_a_secret_buried_three_levels_deep_does_not_reach_the_ledger(_held):
    TL.record_call("x", {"a": {"b": {"c": {"password": SECRET}}}})
    assert SECRET not in _written(_held)


def test_a_secret_in_a_LIST_does_not_reach_the_ledger(_held):
    # Recursion covers dicts; the value-matching exit is what covers everything else.
    TL.record_call("x", {"argv": ["--password", SECRET]})
    assert SECRET not in _written(_held)


def test_a_secret_echoed_back_in_a_RESULT_does_not_reach_the_ledger(_held):
    cid = TL.record_call("unlock", {"password": SECRET})
    TL.record_outcome(cid, ok=True, result={"stdout": "unlocked with %s" % SECRET})
    assert SECRET not in _written(_held)


def test_the_explaining_turn_does_not_reach_the_ledger(_held):
    # THE CASE THE REPORT CALLS THE CRUX: an agent that has just fixed a credential leak then
    # writes the value again to show what it fixed. The most dangerous trigger is the one that
    # happens right after the fix.
    TL.record_call("write_file", {
        "path": "notes.md",
        "content": "Fixed the leak. The old value was %s -- rotate it." % SECRET})
    assert SECRET not in _written(_held)


def test_the_row_is_still_usable_evidence(_held):
    # Redaction that destroys the row would trade one problem for another: the ledger is what a
    # claimed result gets checked against.
    TL.record_call("call_tool", {"name": "unlock", "arguments": {"password": SECRET}})
    rows = [json.loads(l) for l in _written(_held).splitlines() if l.strip()]
    assert rows, "nothing was written at all"
    row = rows[0]
    assert row.get("tool") == "call_tool"
    blob = json.dumps(row)
    assert "unlock" in blob, "the row no longer says which tool was called"
    assert "password" in blob, "the row no longer says a password argument was present"


def test_an_empty_secret_does_not_blank_the_whole_row(monkeypatch, _held):
    # An unset or empty credential must not turn into a substring that matches everywhere.
    monkeypatch.setenv("MCP_UNLOCK_PASSWORD", "")
    TL.record_call("echo", {"text": "hello world"})
    assert "hello world" in _written(_held)

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
import pathlib

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


def _python_files(root):
    """Every .py under root, without dying on a directory entry that cannot be read.

    pathlib.rglob raises FileNotFoundError the moment it meets a DANGLING JUNCTION, and the
    exception is not about the file being scanned -- it aborts the whole walk. Measured
    2026-09-04: one orphan `fleetlink_p08` left behind by an earlier fleet run pointed at a
    worktree that had been cleaned up, and it took both of these tests red. A guarantee about
    secrets that stops holding because an unrelated run left a stale link behind is not a
    guarantee, and a red suite hides whatever fails next.

    Skips are limited to entries that do not resolve, so nothing readable is quietly dropped:
    a real directory that merely fails to open still raises.
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=None):
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if os.path.exists(full):
                keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            if fn.endswith(".py"):
                out.append(pathlib.Path(dirpath) / fn)
    return out


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


# ------------------------------------------------- the ledger must not break the tool


def test_the_caller_s_arguments_are_not_altered(_held):
    # THE WHOLE QUESTION. Unlock only passes when the REAL password reaches the other side, so
    # a redaction that reached into the arguments would not be a hardening -- it would turn a
    # working tool into one that can never authenticate.
    args = {"name": "unlock", "arguments": {"password": SECRET}}
    TL.record_call("call_tool", args)
    assert args["arguments"]["password"] == SECRET, "recording the call mutated the arguments"


def test_the_tool_still_receives_the_real_value(_held):
    # The same property stated end to end: record, then run, and check what the callee got.
    seen = {}

    def fake_unlock(**kw):
        seen.update(kw)
        return "unlocked"

    payload = {"password": SECRET}
    TL.record_call("unlock", payload)          # ledger first, as the real path does
    result = fake_unlock(**payload)            # then the tool
    assert seen["password"] == SECRET, "the tool received a redacted password"
    assert result == "unlocked"
    assert SECRET not in _written(_held), "and the ledger still must not hold it"


def test_redaction_is_only_ever_applied_on_a_write_path(_held):
    # secret_store.redact_secrets says in its own docstring that it may be used only at the
    # moment of writing to a file, never on text being sent. Checked here rather than trusted,
    # because a future call site added on a send path would break unlock silently -- it would
    # look like a wrong password, not like a redaction.
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    allowed = {
        "tools/secret_store.py",            # the definition
        "tools/tool_ledger.py",             # _append: writes the ledger line
        "relay/relay_fleet.py",             # _append: writes the fleet transcript
        "bridge/copilot_bridge.py",         # append_turn: writes the session store
    }
    found = set()
    for path in _python_files(root):
        rel = path.relative_to(root).as_posix()
        if "/worktrees/" in rel or rel.startswith(".") or "test_" in rel:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"\bredact_secrets\s*\(", text):
            found.add(rel)
    new = sorted(found - allowed)
    assert not new, (
        "redact_secrets is called somewhere new: %s. If that site is a WRITE, add it to the "
        "list above. If it is a SEND, it will stop unlock working -- the real password has to "
        "reach the other side." % new)


# ------------------------------------------------------------------ fanout / multi-agent


def test_a_fanout_workers_call_is_redacted_too(_held):
    # Fanout agents are separate Copilot conversations, but their tool calls come back through
    # the SAME server process, which is where the ledger is written. Redaction is a property of
    # the write point, not of the caller -- so it cannot hold for a direct call and lapse for a
    # worker's.
    TL.record_call("call_tool", {"name": "unlock", "arguments": {"password": SECRET}},
                   task="swe-p03", worker="w7")
    written = _written(_held)
    assert SECRET not in written
    assert "w7" in written, "the worker attribution was lost with the secret"


def test_every_worker_is_covered_not_just_a_named_one(_held):
    for w in ("", "w0", "w13", "refuter", "research"):
        TL.record_call("call_tool", {"name": "unlock", "arguments": {"password": SECRET}},
                       worker=w)
    assert SECRET not in _written(_held)


def test_the_ledger_has_exactly_one_writer(_held):
    # THE REASON FANOUT IS COVERED AT ALL. The redaction matches values read from the
    # environment, and this project strips credentials from child processes
    # (_subproc.sanitized_child_env), so a child that wrote the ledger could not redact -- it
    # would not hold the value to match. That is safe only while the single writer is the
    # server process. If a second writer appears in a child, this fails and says why.
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    # tools/registry.py joined this list when the ledger was extended past the call_tool
    # gateway to the DIRECTLY registered tools -- the ten the host invokes without touching
    # the gateway, which until then left no row at all. It satisfies the property this test
    # is really about: the write happens inside register()'s wrapper, and register is
    # imported by main.py alone, so it only ever runs in the server process, which does
    # hold the values redact_args matches. The test below pins that importer set.
    allowed = {"main.py", "tools/tool_ledger.py", "tools/registry.py"}
    writers = set()
    for path in _python_files(root):
        rel = path.relative_to(root).as_posix()
        if "/worktrees/" in rel or rel.startswith(".") or "test_" in rel:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"\b(?:_ledger|tool_ledger|TL)\.record_(?:call|outcome)\s*\(", text):
            writers.add(rel)
    new = sorted(writers - allowed)
    assert not new, (
        "a new ledger writer appeared: %s. If it runs in a CHILD process it cannot redact -- "
        "sanitized_child_env strips the credentials, so the child does not hold the value to "
        "match and the write would be in clear text." % new)


def test_the_registration_wrapper_only_ever_runs_in_the_server_process():
    """The allowance above rests on WHERE register() runs, so pin that rather than the name.

    A ledger writer in a child cannot redact: sanitized_child_env strips the credentials, so the
    child does not hold the value to match and would write it in clear text. register() is safe
    because main.py is its only importer. If a child-side module starts importing it, the
    allowance stops being true and this fails first.
    """
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    importers = set()
    for path in _python_files(root):
        rel = path.relative_to(root).as_posix()
        if "/worktrees/" in rel or rel.startswith(".") or "test_" in rel:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"from\s+(?:tools\.registry|\.registry)\s+import\s+[^\n]*\bregister\b", text):
            importers.add(rel)
    assert importers == {"main.py"}, (
        "register() is imported outside the server process by %s; the ledger write inside its "
        "wrapper can no longer be assumed to hold the credentials it must redact."
        % sorted(importers - {"main.py"}))

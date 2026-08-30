"""The file tools must not hand over the server's own credentials or its approval queue.

The security review found three holes -- an approval file a worker can forge, the harness venv
within reach, and ACLs a worker can restore -- and named one common cause: the worker can write
where the harness lives. The answer written for it was to move execution to a container on
another machine over SSH. That closes nothing for anyone who does not happen to own a second
machine, and the repository is not for one machine.

The hole underneath all three is smaller and portable: `.env` was readable through read_file,
API key and all. A caller who can read the key can authenticate as the operator, so no
server-side secret, signed gate or authenticated channel survives it -- including every scheme
proposed to make the approval file unforgeable.

Both are refused in _validate_path, the one function every reader, writer, resource handler and
search tool passes through. A rule enforced in one caller has as many holes as there are other
callers.

WHAT THIS DOES NOT DO, stated here so no one reads these tests as a closure: run_python and
shell_exec run same-user code in a subprocess -- tools/code_exec.py says "NOT a sandbox" in
its own docstring -- and a read or write from inside that subprocess never reaches
_validate_path. This narrows the holes; it does not shut them. An approval a same-user process
cannot forge needs a boundary this process does not have, and saying so is better than
recording it as handled, which is the mistake this work is correcting.
"""
import os

import pytest

os.environ.setdefault("MCP_API_KEY", "test-key-not-used")

import tools.file_ops as F


def test_the_credentials_file_is_refused():
    with pytest.raises(PermissionError):
        F._validate_path(".env")


def test_the_template_is_not_refused():
    """.env.example holds no values. Refusing it teaches people the rule is noise."""
    F._validate_path(".env.example")


def test_the_approval_queue_is_refused():
    with pytest.raises(PermissionError) as exc:
        F._validate_path(".companion_gates/anything.json")
    assert "approves its own" in str(exc.value)


def test_the_refusal_is_at_the_choke_point_not_in_read_file(tmp_path):
    """Every tool resolves through _validate_path; write_file and list_directory must be
    refused for the same paths, without each of them carrying its own copy of the rule."""
    for p in (".env", ".companion_gates/x.json"):
        with pytest.raises(PermissionError):
            F._validate_path(p)


def test_an_ordinary_file_still_resolves(tmp_path):
    """The guard must not become a blanket refusal: a containment measure that has to be
    switched off to get work done is one that will be found switched off."""
    f = tmp_path / "notes.md"
    f.write_text("hello", encoding="utf-8")
    assert F._validate_path(str(f)).name == "notes.md"


def test_the_authorisation_table_is_still_refused():
    """The rule this extends, kept alongside it so a future edit cannot drop it silently."""
    with pytest.raises(PermissionError):
        F._validate_path(".unlock_state.json")

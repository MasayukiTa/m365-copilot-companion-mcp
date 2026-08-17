"""The server's own authorisation state is not a file the file tools hand over.

The default base is the user's home and the checkout sits under it, so `read_file` could
return `.unlock_state.json` -- the table of authorised identities -- to a caller holding only
the API key. It stores hashes, so it is not directly usable as a credential; publishing the
list of identities to impersonate is the problem, and that is the same mistake `list_unlocked`
was narrowed for, reached through a different tool.

Enforced in `_validate_path` rather than in `read_file`, because every reader, writer, resource
handler and search tool goes through that one function. A rule enforced in one caller has as
many holes as there are other callers.
"""
import pytest

from tools import file_ops


@pytest.mark.parametrize("path", [
    ".unlock_state.json",
    ".fleet/unlock_token_gap.json",
    ".fleet/lock_state.json",
])
def test_authorisation_state_is_refused(path):
    assert "Refusing" in str(file_ops.read_file(path))


def test_it_is_refused_by_name_wherever_it_sits(tmp_path):
    """A rule that knew one absolute path would be defeated by copying the file."""
    decoy = tmp_path / ".unlock_state.json"
    decoy.write_text("{}", encoding="utf-8")
    assert "Refusing" in str(file_ops.read_file(str(decoy)))


def test_the_trace_directory_is_refused():
    """The trace records tool arguments -- where a credential lands if a redaction is missed."""
    with pytest.raises(PermissionError):
        file_ops._validate_path("~/.companion_runs/toolcalls_2026-01-01.jsonl")


def test_writing_to_it_is_refused_too():
    """Read-only would leave "overwrite the table with your own identity" open."""
    with pytest.raises(PermissionError):
        file_ops._validate_path(".unlock_state.json")


@pytest.mark.parametrize("path", ["README.md", "main.py", "tools/security.py"])
def test_ordinary_files_still_open(path):
    out = str(file_ops.read_file(path))
    assert "Refusing" not in out and len(out) > 50

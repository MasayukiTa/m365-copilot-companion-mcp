"""RULE 3's blanket "reading needs no unlock" must name its exception.

2026-09-24: clipboard_get, outlook_inbox, outlook_calendar, odbc_query, odbc_tables,
odbc_columns and registry_read moved behind require_unlocked() (see tools/clipboard_ops.py,
tools/outlook_ops.py, tools/odbc_ops.py, tools/registry_ops.py). An agent that still reads
RULE 3 as "reading never needs unlock" will call one of these expecting it to just work,
get refused, and -- per RULE 3's own instruction for a genuine write refusal -- either
retry pointlessly or abandon a task it could have finished by unlocking. The instructions
must name the exception, not just gate the code.

Reuses tools/test_instructions_do_not_accumulate_cases.py's own instructions_text() reader
(AST-parsed from main.py's instructions= keyword) rather than re-deriving a second way to
find the same string.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_instructions_do_not_accumulate_cases import instructions_text  # noqa: E402

GATED_READS = (
    "clipboard_get", "outlook_inbox", "outlook_calendar",
    "odbc_query", "odbc_tables", "odbc_columns", "registry_read",
)


def test_every_newly_gated_read_tool_is_named_in_the_instructions():
    text = instructions_text()
    missing = [name for name in GATED_READS if name not in text]
    assert not missing, (
        "the server instructions no longer name %s as needing unlock -- an agent reading "
        "only RULE 3/7's general claim ('reading needs no unlock') would call these "
        "expecting a plain read and be refused with no warning" % missing)


def test_the_exception_is_stated_as_gated_like_a_write():
    text = instructions_text()
    assert "gated like a write" in text or "gated exactly like a write" in text

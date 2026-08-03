"""Startup selection, delete navigation, and undo for the chat sidebar.

Three defects, all in how a conversation is chosen or removed:

* startup reopened `_all[0]` -- the first record read off disk, neither the newest
  nor the one last used -- so a months-old conversation stayed resident every launch;
* deleting the open conversation also jumped to `_all[0]`, throwing the user back to
  the top of the list on every deletion in a row;
* a plain click on the trash deleted the file immediately, with no confirmation and
  nothing to undo.

Source-level checks: CopilotChat.cs is C# and has no test harness here, matching
ui/test_fleet_cockpit_approval_center.py.

Run: pytest -q ui/test_copilot_chat_conv_lifecycle.py
"""
from pathlib import Path

SOURCE = Path(__file__).with_name("CopilotChat.cs").read_text(encoding="utf-8")


def test_startup_reopens_the_last_used_conversation():
    assert "string _lastOpenId" in SOURCE
    assert 'ln.StartsWith("last_open_conv=")' in SOURCE
    assert '{ "last_open_conv", _conv != null ? (_conv.Id ?? "") : "" },' in SOURCE


def test_startup_falls_back_to_the_newest_not_the_first_on_disk():
    body = SOURCE[SOURCE.index("Restore what was open last time"):]
    body = body[:body.index("ShowEmptyState")]
    assert "foreach (var c in _all) if (c.Ts > want.Ts) want = c;" in body


def test_delete_moves_to_the_neighbour():
    body = SOURCE[SOURCE.index("int was = _all.IndexOf(c);"):]
    body = body[:body.index("RefreshConvList();")]
    assert "OpenConversation(_all[next])" in body
    assert "OpenConversation(_all[0])" not in body


def test_single_click_delete_is_undoable_rather_than_immediate():
    assert "static readonly TimeSpan UndoWindow" in SOURCE
    assert "void CommitPendingDelete()" in SOURCE
    assert "void UndoPendingDelete()" in SOURCE
    assert "{ ExecuteDelete(cc, 1); ToastUndo(); }" in SOURCE


def test_the_file_is_only_removed_once_the_undo_window_closes():
    body = SOURCE[SOURCE.index("void CommitPendingDelete()"):]
    body = body[:body.index("void UndoPendingDelete()")]
    assert "File.Delete(p)" in body
    # DeleteLocal itself must no longer delete straight away.
    dl = SOURCE[SOURCE.index("void DeleteLocal(Conversation c)"):]
    dl = dl[:dl.index("RefreshConvList();")]
    assert "File.Delete" not in dl


def test_only_one_deletion_is_pending_at_a_time():
    dl = SOURCE[SOURCE.index("void DeleteLocal(Conversation c)"):]
    dl = dl[:dl.index("RefreshConvList();")]
    assert "CommitPendingDelete();" in dl

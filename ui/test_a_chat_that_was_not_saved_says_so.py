# -*- coding: utf-8 -*-
"""The chat window keeps the line the moment it is sent, and says so when a save fails.

BEFORE: SaveConversation was `try { ... } catch { }`, and it ran only when an ANSWER came back
through the bridge stream. A full disk or a locked folder lost the conversation without a word,
a user line sent into a conversation whose answer never arrived was never saved at all, and a
`persist_error` from the bridge (its session store refused the turn) had nowhere to be shown.

This reads ui/CopilotChat.cs as text -- the persist functions are small and a WPF window cannot
be constructed in a test -- and is limited to them. The bridge half runs for real in
bridge/test_a_chat_turn_is_always_stored.py.
"""
from __future__ import annotations

import io
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = io.open(os.path.join(REPO, "ui", "CopilotChat.cs"), encoding="utf-8-sig").read()


def _method(sig_regex):
    m = re.search(sig_regex, SRC)
    assert m, "not found: %s" % sig_regex
    i = SRC.index("{", m.end() - 1) if SRC[m.end() - 1] != "{" else m.end() - 1
    depth, j = 0, i
    while True:
        c = SRC[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return SRC[m.start():j + 1]
        j += 1


def test_save_conversation_reports_a_failure_instead_of_swallowing_it():
    body = _method(r"bool SaveConversation\(Conversation c\)\s*\{")
    assert not re.search(r"catch\s*(\(\s*Exception\s*\))?\s*\{\s*\}", body), \
        "SaveConversation swallows an exception again"
    assert "NoteNotSaved(" in body and "return false" in body and "return true" in body


def test_the_notice_is_visible_logged_and_in_both_languages():
    note = _method(r"void NoteNotSaved\(Conversation c, string reason\)\s*\{")
    assert "AddAssistant(T(\"not_saved\") + reason)" in note, "the failure is not drawn in the chat"
    assert "chat_save_errors.log" in note, "the failure is not logged"
    assert re.search(r'k == "not_saved"\)\s*return ja \? "この会話は保存されていません: "', SRC)
    assert "This conversation was not saved: " in SRC


def test_a_sent_line_is_saved_when_it_is_sent_not_when_an_answer_arrives():
    assert re.search(r"void IChatSendEffects\.AddUser\(string text\)\s*\{\s*AddUser\(text\);\s*"
                     r"PersistSentTurn\(\);\s*\}", SRC), "the send effect no longer persists the line"
    body = _method(r"void PersistSentTurn\(\)\s*\{")
    assert "_chatUserTurnsSent++" in body and "SaveConversation(c)" in body
    # a fleet conversation's durable record is the command file, not a .chat file of its own
    assert 'c.Source == "fleet"' in body


def test_the_stream_reads_the_bridges_persist_words():
    stream = _method(r"void Stream\(string msg, Conversation target\)\s*\{")
    assert 'ExtractField(jsonData, "persist_error")' in stream
    assert 'ExtractField(jsonData, "persist")' in stream
    assert 'NoteNotSaved(target, "bridge: " + persistErr)' in stream

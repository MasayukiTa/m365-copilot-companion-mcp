# -*- coding: utf-8 -*-
"""The run's current state was being appended to the transcript as a turn the agent had taken.

WHAT IT LOOKED LIKE. "状態: running　ターン 3/1000" plus the plan arrived at the end of the fleet
conversation as an assistant message, indistinguishable from something the agent said.

WHY THAT IS WRONG AND NOT MERELY UNTIDY. A status is a value that is true NOW; a message is a
record of a moment that has passed. Giving them one place makes the most volatile thing live in
the most permanent one -- so the transcript had to be rebuilt whenever the status moved, which is
every second while a run is live. That rebuild destroyed the reader's text selection (Ctrl+C
copied nothing) and made the view jump. The selection defect and the status-as-message defect
were the same defect.

WHAT AN EXTERNAL REVIEW SAID, asked before this was written and agreeing with the operator's own
reading: 「今の『ステータス末尾を一つの発言として追加する』方式はやめるべき。状態は更新される
現在値であり、発言はその時点で残された記録。更新の性質が違う」and 「『ターン 3/1000』は通常は
詳細情報へ下げる。ターン数は進捗率ではなく、読み手が知りたい『何が済み、何が残るか』を説明
できない」.

SO: one band above the transcript, updated in place, outside `_messages`. Writing it cannot touch
what the reader is looking at, and the transcript's signature no longer carries the status -- if
it did, every tick would differ and the guard that protects the selection would never hold.
"""
from __future__ import annotations

import io
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT = os.path.join(REPO, "ui", "CopilotChat.cs")


def _src():
    return io.open(CHAT, encoding="utf-8").read()


def _method(signature):
    src = _src()
    i = src.index(signature)
    j = src.find("\n    // ", i + 1)
    k = src.find("\n    void ", i + 1)
    ends = [x for x in (j, k) if x > 0]
    return src[i:min(ends)] if ends else src[i:]


def _code_only(text):
    return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("//"))


# ── the separation ────────────────────────────────────────────────────────────────────────

def test_the_status_is_not_appended_as_a_message():
    """THE DEFECT, in one assertion."""
    body = _code_only(_method("void RefreshFleetSnapshot()"))
    assert "AddAssistant(tailPre)" not in body and "AddAssistant(BuildFleetStatusTail" not in body, (
        "the run state is being appended to the transcript as an assistant turn again")


def test_the_status_goes_to_its_own_band():
    body = _code_only(_method("void RefreshFleetSnapshot()"))
    assert "ShowRunState(tailPre)" in body, "the state no longer reaches the band"


def test_the_band_lives_outside_the_message_panel():
    """If it were a child of _messages, clearing the transcript would clear it and updating it
    would disturb the transcript -- which is the arrangement being removed."""
    body = _code_only(_method("void ShowRunState(string text)"))
    assert "_messages" not in body, "the status band writes into the message panel"
    assert "_statusText.Text" in body and "_statusBand.Visibility" in body


def test_the_transcript_signature_excludes_the_status():
    """THE SUBTLE HALF. Signing the status alongside the messages would make every tick differ,
    the comparison would never hold, and the rebuild -- and the lost selection -- would come back
    while looking fixed.

    Anchored on the block that BUILDS the signature, not on one line of it: the signature became
    a per-turn list when appending was added, and pinning its old single-string spelling failed
    here about a change that made the property stronger."""
    body = _code_only(_method("void RefreshFleetSnapshot()"))
    block = body.split("var sigs = new List<string>();", 1)[1].split("int common", 1)[0]
    assert "tailPre" not in block, "the status is part of the transcript signature again"


def test_the_band_is_updated_before_any_early_return():
    """Most ticks change nothing and return early. If the band were written after that, the
    state would freeze on screen while the run moved -- the opposite defect, and quieter.

    THE RETURNS THAT MATTER ARE THE ONES AFTER THE STATE IS KNOWN. The method opens with guards
    for "no fleet view" and "no such worker", and there is nothing to show in those cases -- an
    earlier draft counted those too and failed on a correct method."""
    body = _code_only(_method("void RefreshFleetSnapshot()"))
    after_state = body.split("string tailPre = ", 1)[1]
    shown = after_state.index("ShowRunState(tailPre);")
    ret = after_state.index("return;")
    assert shown < ret, (
        "a tick can return before the band is written, leaving the state stale on screen "
        "while the run moves")


def test_leaving_the_fleet_view_clears_the_band():
    """A stale "running" line above a normal chat would be a lie about a run that is not there."""
    code = _code_only(_src())
    assert code.count("_activeFleetUrl = null; ShowRunState(null);") >= 3, (
        "some path out of the fleet view leaves the status band showing")

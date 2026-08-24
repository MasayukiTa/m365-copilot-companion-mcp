"""A refusal that ends the turn ends the proposal. It should end somebody's inbox instead.

frozen.py refuses to re-sign the files that define the delegation, and that refusal is right.
What followed was not: the agent reported it, the turn closed, and unless the operator
remembered, the proposal was gone. Two were nearly lost in one day -- an impossible
instruction in tools/security.py, and this CLI's own output not saying how to undo a
re-signing.

Nothing here adds enforcement, and the code says so: the queue runs in the same privilege
domain as the thing that fills it. What it buys is that "stopped" becomes "waiting on
somebody".

Run: pytest -q relay/selfimprove/test_frozen_queue_refused.py
"""
import inspect

import pytest

from relay.selfimprove import frozen as F
from relay.selfimprove import pending as P


class _Args(object):
    def __init__(self, reason="", authorization=""):
        self.reason = reason
        self.authorization = authorization


@pytest.fixture(autouse=True)
def queue(tmp_path, monkeypatch):
    """Never the live queue, and never the live ledger."""
    monkeypatch.setattr(P, "QUEUE_PATH", str(tmp_path / "pending.jsonl"))
    monkeypatch.setattr(P, "_notify", lambda *a, **k: None)
    return tmp_path / "pending.jsonl"


def test_a_refused_proposal_is_queued_with_the_files_it_touched():
    F._queue_refused(["tools/security.py"], _Args(reason="reword the impossible instruction"))
    got = P.items()
    assert len(got) == 1
    assert got[0]["files"] == ["tools/security.py"]
    assert got[0]["reason"] == "reword the impossible instruction"


def test_the_entry_carries_a_command_that_completes_the_act():
    F._queue_refused(["tools/security.py"], _Args(reason="reword it"))
    cmd = P.items()[0]["command"]
    assert "--snapshot --force" in cmd
    assert "reword it" in cmd
    assert "<your words>" in cmd, "the authorisation is the operator's to fill in"


def test_a_refusal_with_no_reason_queues_nothing():
    """--force already refuses without a reason; an entry saying only "something was refused"
    is a reminder, and reminders are what this replaces."""
    F._queue_refused(["tools/security.py"], _Args(reason="  "))
    assert P.items() == []


def test_it_does_not_write_an_argument_on_the_callers_behalf():
    """An entry that read like a case made by the thing asking for permission is worse than no
    entry: the operator has to be able to tell a proposal from an advocate."""
    body = inspect.getsource(F._queue_refused)
    assert "detail=" not in body


def test_queueing_can_never_turn_a_refusal_into_a_crash(monkeypatch):
    """The refusal is the part that matters."""
    import relay.selfimprove.pending as _P
    monkeypatch.setattr(_P, "add", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    F._queue_refused(["tools/security.py"], _Args(reason="x"))     # must not raise


def test_the_refusal_branch_actually_calls_it():
    src = inspect.getsource(F._main)
    branch = src[src.index("REFUSED: this re-signing touches"):]
    branch = branch[:branch.index("return 2")]
    assert "_queue_refused(excluded, args)" in branch


# ── the way back, printed where the reader already is ───────────────────────────────

def test_the_undo_hint_names_the_command_and_what_it_does_not_undo():
    hint = F._undo_hint()
    assert "--revoke" in hint
    assert "not the code" in hint


def test_the_hint_is_printed_on_a_successful_re_signing():
    src = inspect.getsource(F._main)
    # The property is that it is printed on the success path, not that it is printed within
    # some number of characters: a 600-char window turned "another line was added above it"
    # into a failure of an unrelated invariant.
    after = src[src.index('print("snapshot written: %s" % args.baseline)'):]
    after = after[:after.index("return 0")]
    assert "print(_undo_hint())" in after


def test_the_hint_is_printed_when_the_delegation_refuses():
    src = inspect.getsource(F._main)
    branch = src[src.index("REFUSED: this re-signing touches"):]
    branch = branch[:branch.index("return 2")]
    assert "_undo_hint()" in branch


# ── and the other end: an approved proposal that has been carried out ───────────────

class _ArgsFull(object):
    def __init__(self, reason):
        self.reason = reason
        self.authorization = ""


def test_a_successful_re_signing_closes_the_approved_card():
    """An approved proposal stays on the dashboard as "waiting on the agent" until somebody
    says the work is done, and nobody was saying it -- the same mismatch as "I approved it and
    the screen did not move", one transition further along."""
    pid = P.add(["tools/security.py"], "reword the impossible instruction")
    P.resolve(pid, authorization="承認する。この提案のまま実施してよい。",
              status=P.APPROVED, kind="preset")
    assert P.items()[0]["status"] == P.APPROVED

    F._resolve_pending_for(["tools/security.py"], _ArgsFull("reword the impossible instruction"))
    assert P.items() == []
    assert P.items(include_resolved=True)[0]["status"] == P.DONE


def test_an_open_card_is_not_closed_by_a_re_signing():
    """It has not been decided. Closing it would be this process answering for the operator."""
    P.add(["tools/security.py"], "reword it")
    F._resolve_pending_for(["tools/security.py"], _ArgsFull("reword it"))
    assert P.items()[0]["status"] == P.OPEN


def test_a_reworded_reason_leaves_the_card_open_rather_than_closing_another():
    """The safe direction. A stale "waiting" is a visible nuisance; a wrongly-closed card is
    a lie about a decision having been acted on."""
    pid = P.add(["tools/security.py"], "the original wording")
    P.resolve(pid, authorization="ok", status=P.APPROVED)
    F._resolve_pending_for(["tools/security.py"], _ArgsFull("a different wording"))
    assert P.items()[0]["status"] == P.APPROVED


def test_a_different_file_does_not_close_it():
    pid = P.add(["tools/security.py"], "same words")
    P.resolve(pid, authorization="ok", status=P.APPROVED)
    F._resolve_pending_for(["docs/SECURITY.md"], _ArgsFull("same words"))
    assert P.items()[0]["status"] == P.APPROVED


def test_closing_can_never_break_a_re_signing(monkeypatch):
    """It runs after the baseline has been written. An exception here would report failure for
    work that had already succeeded."""
    import relay.selfimprove.pending as _P
    monkeypatch.setattr(_P, "status_of", lambda pid: (_ for _ in ()).throw(OSError("gone")))
    F._resolve_pending_for(["tools/security.py"], _ArgsFull("x"))      # must not raise


def test_the_success_path_actually_calls_it():
    import inspect
    src = inspect.getsource(F._main)
    after = src[src.index('print("snapshot written: %s" % args.baseline)'):]
    assert "_resolve_pending_for(excluded, args)" in after[:600]

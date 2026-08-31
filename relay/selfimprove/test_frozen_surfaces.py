# -*- coding: utf-8 -*-
"""A changed frozen set has to appear where decisions are made, not only where it is checked.

THE EVIDENCE IS THE OPERATOR'S OWN QUESTION: "凍結セットの再署名、これどこなんだろと探して
しまう。実際どこなの、承認必要なskillsほど目立たないので." They went looking and could not
find it. That is the whole finding -- a state this serious cannot be reachable only by a
command somebody has to already know.

WHAT IT WAS. docs/SECURITY.md was edited on 2026-08-31 and the self-improvement loop refused to
run for the rest of the day. That fact existed in exactly three places: three failing tests, a
decision object inside a run nobody was watching, and `--verify`. It was eventually found while
investigating test failures that looked like a broken record-writer -- which is to say, by
accident.

Skill approvals appear on the dashboard. A dead constitution did not. These tests hold the
correction: the mismatch is queued as a decision, with the command to accept it and the command
to revert instead, and it arrives through the loop's own abort path rather than only through a
CLI.
"""
import pytest

from relay.selfimprove import frozen as F
from relay.selfimprove import pending


@pytest.fixture(autouse=True)
def _own_queue(tmp_path, monkeypatch):
    """A queue of this test's own. The repo-wide fixture already redirects the real one; this
    also keeps two tests here from reading each other's rows."""
    monkeypatch.setattr(pending, "QUEUE_PATH", str(tmp_path / "pending.jsonl"), raising=False)


def test_a_mismatch_becomes_a_decision_someone_can_find():
    pid = F.queue_mismatch(["docs/SECURITY.md"])
    assert pid, "nothing was queued"
    rows = pending.items()
    assert len(rows) == 1
    assert "docs/SECURITY.md" in str(rows[0])


def test_the_entry_says_how_to_accept_and_how_to_refuse():
    """A queue entry that does not say how to act on it is a reminder, and reminders are what
    this queue replaces. Both directions, because reverting is the right answer about as often
    as re-signing is."""
    F.queue_mismatch(["docs/SECURITY.md"])
    row = str(pending.items()[0])
    assert "--snapshot --force" in row, "no way to accept"
    assert "--authorization" in row, "accepting without recording the instruction"
    assert "git checkout --" in row, "no way to refuse"
    assert "--verify" in row, "no way to look first"


def test_the_operators_words_are_left_blank():
    """The authorization is the thing being recorded. A queue that pre-fills it is recording
    itself, and the ledger entry would then quote the machine."""
    F.queue_mismatch(["docs/SECURITY.md"])
    row = str(pending.items()[0])
    assert "<your words, verbatim>" in row


def test_the_reason_names_both_outcomes_rather_than_advocating():
    """The proposal must not read like a case made by the thing asking for permission."""
    F.queue_mismatch(["relay/selfimprove/guards.py"])
    reason = pending.items()[0].get("reason", "")
    assert "re-signed" in reason and "reverted" in reason


def test_asking_twice_does_not_queue_twice():
    """The loop checks on every run. A live proposal re-queued each time is a queue nobody
    reads."""
    a = F.queue_mismatch(["docs/SECURITY.md"])
    b = F.queue_mismatch(["docs/SECURITY.md"])
    assert a == b
    assert len(pending.items()) == 1


def test_a_different_file_is_a_different_decision():
    F.queue_mismatch(["docs/SECURITY.md"])
    F.queue_mismatch(["tools/security.py"])
    assert len(pending.items()) == 2


def test_nothing_changed_queues_nothing():
    assert F.queue_mismatch([]) == ""
    assert F.queue_mismatch(None) == ""
    assert pending.items() == []


def test_it_never_raises_even_when_the_queue_is_broken(monkeypatch):
    """It runs inside an abort path. A queue failure must not turn a clean refusal into a
    traceback -- the refusal is the part that matters."""
    def _boom(*_a, **_k):
        raise OSError("queue unwritable")
    monkeypatch.setattr(pending, "add", _boom)
    assert F.queue_mismatch(["docs/SECURITY.md"]) == ""


def test_the_loop_queues_it_on_its_own_abort(monkeypatch, tmp_path):
    """THE PATH THAT MATTERS. --verify is for somebody who already suspects; the loop aborting
    is what actually happens, unattended, and it is where the operator was never told."""
    from relay.selfimprove.controller import EvolutionController
    from relay.selfimprove.ledger import HypothesisLedger

    seen = {}
    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (False, ["docs/SECURITY.md"]))
    monkeypatch.setattr(F, "queue_mismatch",
                        lambda changed, *a, **k: seen.setdefault("changed", list(changed)) or "p1")

    ctl = EvolutionController(ledger=HypothesisLedger(str(tmp_path / "h.jsonl")))
    ok, changed = ctl._frozen()
    assert ok is False
    assert seen.get("changed") == ["docs/SECURITY.md"], "the abort did not reach the queue"


def test_a_test_run_with_its_own_baseline_does_not_post_to_the_operators_queue(monkeypatch, tmp_path):
    """Every wiring test in this repository runs the controller. If those posted, the operator's
    queue would fill with rows from CI -- the same defect as tests writing to live records,
    which this repository has now paid for five times."""
    from relay.selfimprove.controller import EvolutionController
    from relay.selfimprove.ledger import HypothesisLedger

    called = []
    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (False, ["docs/SECURITY.md"]))
    monkeypatch.setattr(F, "queue_mismatch", lambda *a, **k: called.append(1) or "p1")

    ctl = EvolutionController(ledger=HypothesisLedger(str(tmp_path / "h.jsonl")),
                              baseline_path=str(tmp_path / "baseline.json"))
    ctl._frozen()
    assert not called, "a test-owned baseline still posted to the operator's queue"


# --- the contract with the screen ---------------------------------------------------------

def test_the_row_tells_the_screen_it_can_be_carried_out():
    """`action` is what turns "approve" from a note into the act. Without it the card is the
    one the operator described: it says a decision is waiting and hands the work back."""
    F.queue_mismatch(["docs/SECURITY.md"])
    assert pending.items()[0].get("action") == "frozen_resign"


def test_the_command_still_identifies_the_row_for_older_entries():
    """SelfImproveDashboard recognises a re-signing by `action`, and falls back to this
    substring so a row queued before the field existed is still operable. The operator has one
    of those on screen right now; if recognition depended only on the new field, that card
    would stay as inert as it was -- which is the complaint, not the fix."""
    F.queue_mismatch(["docs/SECURITY.md"])
    assert "frozen --snapshot --force" in pending.items()[0].get("command", "")


def test_an_ordinary_proposal_is_not_marked_actionable():
    """Only proposals this screen can actually perform may claim to be performable. Everything
    else keeps the old meaning of approval, which is right for work only a person can do."""
    pending.add(["some/file.py"], "an ordinary proposal", command="do the thing by hand")
    rows = [r for r in pending.items() if "ordinary" in (r.get("reason") or "")]
    assert rows and not rows[0].get("action")


# --- the card has to explain itself ---------------------------------------------------------

def test_the_card_says_what_the_frozen_set_is():
    """THE OPERATOR'S OBJECTION, WHICH IS A SAFETY ONE: "frozenの件、これ何がしたいものなのか
    見てもわからない。… でないと99%のユーザはまあいいやでrm-rfなどを通す." A decision surface
    that names a path and a command, and assumes the reader knows the codebase, is one people
    click through."""
    F.queue_mismatch(["tools/security.py"])
    d = pending.items()[0]["detail"]
    assert "凍結セット" in d and "書き換えてはいけない" in d


def test_it_says_what_the_changed_file_governs():
    """A path is not information. What the file DECIDES is."""
    F.queue_mismatch(["tools/security.py"])
    d = pending.items()[0]["detail"]
    assert "unlock" in d and "権限" in d


def test_it_says_what_each_button_does():
    """Approve and reject must both be described, and the approve text must say the reader is
    signing off on content they may not have read."""
    F.queue_mismatch(["docs/SECURITY.md"])
    d = pending.items()[0]["detail"]
    assert "承認する" in d and "却下する" in d
    assert "確認していないなら、押さないでください" in d
    assert "台帳" in d


def test_it_shows_the_actual_change():
    """Without the diff the reader is agreeing to a filename."""
    F.queue_mismatch(["relay/selfimprove/frozen.py"])
    d = pending.items()[0]["detail"]
    assert "差分" in d or "git checkout" in d


def test_every_frozen_file_has_a_role_written_down():
    """A path with no explanation is the case this whole change is about, and the manifest grows
    -- so a new entry with no role fails here rather than reaching a card as a bare filename."""
    missing = [f for f in F.FROZEN_MANIFEST if f not in F.FILE_ROLE]
    assert not missing, "no role text for: %s" % missing


def test_the_explanation_survives_a_repo_without_git(monkeypatch):
    """The diff is best effort. Losing it must not lose the explanation."""
    monkeypatch.setattr(F, "_diff_stat", lambda *a, **k: "")
    F.queue_mismatch(["docs/SECURITY.md"])
    d = pending.items()[0]["detail"]
    assert "凍結セット" in d and "承認する" in d

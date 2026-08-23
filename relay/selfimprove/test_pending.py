"""A refused proposal must outlive the turn that produced it.

frozen.py refuses to re-sign the files that define the delegation, and that refusal is
correct. What followed was not: the agent reported it, the turn ended, and unless the operator
remembered, the proposal was gone. Two were nearly lost in a single day -- a message in
tools/security.py instructing an impossible action, and a missing undo hint in the frozen
CLI's own output.

These tests cover the queue that turns "stopped" into "queued". They deliberately do not
assert anything about enforcement: everything here runs in the same privilege domain as the
agent, so the queue can be written, read and emptied by the process that fills it. What is
worth pinning is that a proposal survives, that retrying does not bury it, and that consuming
one leaves a record.

Run: pytest -q relay/selfimprove/test_pending.py
"""
import pytest

import relay.selfimprove.pending as P

#: Captured at import, before the autouse fixture redirects it at a temp file. The published
#: -or-not question is about the real destination, not the one the tests write to.
REAL_QUEUE = P.QUEUE_PATH

FILES = ["tools/security.py"]
REASON = "the refusal instructs an action that cannot be performed"


@pytest.fixture(autouse=True)
def queue(tmp_path, monkeypatch):
    """Never the live queue. Sibling suites have wiped live records three times."""
    monkeypatch.setattr(P, "QUEUE_PATH", str(tmp_path / "pending.jsonl"))
    monkeypatch.setattr(P, "_notify", lambda *a, **k: None)
    return tmp_path / "pending.jsonl"


def test_a_proposal_survives_the_turn():
    pid = P.add(FILES, REASON)
    assert pid
    assert [i["id"] for i in P.items()] == [pid]


def test_retrying_the_same_proposal_does_not_queue_it_twice(queue):
    """A refused re-signing is usually retried, and a row per attempt buries the thing the
    queue exists to surface.

    Checked on the FILE, not only on items(): the reader collapses rows by id, so a version
    that appended one row per attempt still showed a single entry and passed -- while the
    append-only record filled with duplicates of the same decision."""
    a = P.add(FILES, REASON)
    b = P.add(FILES, REASON)
    assert a == b
    assert len(P.items()) == 1
    lines = [l for l in queue.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1, "a retry must not append a second row"


def test_a_different_change_to_the_same_file_is_a_different_decision():
    P.add(FILES, REASON)
    P.add(FILES, "print the undo command in the CLI output")
    assert len(P.items()) == 2


def test_the_same_change_to_different_files_is_a_different_decision():
    P.add(FILES, REASON)
    P.add(["docs/SECURITY.md"], REASON)
    assert len(P.items()) == 2


def test_an_entry_says_how_to_act_on_it():
    """A queue entry that does not say what to run is a reminder, and reminders are what this
    replaces."""
    pid = P.add(FILES, REASON, command="python -m relay.selfimprove.frozen --snapshot --force ...")
    item = P.items()[0]
    assert item["id"] == pid
    assert "frozen --snapshot" in item["command"]


def test_resolving_removes_it_from_the_open_list_but_not_from_the_record():
    pid = P.add(FILES, REASON)
    assert P.resolve(pid, authorization="やっていい") is True
    assert P.items() == []
    kept = P.items(include_resolved=True)
    assert len(kept) == 1
    assert kept[0]["status"] == P.DONE
    assert kept[0]["authorization"] == "やっていい"


def test_dropping_is_recorded_as_a_decision_too():
    """"We are not doing this" is an answer, and losing it means being asked again."""
    pid = P.add(FILES, REASON)
    P.resolve(pid, authorization="やらない", status=P.DROPPED)
    assert P.items() == []
    assert P.items(include_resolved=True)[0]["status"] == P.DROPPED


def test_resolving_something_that_was_never_queued_fails_visibly():
    assert P.resolve("deadbeef", authorization="x") is False


def test_the_queue_is_append_only(queue):
    pid = P.add(FILES, REASON)
    P.resolve(pid, authorization="ok")
    lines = [l for l in queue.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2, "resolving must append, not rewrite -- a queue that can be edited " \
                            "in place cannot say what was in it yesterday"


def test_a_missing_queue_is_an_empty_list_not_an_error():
    assert P.items() == []
    assert P.status_of("nope") == ""


def test_a_torn_line_does_not_hide_the_rest(queue):
    pid = P.add(FILES, REASON)
    with open(queue, "a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"event": "queued", "id": "xx", "rea')
    assert [i["id"] for i in P.items()] == [pid]


def test_adding_never_raises(monkeypatch):
    """It is called from a refusal path. A queue that throws would turn a clean refusal into
    a crash, and the refusal is the part that matters."""
    monkeypatch.setattr(P, "_append", lambda rec: (_ for _ in ()).throw(OSError("disk full")))
    assert P.add(FILES, REASON) == ""


def test_the_stored_authorization_is_not_claimed_to_be_verified():
    """Nothing here can verify it, and the module must not imply otherwise."""
    import io as _io
    src = _io.open(P.__file__, encoding="utf-8").read()
    assert "is NOT verified" in src
    assert "does not add enforcement" in src


def test_the_queue_is_not_published():
    """Entries quote proposed diffs to the files the delegation excludes."""
    import subprocess
    r = subprocess.run(["git", "check-ignore", "-q", REAL_QUEUE])
    assert r.returncode == 0, REAL_QUEUE

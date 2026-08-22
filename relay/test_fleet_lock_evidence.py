"""The fleet's lock reader, asked about a window of refusals rather than about one.

_looks_locked's record branch used to read a one-deep slot, so the refusal it judged by was
whichever landed last anywhere in the process. The direction nothing could test while the
evidence was one record deep: a context-less refusal arriving AFTER a genuine one overwrites
the slot, the reader filters it as "not about me", and the turn is called unlocked while a
real lock stands -- an answer produced under a lock nobody noticed, which is the failure the
whole filter exists to avoid.

Run: pytest -q relay/test_fleet_lock_evidence.py
"""
import json

import pytest

import relay.relay_fleet as RF
import tools.lock_state as LS

REAL = "[locked client IP: '203.0.113.7'] Mutating and execution tools require an unlock."
NO_CTX = RF.NO_CONTEXT_REFUSAL + " Denied: this call ran in-process."


@pytest.fixture
def log(tmp_path, monkeypatch):
    p = tmp_path / "refusals.jsonl"
    monkeypatch.setattr(LS, "_LOG_FILE", p)
    monkeypatch.setattr(LS, "_STATE_FILE", tmp_path / "state.json")
    return p


def _write(path, rows):
    with open(path, "a", encoding="utf-8", newline=chr(10)) as fh:
        for ts, detail in rows:
            fh.write(json.dumps({"event": "refused", "ts": ts,
                                 "client_ip": "", "detail": detail, "site": "x"}) + chr(10))


def _resp():
    """The PARAPHRASED reply, which is what actually reaches this branch.

    The marker branch above short-circuits on any reply that still contains the bracket the
    server emits, so a test that uses one never reaches the record branch at all -- an early
    version of this file did exactly that and passed while the branch under test was dead.
    The injected operator discipline makes the agent restate the error in its own words, which
    is the case the record fallback exists for.
    """
    return "unlock パスワード欠如で確定。STUCK: unlock パスワード未提供。"


def test_a_genuine_refusal_hidden_behind_a_later_context_less_one_still_counts(log, monkeypatch):
    """THE CASE THE SLOT COULD NOT REPRESENT."""
    _write(log, [(100.0, REAL), (101.0, NO_CTX)])
    monkeypatch.setattr(RF.time, "time", lambda: 102.0)
    assert RF._looks_locked(_resp(), since=99.0) is True


def test_only_context_less_refusals_still_means_not_this_worker(log, monkeypatch):
    """The filter must not be weakened into "any refusal counts" -- that was the original
    defect, where a concurrent relay's in-process refusals injected unlock into turns that
    were never locked."""
    _write(log, [(100.0, NO_CTX), (101.0, NO_CTX)])
    monkeypatch.setattr(RF.time, "time", lambda: 102.0)
    assert RF._looks_locked(_resp(), since=99.0) is False


def test_a_genuine_refusal_arriving_last_still_counts(log, monkeypatch):
    """The order that the old slot happened to get right, kept right."""
    _write(log, [(100.0, NO_CTX), (101.0, REAL)])
    monkeypatch.setattr(RF.time, "time", lambda: 102.0)
    assert RF._looks_locked(_resp(), since=99.0) is True


def test_no_refusal_in_the_window_is_not_a_lock(log, monkeypatch):
    _write(log, [(50.0, REAL)])
    monkeypatch.setattr(RF.time, "time", lambda: 102.0)
    assert RF._looks_locked(_resp(), since=99.0) is False


def test_a_long_reply_is_never_a_lock_however_many_refusals_stand(log, monkeypatch):
    """The dominance rule sits above the record check: a security review that quotes the
    error is not a locked turn, and this is the incident that produced that rule."""
    _write(log, [(100.0, REAL)])
    monkeypatch.setattr(RF.time, "time", lambda: 102.0)
    long_review = _resp() + " " + ("analysis. " * 200)
    assert len(long_review) >= RF.LOCKED_DOMINANCE_MAX_CHARS
    assert RF._looks_locked(long_review, since=99.0) is False


def test_no_test_in_this_file_can_reach_the_real_records():
    src = open(__file__, encoding="utf-8").read()
    live = "." + "fleet"
    assert live not in src


def test_the_note_names_the_refusal_that_decided_it(log, monkeypatch):
    """_note_locked exists because an incident could not be reconstructed: nothing recorded
    which refusal the fallback had read. Naming whichever record arrived last, rather than the
    one the branch actually acted on, reintroduces exactly that hole -- the note would point at
    a context-less refusal that was explicitly ruled out as evidence."""
    _write(log, [(100.0, REAL), (101.0, NO_CTX)])
    monkeypatch.setattr(RF.time, "time", lambda: 102.0)

    seen = {}

    def spy(branch, *, resp_len, since, consumed=None):
        seen["branch"] = branch
        seen["consumed"] = consumed

    monkeypatch.setattr(LS, "record_classification", spy)
    assert RF._looks_locked(_resp(), since=99.0) is True
    assert seen["branch"] == "fallback"
    assert seen["consumed"]["detail"] == REAL, \
        "the note must point at the refusal the branch acted on, not the last one to arrive"

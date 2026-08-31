# -*- coding: utf-8 -*-
"""The record of what a tool was asked to do, and what came back.

WHY IT HAD TO EXIST. The session store holds 122 MB and 15,605 turns and records NOT ONE tool
call -- 26.8 MB of user text, 4.8 MB of assistant text, and nothing else. So the refuter that
exists to catch a wrong DONE reads the worker's own account of what it did, and DONE precision
is 0.718: 11 of 39 claims wrong on a 40-instance slice. Every question of the form "did it
actually do that" was unanswerable, by construction.

The rule these tests hold: A CLAIM AND THE RECORD OF THE ACT MUST COME FROM DIFFERENT PLACES.
Assistant prose is not evidence, because a worker that is mistaken -- or lying -- writes both.
"""
import json
import os

import pytest

from tools import tool_ledger as L


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    path = str(tmp_path / "tool_events.jsonl")
    monkeypatch.setattr(L, "LEDGER_PATH", path, raising=False)
    return path


def rows(path):
    return [json.loads(l) for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]


# ── two records, not one ──────────────────────────────────────────────────────────────────

def test_a_call_is_recorded_before_it_runs(ledger):
    """THE POINT OF WRITING TWICE. A ledger written only on completion records exactly the runs
    that did not need recording."""
    cid = L.record_call("read_file", {"path": "x.txt"}, task="t1")
    assert rows(ledger) == [r for r in rows(ledger) if r["event"] == "call"]
    assert rows(ledger)[0]["id"] == cid
    L.record_outcome(cid, ok=True, result="contents")
    evs = [r["event"] for r in rows(ledger)]
    assert evs == ["call", "outcome"]


def test_a_call_that_never_returns_is_visible_as_an_orphan(ledger):
    """A crash, a timeout or a killed process leaves this. It is a finding -- the tool was
    entered and never came back -- and no single-record scheme can show it."""
    L.record_call("shell_exec", {"command": "sleep 999"}, task="t1")
    good = L.record_call("read_file", {"path": "a"}, task="t1")
    L.record_outcome(good, ok=True, result="ok")
    orph = L.orphans()
    assert len(orph) == 1 and orph[0]["tool"] == "shell_exec"


def test_an_error_is_an_outcome_not_a_missing_record(ledger):
    cid = L.record_call("odbc_query", {"sql": "select 1"})
    L.record_outcome(cid, ok=False, error="OperationalError: no such table")
    r = [x for x in rows(ledger) if x["event"] == "outcome"][0]
    assert r["ok"] is False and "no such table" in r["error"]
    assert not L.orphans()


# ── what must never be written ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["password", "unlock_token", "api_key", "Authorization"])
def test_secret_values_never_reach_the_file(ledger, key):
    """The ledger is a file that outlives the session. A password written once is written
    forever. The NAME stays -- "there was a password argument" is evidence; the value is not."""
    L.record_call("unlock", {key: "hunter2-the-real-secret", "other": "fine"})
    text = open(ledger, encoding="utf-8").read()
    assert "hunter2-the-real-secret" not in text
    assert key in text, "the argument's presence is itself evidence and must be kept"
    assert '"redacted": true' in text.lower()


def test_a_huge_result_is_bounded_but_still_identifiable(ledger):
    """Disk is the binding constraint on this machine and has already stopped a run. A result
    is truncated, and its digest keeps a later claim about it checkable."""
    cid = L.record_call("read_file", {"path": "big"})
    L.record_outcome(cid, ok=True, result="A" * 50000)
    r = [x for x in rows(ledger) if x["event"] == "outcome"][0]
    assert r["result"]["truncated"] is True
    assert r["result"]["len"] == 50000
    assert len(r["result"]["text"]) <= L.MAX_INLINE
    assert len(r["result"]["sha16"]) == 16
    assert os.path.getsize(ledger) < 20000


def test_the_digest_distinguishes_two_different_big_results(ledger):
    """Truncation must not make two different results look the same, or the record cannot
    contradict a claim about which one came back."""
    a = L.record_call("read_file", {"path": "1"}); L.record_outcome(a, ok=True, result="A" * 9000)
    b = L.record_call("read_file", {"path": "2"}); L.record_outcome(b, ok=True, result="B" * 9000)
    outs = [x for x in rows(ledger) if x["event"] == "outcome"]
    assert outs[0]["result"]["sha16"] != outs[1]["result"]["sha16"]


# ── reading it back ───────────────────────────────────────────────────────────────────────

def test_a_task_can_be_asked_what_it_actually_did(ledger):
    """This is what a verifier reads INSTEAD of the worker's account of itself."""
    a = L.record_call("write_file", {"path": "src/x.py"}, task="job1")
    L.record_outcome(a, ok=True, result="written")
    b = L.record_call("shell_exec", {"command": "pytest -x"}, task="job1")
    L.record_outcome(b, ok=False, error="1 failed")
    L.record_call("read_file", {"path": "z"}, task="OTHER")

    got = L.for_task("job1")
    assert [g["call"]["tool"] for g in got] == ["write_file", "shell_exec"]
    assert got[0]["outcome"]["ok"] is True
    assert got[1]["outcome"]["ok"] is False


def test_a_torn_line_does_not_make_the_ledger_unreadable(ledger):
    """A ledger that cannot be read because one line is torn is a ledger that stops being
    consulted, which is the same as not having one."""
    cid = L.record_call("read_file", {"path": "a"})
    L.record_outcome(cid, ok=True, result="ok")
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write('{"event": "call", "id": "trunc\n')
    assert len(L.read()) == 2


def test_writing_never_raises_even_when_the_path_is_impossible(monkeypatch):
    """It sits on the hot path of every tool call. A tool must not fail over bookkeeping."""
    monkeypatch.setattr(L, "LEDGER_PATH", "Z:/definitely/not/a/place/x.jsonl", raising=False)
    cid = L.record_call("read_file", {"path": "a"})
    L.record_outcome(cid, ok=True, result="ok")
    assert cid


def test_ids_are_unique():
    assert len({L.new_call_id() for _ in range(500)}) == 500


# ── the wiring ────────────────────────────────────────────────────────────────────────────

def test_the_gateway_records_every_dispatched_call():
    """SOURCE-LEVEL, and stated as such: the gateway lives in main.py behind an import that
    starts a server, so this asserts the wiring is present rather than exercising it. The
    behaviour above is tested for real; this catches the wiring being removed."""
    import io
    import os as _os
    repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    src = io.open(_os.path.join(repo, "main.py"), encoding="utf-8").read()
    body = src[src.index("def call_tool("):]
    assert "tool_ledger" in body, "the gateway no longer records tool calls"
    assert body.index("record_call") < body.index("_out = fn(**_args)"), \
        "the call is recorded AFTER the tool runs; an orphan would then be invisible"
    assert body.count("record_outcome") >= 2, "an error path does not record its outcome"

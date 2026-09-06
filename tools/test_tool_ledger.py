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


# ── attribution, which the first real batch proved was missing ────────────────────────────

def test_calls_are_found_by_where_they_operated(ledger):
    """THE `task` FIELD IS USUALLY EMPTY. The gateway records task=_args.get("_task") and
    nothing passes _task -- a fleet worker is a Copilot agent calling a tool; it does not know
    which benchmark instance it is. Measured on the first real batch: every call landed with an
    empty task, for_task returned nothing, and the whole assessment came back UNVERIFIABLE.

    What every call DOES carry is the path it worked on, and the benchmark already maps an
    instance to its worktree."""
    root = "C:/w/.fleet/swe/work/p01_abc"
    a = L.record_call("write_file", {"path": root + "/src/user.js"})
    L.record_outcome(a, ok=True, result="written")
    b = L.record_call("shell_exec", {"command": "npm test", "working_dir": root})
    L.record_outcome(b, ok=True, result="pass")
    L.record_call("read_file", {"path": "C:/w/.fleet/swe/work/p99_other/x.js"})

    got = L.for_task("", root=root)
    assert [g["call"]["tool"] for g in got] == ["write_file", "shell_exec"]
    assert got[0]["outcome"]["ok"] is True


def test_another_instances_work_is_not_attributed_here(ledger):
    """Two worktrees under the same parent. Matching loosely would credit one instance with
    another's work, which is worse than no attribution."""
    L.record_call("write_file", {"path": "C:/w/work/p02_xyz/a.js"})
    assert L.for_task("", root="C:/w/work/p01_abc") == []


def test_backslashes_and_case_do_not_defeat_it(ledger):
    """Windows hands the same directory back in several spellings, and a path comparison that
    only works for one of them works by luck."""
    cid = L.record_call("write_file", {"path": r"C:\W\Work\P01_ABC\src\a.js"})
    L.record_outcome(cid, ok=True, result="ok")
    assert len(L.for_task("", root="c:/w/work/p01_abc")) == 1


def test_an_explicit_task_still_wins(ledger):
    """Where a producer does set it, the field is authoritative -- path matching is the
    fallback for the callers that cannot."""
    cid = L.record_call("write_file", {"path": "/elsewhere/a.py"}, task="job-7")
    L.record_outcome(cid, ok=True, result="ok")
    assert len(L.for_task("job-7")) == 1


def test_no_root_and_no_task_returns_everything_rather_than_guessing(ledger):
    L.record_call("read_file", {"path": "a"})
    L.record_call("read_file", {"path": "b"})
    assert len(L.for_task("")) == 2


# --------------------------------------------------------------------------------------
# A REFUSAL IS NOT A SUCCESS.
#
# A gated tool that is denied returns its refusal normally, so every call site passed
# ok=True and the row was indistinguishable from one where the write happened. Measured
# on the live ledger 2026-09-06: write_file and run_python refused for a missing unlock
# token, filed ok=True, error="". Nothing branches on this field -- it is what later
# measurements are taken over, and two success rates computed from it have already been
# retracted.
# --------------------------------------------------------------------------------------

REFUSAL = ("[locked: no valid unlock token for '203.0.113.7'] The identity in the "
           "forwarding header is not sufficient on its own. Call unlock(password="
           "'<password>') and pass the returned `unlock_token` with the call.")


def test_a_refused_call_is_not_recorded_as_a_success(ledger):
    cid = L.record_call("write_file", {"path": "x"})
    L.record_outcome(cid, ok=True, result=REFUSAL)
    out = [r for r in rows(ledger) if r["event"] == "outcome"][0]
    assert out["ok"] is False
    assert out["error"] == "refused (locked)"
    # The refusal itself is still stored verbatim; only the verdict changed.
    assert REFUSAL[:20] in out["result"]["text"]


def test_the_other_refusal_wording_is_caught_too(ledger):
    # security.py emits two shapes: the token one above and this IP one.
    cid = L.record_call("run_python", {"code": "1"})
    L.record_outcome(cid, ok=True,
                     result="[locked client IP: '203.0.113.7'] Mutating and execution "
                            "tools require an unlock. Call unlock(password='<password>').")
    assert [r for r in rows(ledger) if r["event"] == "outcome"][0]["ok"] is False


def test_content_that_merely_quotes_a_refusal_is_still_a_success(ledger):
    # read_file on a source file that documents the refusal must not be filed as refused.
    # This is why the rule is startswith + length, not a substring test.
    body = ("# -*- coding: utf-8 -*-\n"
            "MARKER = \"[locked: no valid unlock token\"  # what security.py emits\n"
            + "# padding so this is plainly a file, not a short error.\n" * 12)
    cid = L.record_call("read_file", {"path": "security.py"})
    L.record_outcome(cid, ok=True, result=body)
    assert [r for r in rows(ledger) if r["event"] == "outcome"][0]["ok"] is True


def test_a_long_analysis_beginning_with_the_marker_is_not_a_refusal(ledger):
    # Dominance: a genuine refusal IS the whole short return value.
    cid = L.record_call("run_python", {"code": "1"})
    L.record_outcome(cid, ok=True, result="[locked" + " and here is why: " * 40)
    assert [r for r in rows(ledger) if r["event"] == "outcome"][0]["ok"] is True


def test_an_explicit_failure_is_never_upgraded(ledger):
    cid = L.record_call("write_file", {"path": "x"})
    L.record_outcome(cid, ok=False, result=REFUSAL, error="boom")
    out = [r for r in rows(ledger) if r["event"] == "outcome"][0]
    assert out["ok"] is False
    assert out["error"] == "boom"     # the real reason is not overwritten


def test_a_non_string_result_never_raises(ledger):
    cid = L.record_call("glob", {"pattern": "*"})
    L.record_outcome(cid, ok=True, result={"matches": ["[locked"]})
    assert [r for r in rows(ledger) if r["event"] == "outcome"][0]["ok"] is True


def test_the_copied_literals_still_match_the_modules_that_own_them():
    """The prefix and the dominance bound are duplicated, so pin them to their sources.

    tools/security.py is frozen and relay/relay_fleet.py is too heavy to import from a module
    main.py loads first, so tool_ledger keeps its own copies. A copy is only as good as the
    literal staying identical.
    """
    from relay import relay_fleet

    assert L._LOCK_REFUSAL_MAX_CHARS == relay_fleet.LOCKED_DOMINANCE_MAX_CHARS
    # Every marker relay_fleet matches on must start with the prefix this module keys off.
    for marker in relay_fleet.LOCKED_MARKERS:
        assert marker.lower().lstrip("[").startswith(
            L._LOCK_REFUSAL_PREFIX.lstrip("[").lower()[:6])

    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(L.__file__))),
                            "tools", "security.py"), encoding="utf-8").read()
    # The two refusal strings security.py actually emits both open with the prefix.
    assert '"[locked: no valid unlock token' in src or "[locked: no valid unlock token" in src
    assert "[locked client IP:" in src

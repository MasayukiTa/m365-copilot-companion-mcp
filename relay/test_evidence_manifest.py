# -*- coding: utf-8 -*-
"""Checking a DONE against the record instead of against itself.

A DONE is accepted today because the worker said so: precision 0.718, 11 of 39 claims wrong on
a 40-instance slice. The refuter meant to catch those reads the worker's own account, because
until the tool ledger existed there was nothing else to read.

These tests fix the four questions this can answer with no model call, and -- more importantly
-- the ones it must refuse to answer. UNVERIFIABLE is not a pass. "We could not check" and "we
checked and it was fine" are different answers, and collapsing them is how a verifier becomes
decoration.
"""
import pytest

from relay import evidence_manifest as EM


def call(tool, ts, ok=True, command=None, task="t1"):
    args = {}
    if command is not None:
        args["command"] = {"text": command}
    return {"call": {"tool": tool, "ts": ts, "task": task, "args": args},
            "outcome": {"ok": ok, "ts": ts + 1}}


CONTRACT = {"checks": [{"id": "c1", "command": "pytest -x", "expect": "exit_zero"}]}


# ── the claim the records support ─────────────────────────────────────────────────────────

def test_a_real_completion_is_supported():
    events = [call("write_file", 100), call("shell_exec", 200, command="pytest -x")]
    v = EM.assess(True, CONTRACT, events)
    assert v["verdict"] == EM.SUPPORTED
    assert v["evidence"]["check_runs_passed_after_last_write"] == 1


def test_narrowing_the_test_to_the_relevant_file_still_counts():
    """A contract saying `pytest -x` is satisfied by running it on the file that changed.
    Narrowing is ordinary and correct; refusing it would train people to run the slow thing."""
    events = [call("write_file", 100),
              call("shell_exec", 200, command="pytest -x tests/test_retry.py")]
    assert EM.assess(True, CONTRACT, events)["verdict"] == EM.SUPPORTED


# ── the claims the records contradict ─────────────────────────────────────────────────────

def test_a_done_with_no_write_and_no_way_to_write_is_contradicted():
    """The commonest wrong claim: finished, having changed nothing.

    The example is deliberately read-only. There is no route here by which the workspace could
    have changed, so the negative is one the record can actually support.
    """
    events = [call("read_file", 100), call("grep", 200)]
    v = EM.assess(True, CONTRACT, events)
    assert v["verdict"] == EM.CONTRADICTED
    assert "nothing in the workspace changed" in " ".join(v["reasons"])


def test_a_write_made_through_an_exec_tool_is_not_reported_as_no_write():
    """This assertion was changed, and the old one was false.

    It used to require CONTRADICTED for read_file + shell_exec("pytest -x"). But shell_exec can
    write, and no list of tool NAMES distinguishes `pytest -x` from `sed -i`. Measured: nine
    instances were judged "nothing in the workspace changed"; every one had exec calls, and four
    of them produced a patch that graded RESOLVED. The workspace had changed and the ledger
    could not see how.

    UNVERIFIABLE is not success. It is the verdict for "the record cannot settle it", which is
    the true state, and it is why step 4 reruns the commands itself instead of reading what the
    worker chose to report.
    """
    events = [call("read_file", 100), call("shell_exec", 200, command="pytest -x")]
    v = EM.assess(True, CONTRACT, events)
    assert v["verdict"] == EM.UNVERIFIABLE
    assert "cannot see a write made that way" in " ".join(v["reasons"])


def test_edit_and_verify_counts_as_a_write():
    """It is a write tool and it was not in the list. Seventeen calls in the ledger, none of
    them counted, while the instances using it were told nothing had changed. A hand-written
    allow-list goes stale the moment a tool is added."""
    assert "edit_and_verify" in EM.WRITE_TOOLS


def test_a_test_run_before_the_last_edit_does_not_count():
    """THE ORDERING CHECK, and the reason the contract carries its own clock. A green from
    before the change says nothing about the change -- and nobody has to be dishonest for this
    to happen, only out of order."""
    events = [call("shell_exec", 100, command="pytest -x"), call("write_file", 200)]
    v = EM.assess(True, CONTRACT, events)
    assert v["verdict"] == EM.CONTRADICTED
    assert "before the last write" in " ".join(v["reasons"]).lower()


def test_a_failing_acceptance_run_is_contradicted():
    events = [call("write_file", 100), call("shell_exec", 200, command="pytest -x", ok=False)]
    assert EM.assess(True, CONTRACT, events)["verdict"] == EM.CONTRADICTED


def test_never_running_the_acceptance_command_is_contradicted():
    events = [call("write_file", 100), call("shell_exec", 200, command="echo done")]
    v = EM.assess(True, CONTRACT, events)
    assert v["verdict"] == EM.CONTRADICTED
    assert "never run" in " ".join(v["reasons"])


def test_a_command_that_only_mentions_the_binary_does_not_count():
    """`echo pytest` is not running pytest, and `--collect-only` runs no test."""
    for bogus in ("echo pytest -x", "pytest --collect-only", "cat pytest -x",
                  "echo 'ran pytest -x and it passed'"):
        events = [call("write_file", 100), call("shell_exec", 200, command=bogus)]
        assert EM.assess(True, CONTRACT, events)["verdict"] == EM.CONTRADICTED, bogus


# ── the claims it must refuse to settle ───────────────────────────────────────────────────

def test_no_tool_calls_at_all_is_unverifiable_not_contradicted():
    """Either the worker did nothing or the ledger was not writing. Those are different, and
    reporting the second as a lie would be its own kind of wrong."""
    v = EM.assess(True, CONTRACT, [])
    assert v["verdict"] == EM.UNVERIFIABLE
    assert "ledger was not writing" in " ".join(v["reasons"])


def test_a_task_with_no_mechanical_check_is_unverifiable_not_supported():
    """A summary or an investigation cannot be settled by running something. The work may be
    perfectly good; the records simply cannot say."""
    events = [call("write_file", 100)]
    v = EM.assess(True, {"checks": []}, events)
    assert v["verdict"] == EM.UNVERIFIABLE
    assert "no mechanical check" in " ".join(v["reasons"])


def test_a_missing_contract_reads_differently_from_an_unverifiable_one():
    """The distinction acceptance_contract exists to keep: an omission at admission is not a
    fact about the task."""
    events = [call("write_file", 100)]
    absent = " ".join(EM.assess(True, None, events)["reasons"])
    stated = " ".join(EM.assess(True, {"checks": []}, events)["reasons"])
    assert "no acceptance contract was recorded" in absent
    assert "contract records no mechanical check" in stated


def test_no_claim_is_not_assessed():
    assert EM.assess(False, CONTRACT, [call("write_file", 100)])["verdict"] == EM.UNVERIFIABLE


# ── it must not raise, and must not flatter ───────────────────────────────────────────────

@pytest.mark.parametrize("junk", [None, [{"call": None}], [{}], "not a list"])
def test_malformed_input_is_unverifiable_rather_than_an_exception(junk):
    v = EM.assess(True, CONTRACT, junk)
    assert v["verdict"] in (EM.UNVERIFIABLE, EM.CONTRADICTED)


def test_unverifiable_is_never_counted_as_supported():
    s = EM.summarise([{"verdict": EM.UNVERIFIABLE}] * 5 + [{"verdict": EM.SUPPORTED}])
    assert s[EM.SUPPORTED] == 1 and s["total"] == 6
    assert abs(s["supported_share"] - 1 / 6) < 1e-9


def test_the_summary_does_not_call_itself_precision():
    """Precision needs the external grade. This is the share of claims the records could
    support, which is weaker -- and naming it precision would be the same overclaim the whole
    pipeline exists to stop."""
    s = EM.summarise([{"verdict": EM.SUPPORTED}])
    assert "precision" not in " ".join(s.keys())
    assert "supported_share" in s


# -- narrowing the target -------------------------------------------------------------------

def test_a_narrowed_package_selector_is_still_the_acceptance_command():
    """The defect that made every go instance read as untested.

    _matches_check is documented as loose on the tail: `pytest -x` is satisfied by
    `pytest -x tests/test_retry.py`, because narrowing pytest APPENDS a token. Narrowing go
    REPLACES one -- the contract's target IS `./...` -- so requiring that literal token rejected
    every narrowed run. These two commands were taken from the ledger; both were reported as
    "the contract's acceptance command was never run".
    """
    assert EM._matches_check("go test ./models/... ./scan/... ./report/...", "go test ./...")
    assert EM._matches_check("go test ./config/... -count=1 -v -run TestLoad", "go test ./...")
    assert EM._matches_check("go build ./... && go test ./models/...", "go test ./...")


def test_narrowing_the_target_is_not_dropping_it():
    """A selector must be answered by a selector. `go test` with no target is a different act
    from `go test ./...`, and accepting it would turn the loosening into a hole."""
    assert not EM._matches_check("go test", "go test ./...")


def test_a_different_subcommand_is_not_the_acceptance_command():
    """What keeps the loosening honest. Only path-shaped tokens are matched by kind; `test` is
    an ordinary required token, so building is not testing however the paths line up."""
    assert not EM._matches_check("go build ./...", "go test ./...")
    assert not EM._matches_check("go vet ./internal/...", "go test ./...")


def test_the_pytest_cases_are_unchanged():
    """The behaviour this loosening must not disturb."""
    assert EM._matches_check("pytest -x tests/test_retry.py", "pytest -x")
    assert not EM._matches_check("pytest --collect-only", "pytest -x")
    assert not EM._matches_check("echo go test ./...", "go test ./...")
    assert EM._matches_check("npm test", "npm test")
    assert not EM._matches_check("npm run lint", "npm test")


def test_a_flag_is_never_treated_as_a_target():
    """_is_selector must not accept something that only looks path-ish because it is a flag,
    or a required flag could be satisfied by an unrelated path."""
    assert not EM._is_selector("--dir=./x")
    assert not EM._is_selector("-run")
    assert EM._is_selector("./...")
    assert EM._is_selector("./internal/config/...")
    assert not EM._is_selector("test")

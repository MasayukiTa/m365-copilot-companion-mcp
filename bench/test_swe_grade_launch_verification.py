# -*- coding: utf-8 -*-
"""A launch that never took must not come back as a graded verdict.

THE DEFECT THIS PINS, measured 2026-09-09. `systemd-run --no-block` legitimately returns empty
stdout on success, so `_ssh_ps` cannot tell a clean detached launch from a connection that was
silently degraded (the eval host's sshd has a very low MaxStartups and this call follows two scp
calls in quick succession). Three consecutive real runs returned from the launch call looking
exactly like success while /tmp/gb_<runid> was never created and the eval never ran -- and the
poll loop then found a stray, empty, valid-looking .batchresult.json and wrote EVALERR rows for
every instance. An infra fault was recorded as a graded outcome, which is the one thing a
benchmark ledger must never do: loop.py reads zero rows as INFRA_ABORT and a row of EVALERR as
a measurement.

These tests drive the REAL main() with the transport injected, so they exercise the retry loop
and the abort path rather than asserting that some source line exists. Nothing here touches the
network or the eval host.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

BENCH = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BENCH)
sys.path.insert(0, os.path.dirname(BENCH))

import swe_check_remote as R          # noqa: E402
import swe_grade_swebench as G        # noqa: E402


#: THE PRE-LAUNCH PROBE HAS TO BE ANSWERED, or main() returns before the launch loop and every
#: assertion below reads "verified 0 time(s)". swe_grade_swebench asks whether the eval host's
#: interpreter can import swebench before it spends a launch on it (added after these tests were
#: written, which is exactly how they went red in CI while passing here: the fake transport
#: answered "" to the new question and the early return looked like a launch that never
#: happened).
def _grader_present(cmd):
    return "import swebench" in cmd


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """main() with every remote call replaced, and a real prediction on disk to grade."""
    preds = tmp_path / "preds"
    preds.mkdir()
    with io.open(str(preds / "some__repo-1.json"), "w", encoding="utf-8") as fh:
        json.dump([{"model_patch": "diff --git a/x b/x\n"}], fh)

    calls = {"launch": 0, "marker": 0, "scp_from": 0}

    monkeypatch.setattr(R, "_scp", lambda *a, **k: True)
    monkeypatch.setattr(R, "_ssh_ps", lambda *a, **k: "")
    # No real waiting: the retry pause and the poll interval are both time.sleep in main().
    monkeypatch.setattr(G.time, "sleep", lambda *_a, **_k: None)

    def _scp_from(*_a, **_k):
        calls["scp_from"] += 1
        return False
    monkeypatch.setattr(R, "_scp_from", _scp_from)

    monkeypatch.setattr(G, "TMP", str(tmp_path / "_grade_batch"))
    return {"preds": str(preds), "results": str(tmp_path / "grade_results.jsonl"),
            "calls": calls, "monkeypatch": monkeypatch}


def _run(wired, argv_extra=()):
    argv = ["swe_grade_swebench.py", "--preds-dir", wired["preds"],
            "--results", wired["results"], "--run-id", "testrun",
            "--max-wait-min", "1", "--poll-s", "1"] + list(argv_extra)
    wired["monkeypatch"].setattr(sys, "argv", argv)
    G.main()


def _rows(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in io.open(path, encoding="utf-8") if l.strip()]


def test_a_launch_that_never_created_the_workdir_is_retried_and_then_abandoned(wired):
    """The workdir is the run's own first act, so its absence is the only honest signal."""
    calls = wired["calls"]

    def _wsl_token(cmd, *a, **k):
        if _grader_present(cmd):
            return "Y"
        if "test -d" in cmd:
            calls["marker"] += 1
            return "N"          # the launch never took, every time
        return ""
    wired["monkeypatch"].setattr(R, "_wsl_token", _wsl_token)

    _run(wired)

    assert calls["marker"] == 3, (
        "the launch was verified %d time(s); it must be retried up to 3 before giving up"
        % calls["marker"])
    assert calls["scp_from"] == 0, (
        "it polled for a result after a launch that never happened -- that poll is how a "
        "stray .batchresult.json became a false EVALERR")
    assert _rows(wired["results"]) == [], (
        "an infra fault was written into the grade ledger as a verdict: %r"
        % (_rows(wired["results"]),))


def _serve_a_result(wired, resolved=("some__repo-1",)):
    """Make the result fetch succeed, so the poll loop ends on its first tick.

    NOT COSMETIC: time.sleep is a no-op in these tests, so a poll that never finds anything
    spins on wallclock until --max-wait-min really elapses. Serving the result is also what the
    assertion wants -- that a confirmed launch is followed through, not merely attempted.
    """
    calls = wired["calls"]

    def _scp_from(remote, local, *a, **k):
        calls["scp_from"] += 1
        payload = ("DONE\n" if remote.endswith(".done")
                   else json.dumps({"resolved": list(resolved), "unresolved": [],
                                    "error": [], "empty": [], "report": "r.json"}))
        with io.open(local, "w", encoding="utf-8") as fh:
            fh.write(payload)
        return True
    wired["monkeypatch"].setattr(R, "_scp_from", _scp_from)


def test_a_launch_that_takes_on_the_second_attempt_goes_on_to_poll(wired):
    """The retry must not be a disguised abort: a launch that comes up late is a real run."""
    calls = wired["calls"]

    def _wsl_token(cmd, *a, **k):
        if _grader_present(cmd):
            return "Y"
        if "test -d" in cmd:
            calls["marker"] += 1
            return "N" if calls["marker"] < 2 else "Y"
        return ""
    wired["monkeypatch"].setattr(R, "_wsl_token", _wsl_token)
    _serve_a_result(wired)

    _run(wired)

    assert calls["marker"] == 2, "it did not stop verifying once the workdir appeared"
    assert calls["scp_from"] > 0, "a confirmed launch was never polled for its result"
    assert [r["verdict"] for r in _rows(wired["results"])] == ["RESOLVED"], (
        "the late-but-real launch lost its verdict")


def test_a_confirmed_run_still_records_the_verdict_it_was_given(wired):
    """The abort path must not have cost the normal path its output."""
    calls = wired["calls"]

    def _wsl_token(cmd, *a, **k):
        if _grader_present(cmd):
            return "Y"
        if "test -d" in cmd:
            calls["marker"] += 1
            return "Y"
        return ""
    wired["monkeypatch"].setattr(R, "_wsl_token", _wsl_token)
    _serve_a_result(wired)

    _run(wired)

    assert calls["marker"] == 1, "a launch confirmed first time was verified twice"
    rows = _rows(wired["results"])
    assert [r["verdict"] for r in rows] == ["RESOLVED"], (
        "a real graded result did not reach the ledger: %r" % (rows,))


def test_a_missing_grader_is_named_and_costs_no_launch(wired):
    """THE CHECK THAT WOULD HAVE CAUGHT THE ABOVE GOING RED. The pre-launch probe was added
    without a test of its own, so the only thing that noticed it was three unrelated
    assertions turning into "verified 0 time(s)" in CI. This pins the probe's own contract:
    when the eval host's interpreter cannot import swebench, nothing is launched, nothing is
    polled, and -- above all -- no verdict row is written, because a missing grader is an
    eval-host setup fault and not a graded outcome."""
    calls = wired["calls"]

    def _wsl_token(cmd, *a, **k):
        if _grader_present(cmd):
            return "N"
        if "test -d" in cmd:
            calls["marker"] += 1
            return "Y"
        return ""
    wired["monkeypatch"].setattr(R, "_wsl_token", _wsl_token)

    _run(wired)

    assert calls["marker"] == 0, "it went on to launch with no grader on the far end"
    assert calls["scp_from"] == 0, "it polled for a result it never asked for"
    assert _rows(wired["results"]) == [], (
        "a setup fault was written into the grade ledger as a verdict: %r"
        % (_rows(wired["results"]),))

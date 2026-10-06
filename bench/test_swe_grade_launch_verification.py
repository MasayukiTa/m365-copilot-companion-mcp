# -*- coding: utf-8 -*-
"""Grading must run inside a held session, and an infra fault must never become a verdict.

THE HISTORY THIS PINS. Three days of A/B grades came back EVALERR. Five separate causes were
removed before the grader itself was proven correct, and the sixth had been producing the
symptom all along: the batch was handed to `systemd-run --no-block`, and on this eval host
detached work does not survive. Measured 2026-09-10:

  * the identical command run synchronously inside the invoking session finished in 107s and
    returned a real verdict (resolved=1);
  * handed to a transient unit it was STOPPED after 44s and 51s -- the journal says
    "Stopping ... Deactivated successfully", with no error, no OOM and no timeout -- and dmesg
    shows journald flushing its runtime journal, i.e. the distro's systemd being
    re-initialised once no wsl.exe session held it;
  * `setsid nohup` died the same way;
  * touching the distro every 20 seconds did NOT rescue it (two arms, held and unheld, both
    stopped at ~2 heartbeats), because a new session brings up a new systemd rather than
    re-adopting the previous one's units.

So the shape of the call IS the fix, and these tests hold that shape. They also hold the other
half: a run that produces nothing writes NOTHING to the ledger, because loop.py reads zero rows
as INFRA_ABORT and a row of EVALERR as a measurement -- and that difference decides whether a
fresh slice gets burned.

These drive the real main() with the transport injected. Nothing here touches the network.
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


def _is_grader_probe(cmd):
    """The pre-run probe main() asks before spending a run on the eval host."""
    return "import swebench" in cmd


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """main() with every remote call replaced, and a real prediction on disk to grade."""
    preds = tmp_path / "preds"
    preds.mkdir()
    with io.open(str(preds / "some__repo-1.json"), "w", encoding="utf-8") as fh:
        json.dump([{"model_patch": "diff --git a/x b/x\n"}], fh)

    calls = {"ssh": [], "scp_from": [], "probe": 0}

    monkeypatch.setattr(R, "_scp", lambda *a, **k: True)
    monkeypatch.setattr(G.time, "sleep", lambda *_a, **_k: None)

    def _ssh_ps(script, *a, **k):
        calls["ssh"].append(script)
        return ""
    monkeypatch.setattr(R, "_ssh_ps", _ssh_ps)

    def _wsl_token(cmd, *a, **k):
        if _is_grader_probe(cmd):
            calls["probe"] += 1
            return "Y"
        return ""
    monkeypatch.setattr(R, "_wsl_token", _wsl_token)

    monkeypatch.setattr(G, "TMP", str(tmp_path / "_grade_batch"))
    return {"preds": str(preds), "results": str(tmp_path / "grade_results.jsonl"),
            "calls": calls, "monkeypatch": monkeypatch}


def _run(wired, argv_extra=()):
    argv = ["swe_grade_swebench.py", "--preds-dir", wired["preds"],
            "--results", wired["results"], "--run-id", "testrun",
            "--max-wait-min", "2"] + list(argv_extra)
    wired["monkeypatch"].setattr(sys, "argv", argv)
    G.main()


def _rows(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in io.open(path, encoding="utf-8") if l.strip()]


def _serve(wired, payload, done=True):
    """Make the result fetch behave: `done` controls whether the marker is there at all."""
    calls = wired["calls"]

    def _scp_from(remote, local, *a, **k):
        calls["scp_from"].append(remote)
        if remote.endswith(".done"):
            if not done:
                return False
            body = "DONE\n"
        elif remote.endswith(".batchresult.json"):
            body = json.dumps(payload)
        else:
            body = "remote log contents"
        with io.open(local, "w", encoding="utf-8") as fh:
            fh.write(body)
        return True
    wired["monkeypatch"].setattr(R, "_scp_from", _scp_from)


# -- the shape of the call ----------------------------------------------------------------------

def test_the_batch_is_not_detached(wired):
    """THE CAUSE THAT COST THREE DAYS. Detached work is stopped on this host within a minute,
    having produced nothing, and that failure is indistinguishable from a graded miss."""
    _serve(wired, {"resolved": ["some__repo-1"], "unresolved": [], "error": [], "empty": []})
    _run(wired)
    sent = "\n".join(wired["calls"]["ssh"])
    assert "systemd-run" not in sent, "the batch was detached again; it will be stopped mid-run"
    assert "--no-block" not in sent, "the batch was detached again"
    assert "Wait-Job" in sent, "the session is not held for the duration of the run"


def test_the_hold_is_at_least_as_long_as_the_caller_asked_for(wired):
    """--max-wait-min is the ceiling on the work, so it must also be the ceiling on the wait:
    a hold shorter than the work turns a slow grade into a silent infra abort."""
    _serve(wired, {"resolved": [], "unresolved": ["some__repo-1"], "error": [], "empty": []})
    _run(wired, ["--max-wait-min", "7"])
    sent = "\n".join(wired["calls"]["ssh"])
    assert "-Timeout 420" in sent, sent[-400:]


def test_the_runner_log_goes_somewhere_that_survives(wired):
    """/tmp on this host is cleaned within minutes -- measured, the workdir was gone while the
    run was still being polled -- so a log written there cannot explain a failure afterwards."""
    _serve(wired, {"resolved": ["some__repo-1"], "unresolved": [], "error": [], "empty": []})
    _run(wired)
    sent = "\n".join(wired["calls"]["ssh"])
    assert "/mnt/c/wsl-setup/testrun.log" in sent, sent[-400:]


# -- what reaches the ledger --------------------------------------------------------------------

def test_a_real_verdict_reaches_the_ledger(wired):
    _serve(wired, {"resolved": ["some__repo-1"], "unresolved": [], "error": [], "empty": []})
    _run(wired)
    assert [r["verdict"] for r in _rows(wired["results"])] == ["RESOLVED"]


def test_a_run_that_produced_nothing_writes_no_row(wired):
    _serve(wired, {}, done=False)
    _run(wired)
    assert _rows(wired["results"]) == [], (
        "an infra fault was written into the grade ledger as a verdict: %r"
        % (_rows(wired["results"]),))


def test_a_run_that_produced_nothing_fetches_the_remote_log(wired):
    """Saying "no result" without saying why is what made five causes take days each."""
    _serve(wired, {}, done=False)
    _run(wired)
    assert any(r.endswith(".log") for r in wired["calls"]["scp_from"]), (
        "it gave up without reading the log it had just written: %r"
        % (wired["calls"]["scp_from"],))


def test_a_missing_grader_is_named_and_costs_no_run(wired):
    """The probe's own contract: no grader on the far end means nothing is run, nothing is
    fetched, and above all no verdict row is written."""
    calls = wired["calls"]

    def _wsl_token(cmd, *a, **k):
        if _is_grader_probe(cmd):
            calls["probe"] += 1
            return "N"
        return ""
    wired["monkeypatch"].setattr(R, "_wsl_token", _wsl_token)
    _serve(wired, {"resolved": ["some__repo-1"], "unresolved": [], "error": [], "empty": []})

    _run(wired)

    assert calls["probe"] == 1
    assert calls["scp_from"] == [], "it went looking for a result it never asked for"
    assert _rows(wired["results"]) == []

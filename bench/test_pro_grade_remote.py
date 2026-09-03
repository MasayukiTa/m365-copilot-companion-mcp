# -*- coding: utf-8 -*-
"""Grading SWE-bench-Pro predictions: the entry point that did not exist.

WHAT ITS ABSENCE COST. Thirty-nine captured patches went unscored for a night. The only grader
anyone could invoke passes `--dataset_name princeton-nlp/SWE-bench_Lite`, and the instances were
Pro -- not in Lite, so the harness found nothing, wrote no report, and every verdict came back
EVALERR. That looks exactly like a broken eval host, and it was reported as one. Twice. The Pro
pipeline was on the host the whole time; what was missing was any way to run it that did not
have a smoke run's filenames baked in.
"""
import io
import json
import os

import pytest

from bench import pro_grade_remote as G


# -- who the cycle actually calls ------------------------------------------------------------

def _script_args_in_calls(path):
    """Every string constant that pro_cycle passes to a call, comments and docstrings excluded.

    Read as text this file is useless for the question: pro_cycle explains this whole incident in
    its comments, so "swe_grade_batch" appears there several times on purpose. A substring search
    matches the explanation and reports the bug as fixed while the call is still wrong. Parsing
    Call nodes asks what the code DOES.
    """
    import ast
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in list(node.args) + [k.value for k in node.keywords]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out.append(sub.value)
    return out


def test_the_cycle_grades_with_the_pro_grader_not_the_lite_one():
    """The defect this whole module was written about, wired the wrong way for the entire time
    the module existed.

    grade.py on the eval host passes --dataset_name princeton-nlp/SWE-bench_Lite. Pro instances
    are not in Lite, so the harness finds nothing, writes no report, and the runner's fallback
    writes VERDICT=EVALERR. Measured on the run that exposed it: 4 instances, 4 patches, 4
    EVALERR, 0 graded -- and twice before that it was reported as the eval host being down.
    """
    consts = _script_args_in_calls(os.path.join(os.path.dirname(__file__), "pro_cycle.py"))
    assert "pro_grade_remote.py" in consts, (
        "the cycle must grade through pro_grade_remote, which builds the "
        "ScaleAI/SWE-bench_Pro rows before scoring"
    )
    assert "swe_grade_batch.py" not in consts, (
        "swe_grade_batch drives the Lite grader; every Pro verdict it returns is EVALERR"
    )


def test_the_grader_can_be_asked_for_one_batch(tmp_path, capsys, monkeypatch):
    """The cycle grades a batch at a time. Without a filter the preds file -- which accumulates
    every capture ever made, eighty rows when this was written -- would be re-scored after each
    batch to learn about three instances."""
    monkeypatch.setenv("SWE_EVAL_HOST", "someho.st")
    preds = tmp_path / "preds.json"
    preds.write_text(json.dumps([
        {"instance_id": "keep_me", "patch": "diff --git a/x b/x\n"},
        {"instance_id": "other", "patch": "diff --git a/y b/y\n"},
    ]), encoding="utf-8")
    rc = G.main(["--preds", str(preds), "--results", str(tmp_path / "r.json"),
                 "--dry-run", "--instances", "keep_me"])
    assert rc == 0
    assert "would stage 1 predictions" in capsys.readouterr().out


def test_no_filter_still_grades_everything():
    """The filter is opt-in. pro_grade_remote has other callers that grade the whole ledger, and
    a filter that defaulted to empty would silently grade nothing for them."""
    import ast
    src = io.open(os.path.join(os.path.dirname(__file__), "pro_grade_remote.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    default = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "add_argument"
                and node.args and getattr(node.args[0], "value", "") == "--instances"):
            for k in node.keywords:
                if k.arg == "default":
                    default = k.value
    assert default is not None, "--instances must declare its default explicitly"
    assert ast.literal_eval(default) is None


# -- what gets sent ------------------------------------------------------------------------

def test_only_predictions_with_a_patch_are_sent():
    """An EMPTY row usually means the capture REFUSED an oversize diff -- one instance produced
    74,850,968 bytes. Sending it would let the harness score it as unresolved, turning "we have
    no patch" into "the patch was wrong"."""
    rows = G.gradeable([
        {"instance_id": "a", "patch": "diff --git a/x b/x\n"},
        {"instance_id": "b", "patch": ""},
        {"instance_id": "c", "patch": "   \n"},
        {"instance_id": "d", "model_patch": "diff --git a/y b/y\n"},
        {"instance_id": "", "patch": "diff"},
        "not a dict",
    ])
    assert [r["instance_id"] for r in rows] == ["a", "d"]


def test_it_sends_the_key_the_pro_harness_reads():
    """The Pro predictions file uses `patch`; the Lite grader used `model_patch`. Reading the
    real file on the host settled which -- guessing here is what produced the whole incident."""
    rows = G.gradeable([{"instance_id": "a", "model_patch": "diff"}])
    assert set(rows[0]) == {"instance_id", "patch"}
    assert rows[0]["patch"] == "diff"


# -- the two rules that are compiled in ------------------------------------------------------

def test_the_script_never_prunes():
    """The eval host's drive is append-only by the owner's decision, made after a drive was
    destroyed by exactly this churn. The predecessor's floor-janitor was disabled for it; this
    one must not be able to grow one back."""
    s = G.grade_script("/r.jsonl", "/p.json", "/out", "/log.out")
    for forbidden in ("docker system prune", "docker rmi", "docker image prune", "rm -rf /mnt"):
        assert forbidden not in s, "the grade script would delete images: %r" % forbidden


def test_the_script_stops_rather_than_making_room():
    """Measure first and refuse, instead of deleting to continue."""
    s = G.grade_script("/r.jsonl", "/p.json", "/out", "/log.out")
    assert "refusing to start" in s
    assert "exit 3" in s


def test_the_script_names_the_pro_harness_not_the_lite_one():
    s = G.grade_script("/r.jsonl", "/p.json", "/out", "/log.out")
    assert "swe_bench_pro_eval.py" in s
    assert "SWE-bench_Lite" not in s
    assert "--raw_sample_path" in s and "--patch_path" in s


def test_the_output_it_reads_back_is_the_one_it_writes():
    s = G.grade_script("/r.jsonl", "/p.json", "/out_dir", "/log.out")
    assert "/out_dir" in s and "eval_results.json" in s


# -- the transport lesson, compiled in -------------------------------------------------------

def test_stale_locks_are_cleared(tmp_path, monkeypatch):
    """THE STEP THAT WAS MISSING FROM THE PROCEDURE, and it cost a night. Killing a hung
    cloudflared leaves 0-byte .lock files; the next cloudflared waits on them forever and the
    symptom is a banner-exchange timeout that reads as the host being down. `Get-Process
    cloudflared` returning zero is not proof of innocence -- the locks outlive the process."""
    monkeypatch.setattr(G, "_cloudflared_running", lambda: False)
    (tmp_path / "a-token.lock").write_bytes(b"")
    (tmp_path / "b-token.lock").write_bytes(b"")
    assert G.clear_stale_locks(str(tmp_path)) == 2
    assert not list(tmp_path.glob("*.lock"))


def test_a_lock_somebody_is_holding_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "_cloudflared_running", lambda: True)
    (tmp_path / "a-token.lock").write_bytes(b"")
    assert G.clear_stale_locks(str(tmp_path)) == 0
    assert list(tmp_path.glob("*.lock"))


def test_a_non_empty_lock_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "_cloudflared_running", lambda: False)
    (tmp_path / "a-token.lock").write_bytes(b"held")
    assert G.clear_stale_locks(str(tmp_path)) == 0


def test_an_unreadable_process_list_leaves_the_locks_alone(monkeypatch):
    """FAIL CLOSED. Breaking a live lock is worse than leaving a stale one."""
    import builtins
    real = builtins.__import__

    def no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("gone")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_psutil)
    assert G._cloudflared_running() is True


def test_every_transport_call_clears_locks_first():
    """SOURCE-LEVEL, stated as such: these shell out over a tunnel. What is asserted is that no
    entry point can connect without the cleanup, because forgetting it once already produced a
    misdiagnosis of someone else's machine."""
    import inspect
    for fn in (G.ssh, G.scp_to, G.scp_from):
        assert "clear_stale_locks()" in inspect.getsource(fn), fn.__name__


def test_it_uses_windows_openssh_explicitly():
    """Git Bash ships its own ssh and the ProxyCommand is a Windows path. Mixing them is one
    more way to spend an hour on a connection that was never going to work."""
    import inspect
    assert "OpenSSH" in inspect.getsource(G._ssh_base)


# -- folding the result back -----------------------------------------------------------------

def test_results_are_written_in_the_shared_vocabulary(tmp_path):
    p = tmp_path / "results.json"
    added = G.ingest({"a": True, "b": False}, str(p))
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert added == 2
    assert {r["instance_id"]: r["verdict"] for r in rows} == {"a": "RESOLVED", "b": "not"}


def test_an_instance_already_graded_is_not_written_twice(tmp_path):
    """Re-measuring an instance that already has a score is how a benchmark number drifts
    upward without anybody deciding to cheat."""
    p = tmp_path / "results.json"
    p.write_text(json.dumps({"instance_id": "a", "verdict": "RESOLVED"}) + "\n", encoding="utf-8")
    assert G.ingest({"a": True, "b": True}, str(p)) == 1


def test_an_earlier_evalerr_does_not_block_the_real_verdict(tmp_path):
    """EVALERR means the evaluation could not be RUN. Treating it as a score is what would have
    left 36 patches unscored for good."""
    p = tmp_path / "results.json"
    p.write_text(json.dumps({"instance_id": "a", "verdict": "EVALERR"}) + "\n", encoding="utf-8")
    assert G.ingest({"a": True}, str(p)) == 1
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rows[-1]["verdict"] == "RESOLVED"


def test_ingest_never_invents_an_evalerr(tmp_path):
    """This function only sees instances the harness reported on, so it has nothing to say
    about the ones it did not."""
    p = tmp_path / "results.json"
    G.ingest({"a": False}, str(p))
    assert "EVALERR" not in p.read_text(encoding="utf-8")


# -- the host is configuration, not a diagnosis ----------------------------------------------

def test_a_missing_host_is_reported_as_configuration(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(G, "ssh_host", lambda: "")
    rc = G.main(["--preds", str(tmp_path / "nope.json")])
    assert rc == 2
    assert "no eval host configured" in capsys.readouterr().out


def test_a_stray_carriage_return_does_not_silently_drop_instances(tmp_path, capsys, monkeypatch):
    """How fifteen instances became one.

    The list was written by Python in text mode on Windows, so every line but the last carried a
    trailing carriage return through the shell. The filter matched exactly one id, graded it,
    and reported "graded 1 instance(s)" -- a true sentence about a fraction of the job.
    """
    monkeypatch.setenv("SWE_EVAL_HOST", "someho.st")
    preds = tmp_path / "p.json"
    preds.write_text(json.dumps([
        {"instance_id": "a", "patch": "diff --git a/x b/x\n"},
        {"instance_id": "b", "patch": "diff --git a/y b/y\n"},
    ]), encoding="utf-8")
    rc = G.main(["--preds", str(preds), "--results", str(tmp_path / "r.json"),
                 "--dry-run", "--instances", "a\r", "b"])
    assert rc == 0
    assert "would stage 2 predictions" in capsys.readouterr().out


def test_an_instance_with_no_patch_is_named_not_dropped(tmp_path, capsys, monkeypatch):
    """Grading a subset of what was asked for must never look like grading all of it."""
    monkeypatch.setenv("SWE_EVAL_HOST", "someho.st")
    preds = tmp_path / "p.json"
    preds.write_text(json.dumps([{"instance_id": "a", "patch": "diff --git a/x b/x\n"}]),
                     encoding="utf-8")
    G.main(["--preds", str(preds), "--results", str(tmp_path / "r.json"),
            "--dry-run", "--instances", "a", "ghost"])
    out = capsys.readouterr().out
    assert "have no gradeable patch and were NOT graded" in out
    assert "ghost" in out


# -- folding results into the ledger ---------------------------------------------------------

def test_a_retracted_verdict_does_not_block_the_real_one(tmp_path):
    """The last step at which a measurement can be lost, and it lost one.

    ingest() treated an instance as already known if ANY non-EVALERR row mentioned it, without
    regard to order. Sixteen instances graded in 1186 seconds -- every image pulled, no None,
    the first trustworthy grade in days -- reported "0 new rows in the ledger", because each
    carried a stale "not" written while the eval filesystem was read-only and later retracted
    by appending EVALERR. Order was never consulted, so the retraction was invisible.
    """
    led = tmp_path / "results.json"
    led.write_text(
        json.dumps({"instance_id": "a", "verdict": "not"}) + "\n"
        + json.dumps({"instance_id": "a", "verdict": "EVALERR", "note": "host was read-only"})
        + "\n", encoding="utf-8")
    assert G.ingest({"a": True}, str(led)) == 1
    rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert rows[-1]["instance_id"] == "a" and rows[-1]["verdict"] == "RESOLVED"


def test_an_instance_already_graded_for_real_is_not_regraded(tmp_path):
    """The behaviour worth keeping: a standing verdict is not overwritten by a later run, or a
    re-grade would silently replace measurements nobody asked to redo."""
    led = tmp_path / "results.json"
    led.write_text(json.dumps({"instance_id": "a", "verdict": "RESOLVED"}) + "\n",
                   encoding="utf-8")
    assert G.ingest({"a": False}, str(led)) == 0


def test_an_instance_the_ledger_has_never_seen_is_written(tmp_path):
    led = tmp_path / "results.json"
    led.write_text("", encoding="utf-8")
    assert G.ingest({"a": True, "b": False}, str(led)) == 2

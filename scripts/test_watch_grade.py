# -*- coding: utf-8 -*-
"""The grade watcher must be safe to run while a grade is running, and must call a zero a zero.

Two things this pins.

FIRST, it may not start a WSL session. The grade runs inside one held ssh session because the
WSL VM tears down when a `wsl -d ... --exec` command returns; every ordinary way of looking at
it is itself such a session. A 14-instance grade died about twenty seconds after two progress
checks were issued against the same host, leaving `client_loop: send disconnect` and no
eval_results.json, and the run had to be repeated. The log is on /mnt/c, so reading it from the
Windows side answers the same question without a VM.

SECOND, it must recognise the shape of a run that is producing zeros rather than measurements.
Fourteen instances were once "graded" in 87 seconds, every image missing, every verdict None,
and the wrapper wrote 0.0% into the ledger as though that were a score.
"""
import ast
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import watch_grade as W   # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watch_grade.py")


def _strings_in_calls(path):
    """Every string constant the module EXECUTES with; comments and docstrings excluded.

    The file explains the hazard in prose that necessarily contains the word wsl, so a substring
    search over the text matches the explanation and passes while the call is still there. The
    first version of this looked only inside Call nodes and missed the command entirely, because
    it is built into a local first and passed second -- so it reported "no Get-Content here" for
    a module whose only remote command is a Get-Content.
    """
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                docstrings.add(d)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                out.append(node.value)
    return out


def test_the_watcher_never_starts_a_wsl_session():
    """The whole point. A watcher that can kill what it is watching is worse than no watcher."""
    for value in _strings_in_calls(SRC):
        assert "wsl" not in value.lower(), value


def test_the_command_it_sends_reads_from_the_windows_side():
    assert any("Get-Content" in v for v in _strings_in_calls(SRC))


def test_a_missing_image_is_reported_as_manufacturing_zeros():
    """Not "0% resolved". Every instance that cannot find its image is recorded as a zero, and
    the run should be stopped rather than left to finish."""
    log = ("[08:29:50] START pro grade free=233G\n"
           "Failed to pull or find image locally for instance_x: 404 Client Error\n"
           "Evaluation for instance_x returned None\n")
    state, notes = W.assess(log)
    assert state == "running"
    joined = " ".join(notes)
    assert "Failed to pull or find image locally" in joined
    assert "recorded as a zero" in joined
    assert "returned None" in joined


def test_a_batch_that_finishes_too_fast_is_called_out():
    """87 seconds for fourteen instances is not a duration in which anything is evaluated."""
    log = ("[08:29:50] START pro grade free=233G\n"
           "RESOLVED 0/14 = 0.0%\n"
           "DONE_PRO_GRADE 08:31:17 free=233G\n")
    state, notes = W.assess(log)
    assert state == "finished"
    joined = " ".join(notes)
    assert "TOO FAST TO BE A MEASUREMENT" in joined
    assert "RESOLVED 0/14" in joined


def test_a_real_length_run_is_not_flagged_as_too_fast():
    """The guard must not cry wolf on a grade that actually ran."""
    log = ("[08:29:50] START pro grade free=233G\n"
           "RESOLVED 7/14 = 50.0%\n"
           "DONE_PRO_GRADE 09:14:02 free=180G\n")
    state, notes = W.assess(log)
    assert state == "finished"
    assert "TOO FAST" not in " ".join(notes)


def test_a_run_that_crosses_midnight_is_not_reported_as_negative():
    log = ("[23:58:00] START pro grade free=233G\n"
           "DONE_PRO_GRADE 00:41:00 free=180G\n")
    _, notes = W.assess(log)
    assert "took 2580 seconds end to end" in " ".join(notes)


def test_an_empty_log_is_unknown_rather_than_healthy():
    """A grade that never started must not read as one that is running fine."""
    state, notes = W.assess("")
    assert state == "unknown"
    assert any("may not have started" in n for n in notes)

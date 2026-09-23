# -*- coding: utf-8 -*-
"""The underpowered-slice refusal, proven through loop.py's REAL command-line entry point.

test_a_slice_that_cannot_decide_is_not_burned.py already proves the refusal and the n==min_n
warning by CALLING `validate(...)` as a Python function, with `L.G` and `L.log` monkeypatched.
That is real coverage of the logic, and it is not the same claim as "the refusal fires when a
person or a scheduled task actually runs this the way it is documented to be run" -- commit
93d4b45's own history is a prior instance of exactly that gap: something that passed in tests
and never fired in a real run. This file closes that specific gap for THIS guard by launching
`relay/selfimprove/loop.py`'s own `main()` -- the same `argparse` parser and the same `validate`
call bottom-of-file uses -- as a **child process**, and reading the refusal/warning off its real
stdout and its real exit code.

WHY NOT LITERALLY "python -m relay.selfimprove.loop ...". `validate()` writes its targets file
to `os.path.join(SWEDIR, "_selfimprove_slice.txt")` UNCONDITIONALLY, before the refusal check
even runs (relay/selfimprove/loop.py, `select_fresh_slice` result then `open(targets_file, "w")`
-- see the `validate` function body, a few lines before the `margin < 0` check). `SWEDIR` is a
module-level constant (`os.path.join(REPO, ".fleet", "swe")`) with no CLI flag or environment
override, and this task's own hard rule is "do not write under .fleet/". Plain
`python -m relay.selfimprove.loop` therefore cannot be driven honestly into `tmp_path` at all --
every invocation, refused or not, writes one file into the real `.fleet/swe/` first.

The child process launched here still runs the REAL, UNMODIFIED `main()` -- real `argparse`
parsing of `sys.argv`, real call into the real `validate()` -- via a two-line bootstrap script
(written into `tmp_path`, not committed anywhere) that does exactly one thing before calling
`main()`: `L.SWEDIR = <a directory under tmp_path>`. That is the same kind of seam
`test_a_slice_that_cannot_decide_is_not_burned.py` already leans on with `monkeypatch.setattr(L,
"SWEDIR", ...)` -- the difference here is that everything downstream of that one assignment,
including `argparse` and `validate`, runs for real, inside a real child process, rather than
being called directly by the test.

WHY THIS CANNOT LAUNCH GRADING, A FLEET, OR A REMOTE HOST, structurally rather than by promise --
read `validate()` top to bottom to see why:

  * the n < min_n case returns from the `margin < 0` branch (relay/selfimprove/loop.py, the
    block starting `if margin < 0:`) BEFORE the `if dry_run:` check even exists in the function
    -- there is no argument that reaches past this branch once it is taken, `--dry-run` or not.
  * the n == min_n case falls through the `margin == 0` warning into `if dry_run: ... return
    plan` -- which is the line immediately before `dataset = DATASETS[dataset_key]` and the
    `os.makedirs`/`_run_solve_arm` calls that would start real work. `--dry-run` is passed on
    every invocation in this file specifically to guarantee that stop, for BOTH cases (it is a
    no-op for the n < min_n case, which never reaches the check, and it is the thing that stops
    the n == min_n case).

  Neither `_run_solve_arm` (which shells out to `bench/swe_solve_decoupled.py`) nor `_grade_arm`
  (which shells out to `bench/swe_grade_swebench.py`, the eval-host path) is reachable from
  either argument set this file constructs. Nothing here calls a fleet or a remote host because
  the code path that would has not been given the chance to run.

MUTATION-CHECK (performed by hand while writing this, not part of the automated suite): a .bak
copy of loop.py was made, the `return {...}` in the `if margin < 0:` branch was deleted (fall
through to the rest of `validate`), and this file's `test_the_refusal_prints_and_stops_before_
the_dry_run_plan_line` failed -- the mutated process printed "DRY-RUN plan" for the n < min_n
case, which the un-mutated one never does. loop.py was restored from the .bak and `git diff
--stat relay/selfimprove/loop.py` showed no change afterward.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from relay.selfimprove import loop as L  # noqa: E402 -- for introspection only (REPO/VENVPY),
                                          # never called directly: the child process is what runs.
from tools import childproc  # noqa: E402


def _spec(tmp_path, n, name, prefix="a__a-"):
    """A spec file in the real shape `select_fresh_slice` reads: a JSON list of
    {"instance_id": ...} objects (see relay/selfimprove/loop.py `select_fresh_slice`)."""
    import json
    p = tmp_path / name
    p.write_text(json.dumps([{"instance_id": "%s%d" % (prefix, i)} for i in range(n)]),
                encoding="utf-8")
    return str(p)


def _write_runner(tmp_path, swedir):
    """A two-line bootstrap that calls loop.py's REAL, unmodified `main()` -- real argparse,
    real `validate()` -- after redirecting the one module-level path (`SWEDIR`) that `validate`
    writes to unconditionally and that no CLI flag reaches. See the module docstring for why
    this exists instead of a literal `python -m relay.selfimprove.loop`."""
    runner = tmp_path / "run_the_real_entry_point.py"
    runner.write_text(
        "import sys\n"
        "sys.path.insert(0, %r)\n" % REPO +
        "from relay.selfimprove import loop as L\n"
        "L.SWEDIR = %r\n" % str(swedir) +
        "sys.argv = ['loop.py'] + sys.argv[1:]\n"
        "L.main()\n",
        encoding="utf-8")
    return runner


def _run_real_entry(tmp_path, argv, case_name):
    """Launch loop.py's real `main()` as a child process (via `tools.childproc.run`, per this
    repository's ratchet against undecoded child output) with the given CLI argv, and return
    the completed process."""
    swedir = tmp_path / ("fleet_swe_" + case_name)
    swedir.mkdir()
    runner = _write_runner(tmp_path, swedir)
    cmd = [L.VENVPY if os.path.isfile(L.VENVPY) else sys.executable, str(runner)] + argv
    return childproc.run(cmd, cwd=REPO, timeout=120)


# ── n < min_n: refused before anything burns, through the real entry point ─────────────────

def test_the_refusal_prints_and_stops_before_the_dry_run_plan_line(tmp_path):
    spec_path = _spec(tmp_path, 5, "spec_underpowered.json")
    burned_path = tmp_path / "burned_underpowered.jsonl"
    argv = ["--toggle", "T", "--spec", spec_path, "--n", "5", "--seed", "1",
            "--dataset", "Verified", "--alpha", "0.05", "--min-n", "10", "--min-pp", "1.0",
            "--chunk", "20", "--max-concurrent", "3", "--max-turns", "50", "--floor-gb", "7.0",
            "--dry-run", "--burned", str(burned_path)]

    proc = _run_real_entry(tmp_path, argv, "underpowered")

    assert proc.returncode == 0, (
        "the real entry point should exit cleanly on a refusal, not crash:\n%s" % proc.stderr)
    assert "REFUSING" in proc.stdout, proc.stdout
    assert "guaranteed" in proc.stdout, proc.stdout
    # THE MUTATION-CATCHING ASSERTION: a real refusal returns before `if dry_run:` is even
    # reached, so "DRY-RUN plan" must never appear for this n < min_n case. A refusal branch
    # that fell through instead of returning (the mutation described in the module docstring)
    # would reach the dry-run return and print this line -- which is exactly what happened when
    # that mutation was tried by hand.
    assert "DRY-RUN plan" not in proc.stdout, (
        "the refusal did not stop the run -- execution fell through to the dry-run plan:\n%s"
        % proc.stdout)
    assert not burned_path.exists(), (
        "a refused configuration must never burn instances, but the burned registry file was "
        "created: %s" % burned_path)
    # AND IT WRITES NOTHING. The targets file is the instance list a validation already in
    # progress reads, and a refused invocation used to overwrite it -- the refusal ran after the
    # write. Found by this file needing to redirect SWEDIR at all.
    slice_file = tmp_path / "fleet_swe_underpowered" / "_selfimprove_slice.txt"
    assert not slice_file.exists(), (
        "a refused configuration wrote the targets file another run may be reading: %s"
        % slice_file)


def test_the_refusal_names_what_would_fix_it_through_the_real_entry(tmp_path):
    spec_path = _spec(tmp_path, 5, "spec_underpowered2.json")
    burned_path = tmp_path / "burned_underpowered2.jsonl"
    argv = ["--toggle", "T", "--spec", spec_path, "--n", "5", "--seed", "1",
            "--dataset", "Verified", "--alpha", "0.05", "--min-n", "10", "--min-pp", "1.0",
            "--chunk", "20", "--max-concurrent", "3", "--max-turns", "50", "--floor-gb", "7.0",
            "--dry-run", "--burned", str(burned_path)]

    proc = _run_real_entry(tmp_path, argv, "underpowered2")

    assert proc.returncode == 0, proc.stderr
    assert "--n" in proc.stdout and "--min-n" in proc.stdout, proc.stdout


# ── n == min_n: warned, then stopped by --dry-run before any solve/grade arm ───────────────

def test_zero_margin_warns_through_the_real_entry_and_dry_run_stops_it_there(tmp_path):
    spec_path = _spec(tmp_path, 10, "spec_zero_margin.json")
    burned_path = tmp_path / "burned_zero_margin.jsonl"
    argv = ["--toggle", "T", "--spec", spec_path, "--n", "10", "--seed", "1",
            "--dataset", "Verified", "--alpha", "0.05", "--min-n", "10", "--min-pp", "1.0",
            "--chunk", "20", "--max-concurrent", "3", "--max-turns", "50", "--floor-gb", "7.0",
            "--dry-run", "--burned", str(burned_path)]

    proc = _run_real_entry(tmp_path, argv, "zero_margin")

    assert proc.returncode == 0, (
        "the real entry point should exit cleanly on the zero-margin warning path:\n%s"
        % proc.stderr)
    assert "WARNING" in proc.stdout and "ZERO attrition" in proc.stdout, proc.stdout
    # --dry-run is what stops this case before `_run_solve_arm`/`_grade_arm` -- i.e. before
    # anything that could start real solving, grading, a fleet, or a remote host. Its presence
    # in stdout is the proof the run actually reached (and stopped at) that line, not a promise.
    assert "DRY-RUN plan" in proc.stdout, (
        "expected the run to reach the dry-run stop after the warning, but it did not:\n%s"
        % proc.stdout)
    # Nothing past the dry-run return can have run, so none of the solve/grade log lines
    # (which only `_run_solve_arm`/`_grade_arm` -- unreached here -- ever print) may appear.
    for unreachable in ("solve ON", "solve OFF", "ON resolved", "OFF resolved", "GATE verdict"):
        assert unreachable not in proc.stdout, (
            "%r appears in stdout; the dry-run stop did not hold and real work may have "
            "started:\n%s" % (unreachable, proc.stdout))
    assert not burned_path.exists(), (
        "a dry-run must never burn instances, but the burned registry file was created: %s"
        % burned_path)

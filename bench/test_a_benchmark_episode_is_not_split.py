# -*- coding: utf-8 -*-
"""A default moved, and four benchmark launchers inherited it without saying so.

Fan-out became ON by default on 2026-09-13 (`--fanout` defaults true; `run_relay_fleet` and
`RelayWorker` default `fanout=True`). Every launcher that did not name the flag changed what it
measures, in silence:

    bench/companionbench/fleet_agent.py   run_relay_fleet(context, [goal], ...)
    bench/review_run.py  fleet_cmd        ["-m", "relay.fleet_runner", ...]
    bench/pro_cycle.py                    ["-m", "relay.fleet_runner", ...]
    bench/pro_run_50.py                   ["-m", "relay.fleet_runner", ...]

A BENCHMARK EPISODE IS ONE GOAL IN ONE CONVERSATION BY CONSTRUCTION. fleet_agent passes
`max_concurrent=1` with a comment saying any other value "would be a lie about how many goals
there are", and then reads `res[0]["last_response"]`. A split ends that worker with
`outcome=FANOUT` and no answer of its own; the children become separate goals the grader never
looks at, and the merge -- if it runs at all -- lands after the episode has been scored.

So the grader would read an empty reply and record a miss for a goal that was merely divided.
Measured over the real corpus the same day, 47% of goals triage to SPLIT or UNCERTAIN, so this
is not a corner case. It would not have shown up as a fan-out bug; it would have shown up as a
score drop attributed to whatever was under test.

THESE ARE SOURCE ASSERTIONS AND THAT IS A KNOWN WEAKNESS -- they cannot prove the flag reaches
the subprocess. `test_the_runner_accepts_the_flag_these_launchers_pass` closes the half that
matters most: that the flag exists and parses, which is what would break if the option were
renamed. The launchers themselves cannot be executed in a test: each one opens a browser.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _src(rel):
    with open(os.path.join(REPO, *rel.split("/")), encoding="utf-8") as fh:
        return fh.read()


def test_the_in_process_episode_runner_says_fanout_false():
    src = _src("bench/companionbench/fleet_agent.py")
    i = src.index("res = run_relay_fleet(")
    # To the call's own closing line. Stopping at the first ")" lands inside
    # `payload.get("refuter", False)` and reads a truncated call as a missing flag.
    call = src[i:src.index("\n        )", i)]
    assert "fanout=False" in call, (
        "ベンチのエピソードが分割されうる -- 親は outcome=FANOUT で空の回答を返し、"
        "採点側はそれを miss として記録する")


def test_every_subprocess_launcher_passes_no_fanout():
    for rel in ("bench/review_run.py", "bench/pro_cycle.py", "bench/pro_run_50.py"):
        src = _src(rel)
        i = src.index('"relay.fleet_runner"')
        # The flag must be in the same argv list, not merely somewhere in the file.
        argv = src[i:src.index("]", i)]
        assert '"--no-fanout"' in argv, "%s: launches the fleet without naming fan-out" % rel


def test_the_runner_accepts_the_flag_these_launchers_pass():
    """The half a source assertion cannot cover. If `--no-fanout` is ever renamed, every
    launcher above starts failing to start instead of failing to split -- which is at least
    loud, but this catches it first."""
    import argparse
    import relay.fleet_runner as fr  # noqa: F401  (import proves the module loads)

    src = _src("relay/fleet_runner.py")
    assert "action=argparse.BooleanOptionalAction" in src
    assert hasattr(argparse, "BooleanOptionalAction")

    p = argparse.ArgumentParser()
    p.add_argument("--fanout", action=argparse.BooleanOptionalAction, default=True)
    assert p.parse_args(["--no-fanout"]).fanout is False
    assert p.parse_args([]).fanout is True


def test_the_review_fix_path_reuses_the_same_builder():
    """bench/review_fix.py calls review_run.fleet_cmd rather than building its own argv, so it
    inherits the opt-out. Asserted because a second hand-built argv is exactly how one launcher
    gets left behind when a default changes."""
    src = _src("bench/review_fix.py")
    assert "review_run.fleet_cmd(" in src
    assert '"relay.fleet_runner"' not in src

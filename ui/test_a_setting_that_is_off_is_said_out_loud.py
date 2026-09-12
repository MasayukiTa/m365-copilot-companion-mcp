# -*- coding: utf-8 -*-
"""The cockpit said "off" by saying nothing, and nothing started meaning on.

    bool _fanout = false;                          // the operator's setting
    if (_fanout) psi.Arguments += " --fanout";      // how it reached the runner

On 2026-09-13 `--fanout` became `argparse.BooleanOptionalAction` with `default=True`, so
OMITTING the flag means ON. From that moment a person who typed `/fanout off` got fan-out
anyway -- and the cockpit went on printing 分割実行 OFF at them. A control that reports a state
it is not producing is worse than no control, and this is the surface most people use.

THE RESUME PATH NEVER CARRIED IT AT ALL. `-m relay.fleet_runner --resume` passed --state-dir
and --effort and nothing else, so the setting was ignored there in both directions: off before
the default moved, on after.

THIRD INSTANCE OF ONE SHAPE IN ONE DAY. The four benchmark launchers were the first two
batches. The cause is not the default: it is expressing a boolean by the PRESENCE of a flag.
Presence can state only one of two values, so the other is stated by an absence -- which is
indistinguishable from "not configured", and therefore from whatever the default happens to be
this week. Both values are written out now.

SOURCE ASSERTIONS, and the limits are real: these read FleetCockpit.cs as text. They cannot
prove the built binary behaves this way, and C# in this repository has no executable test
harness. What they do catch is the edit that quietly drops one branch again, which is exactly
what happened.
"""
from __future__ import annotations

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SRC = os.path.join(REPO, "ui", "FleetCockpit.cs")


def _src():
    with open(SRC, encoding="utf-8") as fh:
        return fh.read()


def test_the_cockpit_default_matches_the_runners_default():
    """Otherwise an untouched installation disagrees with every other entry point about what
    'not configured' means."""
    m = re.search(r"bool\s+_fanout\s*=\s*(true|false)\s*;", _src())
    assert m, "the fan-out setting field is gone or renamed; re-derive this test"
    assert m.group(1) == "true", (
        "cockpit defaults fan-out off while relay.fleet_runner defaults it on")


def test_both_values_reach_the_runner_on_a_new_run():
    src = _src()
    i = src.index('psi.Arguments = "-m relay.fleet_runner --goals-file')
    block = src[i:src.index("Process.Start(psi)", i)]
    assert "--fanout" in block and "--no-fanout" in block, (
        "片方の値しか渡していない -- 渡さない側は「未設定」と区別できず、既定が動いた瞬間に無視される")


def test_both_values_reach_the_runner_on_a_resume():
    src = _src()
    i = src.index('psi.Arguments = "-m relay.fleet_runner --resume"')
    block = src[i:src.index("Process.Start(psi)", i)]
    assert "--fanout" in block and "--no-fanout" in block, (
        "再開時に分割設定が渡らない -- 同じ run なのに操作者の答えが消える")


def test_the_off_branch_is_not_expressed_as_an_omission():
    """The specific regression: `if (_fanout) ... += " --fanout";` with no else."""
    src = _src()
    assert not re.search(r'if\s*\(\s*_fanout\s*\)\s*psi\.Arguments\s*\+=\s*"\s*--fanout"\s*;',
                         src), (
        "off を「何も付けない」で表現している -- 既定が true なのでそれは on を意味する")


def test_the_slash_command_still_writes_the_setting():
    """The other half of the control: what /fanout on|off persists. Unchanged, asserted so a
    fix to the launch side cannot quietly leave the setting unwritten."""
    src = _src()
    assert 'SaveKey("fanout", _fanout ? "on" : "off")' in src
    assert 'ln.StartsWith("fanout=")' in src

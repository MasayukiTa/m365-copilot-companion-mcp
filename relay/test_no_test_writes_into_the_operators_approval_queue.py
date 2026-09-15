# -*- coding: utf-8 -*-
"""The approval queue a test run can reach must never be the operator's own.

WHY A SECOND TEST, WHEN `test_live_record_isolation` ALREADY EXISTS. That one scans MODULE
CONSTANTS and checks each is either redirected or listed as deliberately unredirected. The
approval directory has no constant to scan on one of its two paths: `relay/skills.py` builds it
inside `SkillStore._default_gate_dir()` at construction time, from environment variables. So the
marker `.companion_gates` sat in `RECORD_DIR_MARKERS` while the route to it went past unread.

WHAT THAT COST. Measured 2026-09-15 on the live queue: **2,175 approval cards, of which 2,167
were written by test runs** -- every one carrying a pytest temp path, all sharing one digest,
median time from question to answer 0.08 seconds. Eight were real: a decision about the OGF
September report, two Skill approvals, two contract-state questions, a job approval. The
operator's answer when shown three of them was "it is not three -- do you not know the count?"
Eight real decisions were buried under two thousand that were never decisions at all.

This test asks the outcome instead of the spelling: **with conftest's environment in force, where
would a gate actually be written?** If that is the operator's home, it fails -- whatever the
constants say, and whichever of the two doors a future caller opens.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

#: The operator's real queues. Not a spelling this test compares against -- the paths below are
#: resolved and compared as filesystem locations, so a different spelling of the same directory
#: still fails.
HOME = Path(os.path.expanduser("~")).resolve()
REAL_QUEUES = (HOME / ".companion_gates", HOME / ".companion_runs")


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent)
        return True
    except (ValueError, OSError):
        return False


def _assert_not_the_operators(path, what):
    resolved = Path(str(path)).expanduser().resolve()
    for real in REAL_QUEUES:
        assert not _is_under(resolved, real) and resolved != real, (
            "%s resolves to the operator's own queue (%s). A test run that reaches an approval "
            "path will write a real card there; 2,167 of them accumulated this way. Set the "
            "matching environment variable in conftest at MODULE scope -- these are read at "
            "import, so a fixture is too late." % (what, resolved))


def test_the_gateway_gate_directory_is_not_the_operators():
    from tools import gate_ops
    _assert_not_the_operators(gate_ops.GATE_DIR, "tools.gate_ops.GATE_DIR")


def test_the_skill_stores_gate_directory_is_not_the_operators(tmp_path):
    """The door with no constant.

    `SkillStore` is constructed with no `gate_dir` by `relay_fleet._with_matched_skill`, which is
    exactly the call that fired two thousand times, so it is constructed that way here too.
    """
    from relay.skills import SkillStore
    store = SkillStore(str(tmp_path))
    _assert_not_the_operators(store.gate_dir, "SkillStore(...).gate_dir")


def test_both_doors_are_actually_closed_by_an_environment_variable():
    """Not just pointing elsewhere by luck -- pointing elsewhere BECAUSE something set it.

    `MCP_SKILLS_GATE_DIR` existed for a week as an escape hatch that nothing turned on, and the
    queue kept filling the whole time. An unset variable that happens to resolve somewhere
    harmless on this machine is the same hatch again.
    """
    for var in ("MCP_GATE_DIR", "MCP_SKILLS_GATE_DIR"):
        value = os.environ.get(var, "")
        assert value, (
            "%s is not set during the test run. It is the only thing that moves one of the two "
            "approval doors off the operator's home, and it is read at import -- conftest must "
            "set it at module scope." % var)
        _assert_not_the_operators(value, var)


@pytest.mark.parametrize("marker", [".companion_gates", ".companion_runs"])
def test_the_marker_is_still_the_one_the_scanner_knows(marker):
    """Keeps this test and the constant-scanner talking about the same directories.

    If `RECORD_DIR_MARKERS` is renamed or trimmed, the other test stops covering the paths that
    DO have constants, and this one would be the only thing left -- so it should go red and say
    so rather than quietly become the whole coverage.
    """
    from relay import test_live_record_isolation as scanner
    assert marker in scanner.RECORD_DIR_MARKERS, (
        "%s left RECORD_DIR_MARKERS; the constant scanner no longer covers it" % marker)

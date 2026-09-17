# -*- coding: utf-8 -*-
"""conftest hands each run a private sandbox. This is what stops a run inheriting another's.

THE FAILURE IT COMES FROM. tools/test_contract_activation.py::test_activate_then_approve_then_
pass_is_the_full_hitl_cycle failed roughly one run in eight, in a fixed order, with no random
ordering plugin installed -- so nothing about the ordering explained it. The sandbox paths were
named `<thing>_pytest_<os.getpid()>`, and nothing ever removed them. Windows recycles pids, so
a run that drew a pid an earlier run had used opened that run's directory and found its files.

Measured 2026-09-17: 179 companion_gates_pytest_* directories in the temp folder, 147 holding
the same answered gate this test writes -- {"answered": true, "answer": "approved"} for
"delete: demo scratch file". On a collision, check_op found the old approval on the FIRST call
and returned None, and the test that exists to prove an unanswered gate refuses an operation
watched it proceed. A stale "approved" is the one answer that must never be inherited, and the
same collision also hands over the job store, the memory store and two hash-chained ledgers.

The tag now carries a random half, which is what makes it unique over time, and the run drops
its own sandbox on exit with a sweep for runs that were killed before they could.
"""
from __future__ import annotations

import os
import re
import tempfile

import conftest as C


SANDBOX_ENV = (
    "MCP_GATE_DIR",
    "MCP_SKILLS_GATE_DIR",
    "MCP_SKILLS_STATE_DB",
    "MCP_LOCAL_JOB_DB",
    "MCP_SELFIMPROVE_LEDGER",
    "MCP_SELFIMPROVE_HYPOTHESES",
    "FLEET_STATE_DIR",
)


def test_the_run_tag_is_not_just_a_pid():
    """A pid alone is the defect. The tag must carry something a later process cannot redraw."""
    assert re.fullmatch(r"\d+_[0-9a-f]{8}", C._RUN_TAG), C._RUN_TAG
    assert C._RUN_TAG != str(os.getpid())


def test_every_sandbox_path_carries_the_tag():
    """Named one at a time so a path added later without the tag is a failure here rather than
    a flake somewhere else. Each of these is a store a previous run could otherwise hand over."""
    for var in SANDBOX_ENV:
        value = os.environ.get(var, "")
        assert value, "%s is not set; the sandbox is not in place" % var
        assert C._RUN_TAG in value, "%s=%s does not carry the run tag" % (var, value)


def test_nothing_outside_the_temp_directory_can_be_swept():
    """The cleanup deletes, so its reach is the thing to pin. It is confined to the temp
    directory and to the exact prefixes conftest creates; a name that merely contains one of
    them is not a match, and neither is anything elsewhere on the disk."""
    tmp = tempfile.gettempdir()
    for name in C._SANDBOX_NAMES:
        assert "%s" in name, name
        assert C._sandbox_path(name).startswith(tmp)
    prefixes = tuple(n.split("%s")[0] for n in C._SANDBOX_NAMES)
    assert not "my_notes.txt".startswith(prefixes)
    assert not "companion_notes.txt".startswith(prefixes)
    assert "companion_gates_pytest_1_abcdef12".startswith(prefixes)


def test_a_fresh_sandbox_is_swept_only_once_it_is_old(tmp_path, monkeypatch):
    """A run that is still going must survive the sweep -- deleting a live run's gate directory
    would turn its pending approvals into missing ones, which is the failure this file is about,
    arriving from the other direction."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(C._tempfile, "gettempdir", lambda: str(tmp_path))
    fresh = tmp_path / "companion_gates_pytest_99_deadbeef"
    fresh.mkdir()
    (fresh / "gate_x.json").write_text("{}", encoding="utf-8")
    keep = tmp_path / "somebody_elses_file.txt"
    keep.write_text("not ours", encoding="utf-8")

    C._drop_abandoned_sandboxes()
    assert fresh.is_dir(), "a sandbox minutes old was swept"
    assert keep.is_file()

    old = 1.0   # 1970; older than any threshold
    os.utime(fresh, (old, old))
    C._drop_abandoned_sandboxes()
    assert not fresh.exists(), "an abandoned sandbox was left behind"
    assert keep.is_file(), "the sweep reached a file that was not ours"

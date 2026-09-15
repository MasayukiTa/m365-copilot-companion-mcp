# -*- coding: utf-8 -*-
"""A test run must not put a window on the operator's desktop.

WHAT APPEARED, 2026-09-15. A dialog titled 「承認が必要です」 opened mid-run and said it could
not open the approval gate, naming two directories:

    skills_gates_pytest_24204   (where the gate was written)
    companion_gates_pytest_24204 (where the prompt looked)

Both carry a pytest pid, so the window was a child of a pytest process. And it could never have
been satisfied: `relay/skills.py` writes its approval questions through MCP_SKILLS_GATE_DIR while
the prompt resolves MCP_GATE_DIR -- the same directory in production, deliberately different
under test so neither leaks into the operator's real queue. The window existed only to report
its own failure, and the operator could do nothing with it but close it.

WHY THE EXISTING GUARD DID NOT HOLD. `notify_approval_gate` checks PYTEST_CURRENT_TEST, which
pytest sets only while a test FUNCTION is executing. Collection, session-scoped fixtures and
teardown all run with it absent. A guard that is present for part of a run is the harder kind to
notice missing, because it works every time anyone tests it directly.

So the switch conftest sets is a plain environment variable at MODULE scope, which is live from
import to exit and does not depend on which phase pytest thinks it is in.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import notify_ops  # noqa: E402


@pytest.fixture
def no_toast(monkeypatch):
    monkeypatch.setattr(notify_ops, "notify_desktop", lambda *a, **k: "toast")


def test_the_switch_is_on_for_this_whole_session():
    """Set at module scope in conftest, so it is true here, during collection, and in teardown."""
    assert os.environ.get("MCP_SUPPRESS_GUI") == "1", (
        "conftest no longer suppresses GUI prompts; a test run can open a window on the "
        "operator's desktop again")


def test_the_prompt_is_suppressed_even_with_no_pytest_variable(monkeypatch, no_toast, tmp_path):
    """The exact hole: outside a test function, PYTEST_CURRENT_TEST is gone."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    launched = []
    monkeypatch.setattr(notify_ops.subprocess, "Popen",
                        lambda *a, **k: launched.append(a) or None)
    gate = tmp_path / "gate_abc.json"
    gate.write_text("{}", encoding="utf-8")
    result = notify_ops.notify_approval_gate("t", "b", str(gate))
    assert not launched, "a window was launched with no test variable set"
    assert "MCP_SUPPRESS_GUI" in result


def test_it_still_says_what_it_did_rather_than_failing_silently(monkeypatch, no_toast, tmp_path):
    """A suppressed prompt that reports nothing is indistinguishable from a broken one."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    gate = tmp_path / "gate_abc.json"
    gate.write_text("{}", encoding="utf-8")
    result = notify_ops.notify_approval_gate("t", "b", str(gate))
    assert "toast" in result, "the toast result was dropped along with the window"
    assert "suppressed" in result


def test_with_the_switch_off_the_prompt_is_reachable_again(monkeypatch, no_toast, tmp_path):
    """The suppression must be a switch, not a removal -- production has no conftest, so this
    path is the one that actually runs for the operator."""
    monkeypatch.setenv("MCP_SUPPRESS_GUI", "0")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    launched = []
    monkeypatch.setattr(notify_ops.subprocess, "Popen",
                        lambda *a, **k: launched.append(a) or None)
    monkeypatch.setattr(notify_ops.Path, "is_file", lambda self: True)
    gate = tmp_path / "gate_abc.json"
    gate.write_text("{}", encoding="utf-8")
    notify_ops.notify_approval_gate("t", "b", str(gate))
    assert launched, "the prompt can no longer open at all, which breaks the operator's path"

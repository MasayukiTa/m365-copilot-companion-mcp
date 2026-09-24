# -*- coding: utf-8 -*-
"""No approval toast the person cannot act on (owner report, 2026-09-24).

The owner saw a stack of "Skill approval needed" toasts; clicking them opened nothing. The
gates they named lived in pytest temp directories: the approval prompt correctly refused them,
but the toast had already been shown, because notify_desktop only honoured PYTEST_CURRENT_TEST,
which a child process started outside a test function does not inherit. MCP_SUPPRESS_GUI
(conftest, module scope) does reach every child.

Both halves are exercised for real here, with the actual PowerShell launch replaced by a spy
that fails the test if it is reached.
"""
from __future__ import annotations

import pytest

from tools import notify_ops


@pytest.fixture
def no_shell(monkeypatch):
    calls = []

    def _spy(*a, **k):
        calls.append(a)
        raise AssertionError("a toast was about to be raised: %r" % (a,))

    import tools.childproc as childproc
    monkeypatch.setattr(childproc, "run", _spy)
    return calls


def _real_notify_desktop():
    """conftest's autouse fixture replaces notify_ops.notify_desktop with a capture stub for
    every test, so the attribute is not the function under test. A fresh, separately named
    copy of the module carries the real one."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_notify_ops_real", notify_ops.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.notify_desktop


def test_the_suppress_switch_silences_the_toast_even_outside_a_test_function(monkeypatch, no_shell):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("MCP_SUPPRESS_GUI", "1")
    out = _real_notify_desktop()("Skill approval needed", "body")
    assert "suppressed" in out and "MCP_SUPPRESS_GUI" in out
    assert no_shell == []


def test_a_gate_the_prompt_cannot_open_gets_no_toast(monkeypatch, tmp_path, no_shell):
    # Neither guard variable set: this is the production path, so only the reachability
    # rule can keep the toast away.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("MCP_SUPPRESS_GUI", raising=False)
    allowed = tmp_path / "companion_gates"
    allowed.mkdir()
    monkeypatch.setenv("MCP_GATE_DIR", str(allowed))
    elsewhere = tmp_path / "some_temp_gates"
    elsewhere.mkdir()
    gate = elsewhere / "gate_abc.json"
    gate.write_text("{}", encoding="utf-8")
    seen = []
    monkeypatch.setattr(notify_ops, "notify_desktop", lambda *a, **k: seen.append(a) or "shown")
    popen = []
    monkeypatch.setattr(notify_ops.subprocess, "Popen", lambda *a, **k: popen.append(a))
    out = notify_ops.notify_approval_gate("Skill approval needed", "q", gate)
    assert seen == [], "a toast was shown for a gate no click can open"
    assert popen == [], "the approval prompt was launched for an unreachable gate"
    assert "toast not shown" in out


def test_a_gate_the_prompt_can_open_still_gets_its_toast(monkeypatch, tmp_path, no_shell):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("MCP_SUPPRESS_GUI", raising=False)
    allowed = tmp_path / "companion_gates"
    allowed.mkdir()
    monkeypatch.setenv("MCP_GATE_DIR", str(allowed))
    gate = allowed / "gate_real.json"
    gate.write_text("{}", encoding="utf-8")
    seen = []
    monkeypatch.setattr(notify_ops, "notify_desktop", lambda *a, **k: seen.append(a) or "shown")
    monkeypatch.setattr(notify_ops.subprocess, "Popen", lambda *a, **k: None)
    notify_ops.notify_approval_gate("Skill approval needed", "q", gate)
    assert len(seen) == 1, "a real, answerable approval question must still notify"

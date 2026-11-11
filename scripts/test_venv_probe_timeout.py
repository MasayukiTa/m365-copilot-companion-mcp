# -*- coding: utf-8 -*-
"""The venv-probe timeout must be generous by default and raisable on a slow box.

WHY THIS FILE EXISTS. On a brand-new PC the installer failed in STEP 1/7 (ensure_venv):
every venv probe (`_venv_runs`, `_venv_version`, `_venv_has_pip`) spawned the venv's python
and waited a HARD-CODED 60s. Real-time AV scanning and a cold file cache tax every
interpreter start on a fresh machine -- measured there, a bare `python -c "import sys"` took
~8-9s and `python -m venv` ran past 60s -- so a perfectly healthy venv was mis-judged
"cannot run", and STEP 1/7 looped rebuilding a venv that was fine. The fix made the probe
timeout a single function, `_venv_probe_timeout()`, with a generous default and a
`MCP_VENV_PROBE_TIMEOUT` override.

These tests pin the OBSERVABLE contract of that function -- default, override, and that a
bad/blank/non-positive env value can never collapse it to something that would flap the
probe -- not how bootstrap calls it.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # scripts/ importable
import bootstrap  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(bootstrap._VENV_PROBE_TIMEOUT_ENV, raising=False)
    yield


def test_default_is_the_generous_value_not_the_old_60s():
    # The old hard-coded 60s is exactly what was too short on a fresh PC. The default must be
    # comfortably larger so a healthy-but-slow venv is not called broken.
    assert bootstrap._venv_probe_timeout() == bootstrap._VENV_PROBE_TIMEOUT_DEFAULT
    assert bootstrap._venv_probe_timeout() > 60


def test_env_var_raises_the_timeout(monkeypatch):
    monkeypatch.setenv(bootstrap._VENV_PROBE_TIMEOUT_ENV, "600")
    assert bootstrap._venv_probe_timeout() == 600.0


def test_env_var_accepts_a_float(monkeypatch):
    monkeypatch.setenv(bootstrap._VENV_PROBE_TIMEOUT_ENV, "90.5")
    assert bootstrap._venv_probe_timeout() == 90.5


@pytest.mark.parametrize("bad", ["", "   ", "abc", "0", "-5", "nan"])
def test_blank_or_bad_or_nonpositive_falls_back_to_default(monkeypatch, bad):
    # A bad env var must NEVER make the probe timeout zero/negative/NaN -- that would make a
    # slow child time out instantly and flap the very judgement this function guards.
    monkeypatch.setenv(bootstrap._VENV_PROBE_TIMEOUT_ENV, bad)
    out = bootstrap._venv_probe_timeout()
    assert out == bootstrap._VENV_PROBE_TIMEOUT_DEFAULT
    assert out > 0


def test_probe_helpers_pass_the_configured_timeout_through(monkeypatch):
    # The three probe helpers must actually USE _venv_probe_timeout(), so that raising the env
    # var raises the wait they impose on the child. We stub the child runner and the python
    # path check, capture the timeout each helper requests, and assert it is the configured
    # value -- not a stale literal.
    monkeypatch.setenv(bootstrap._VENV_PROBE_TIMEOUT_ENV, "321")
    seen = []

    class _Res:
        returncode = 0
        stdout = "3.10"

    def _fake_run(cmd, timeout=None, **kw):
        seen.append(timeout)
        return _Res()

    import tools.childproc as childproc
    monkeypatch.setattr(childproc, "run", _fake_run)

    # VENV_PYTHON.exists() guards each helper; a WindowsPath's .exists is read-only, so swap
    # the whole path object for a stand-in whose exists() is True.
    class _AlwaysExists:
        def exists(self):
            return True

        def __str__(self):
            return "<venv-python>"

    monkeypatch.setattr(bootstrap, "VENV_PYTHON", _AlwaysExists())

    bootstrap._venv_runs()
    bootstrap._venv_version()
    bootstrap._venv_has_pip()

    assert seen == [321.0, 321.0, 321.0], seen

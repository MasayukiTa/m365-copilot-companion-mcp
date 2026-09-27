# -*- coding: utf-8 -*-
"""Unlock repair never emits a password on stdout; the interactive values script can show it."""
from __future__ import annotations

import os
import shutil
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from tools import childproc  # noqa: E402

REPAIR = os.path.join(REPO, "scripts", "repair_unlock.py")
_POWERSHELL = (shutil.which("powershell") or
    (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
     if os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") else None))
pytestmark = pytest.mark.skipif(os.name != "nt", reason="DPAPI exists only on Windows")


def _protect(value):
    from tools.secret_store import protect_secret
    return protect_secret(value)


def _run(*args):
    proc = childproc.run([sys.executable, REPAIR, *args], timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.splitlines()


def _env(tmp_path, text):
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_legacy_cleartext_output_flags_are_refused_without_reading_the_secret(tmp_path):
    p = _env(tmp_path, "MCP_UNLOCK_PASSWORD_PROTECTED=%s\n" % _protect("known-pw-1234"))
    for flag in ("--current", "--show"):
        out = _run(flag, p)
        assert out == [r"failed:cleartext output was removed; run scripts\copilot_studio_values.bat"]
        assert "known-pw-1234" not in "\n".join(out)


def test_a_repair_under_capture_never_emits_the_new_value(tmp_path):
    p = _env(tmp_path, "MCP_API_KEY=k\nMCP_UNLOCK_PASSWORD_PROTECTED=dpapi:AAAAZm9yZWlnbg==\n")
    out = _run(p)
    assert len(out) == 1 and out[0].startswith("repaired:"), out
    assert "copilot_studio_values" in out[0]
    assert "password:" not in out[0].lower()


@pytest.mark.skipif(not _POWERSHELL, reason="needs Windows PowerShell")
def test_copilot_studio_values_prints_the_unlock_password_directly_from_dpapi(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(os.path.join(REPO, "scripts", "copilot_studio_values.ps1"), root / "scripts" / "copilot_studio_values.ps1")
    (root / ".env").write_text("MCP_API_KEY=bearer-abc\nMCP_UNLOCK_PASSWORD_PROTECTED=%s\n"
                               % _protect("visible-pw-5678"), encoding="utf-8")
    proc = childproc.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(root / "scripts" / "copilot_studio_values.ps1")], timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Unlock password  :  visible-pw-5678" in proc.stdout  # gitleaks:allow -- test fixture only


def test_repair_source_has_no_cleartext_password_print_path():
    src = open(REPAIR, encoding="utf-8").read()
    assert 'print("password:' not in src
    assert "_reveal_allowed" not in src
    assert "_show_current" not in src

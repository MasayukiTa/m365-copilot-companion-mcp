# -*- coding: utf-8 -*-
"""The unlock password can be read back by its owner, and never lands in start_all's capture.

D1 in the 2026-09-24 new-PC review: the password was shown once, in the middle of pip output;
bootstrap.py, start_all.ps1 and quickstart sent the operator to scripts/copilot_studio_values.ps1
to "re-read" it, and that script printed only the URL, the header and the Bearer. The automatic
repair after a carried .env minted a new password nobody could read. So:

  * `repair_unlock.py --current` decrypts .env's value with tools.secret_store's
    unlock_password_from_env -- the server's own gate function -- and prints it;
  * copilot_studio_values.ps1 prints what that returns;
  * a repair shows the new password to a PERSON (a console, or --show) and still never puts it
    on the piped stdout start_all.ps1 captures (CodeQL alert #32, which is why it was removed).

DPAPI is Windows-only, and so are these.
"""
from __future__ import annotations

import os
import shutil
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from tools import childproc  # noqa: E402

REPAIR = os.path.join(REPO, "scripts", "repair_unlock.py")

_POWERSHELL = (
    shutil.which("powershell")
    or (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") else None)
)

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


def test_current_prints_the_password_this_account_can_open(tmp_path):
    p = _env(tmp_path, "MCP_API_KEY=k\nMCP_UNLOCK_PASSWORD_PROTECTED=%s\n"
             % _protect("known-pw-1234"))
    assert _run("--current", p) == ["password:known-pw-1234"]


def test_current_says_undecryptable_rather_than_absent(tmp_path):
    p = _env(tmp_path, "MCP_UNLOCK_PASSWORD_PROTECTED=dpapi:AAAAbm90LWEtYmxvYg==\n")  # gitleaks:allow -- base64 of "not-a-blob", a deliberately-undecryptable fixture, not a secret
    out = _run("--current", p)
    assert len(out) == 1 and out[0].startswith("undecryptable:"), out


def test_current_says_unset(tmp_path):
    p = _env(tmp_path, "MCP_API_KEY=k\n")
    out = _run("--current", p)
    assert len(out) == 1 and out[0].startswith("unset:"), out


def test_a_repair_under_capture_hides_the_value_and_current_then_shows_it(tmp_path):
    """start_all's shape: stdout piped, no --show. The verdict names where to read it, and that
    place now does show it -- the value it shows is the one the repair minted."""
    p = _env(tmp_path, "MCP_API_KEY=k\nMCP_UNLOCK_PASSWORD_PROTECTED=dpapi:AAAAZm9yZWlnbg==\n")
    out = _run(p)
    assert len(out) == 1 and out[0].startswith("repaired:"), out
    assert "copilot_studio_values" in out[0]
    shown = _run("--current", p)
    assert len(shown) == 1 and shown[0].startswith("password:"), shown
    new_pw = shown[0].split(":", 1)[1]
    assert len(new_pw) >= 16 and new_pw not in out[0]


def test_a_repair_with_show_prints_the_new_password(tmp_path):
    p = _env(tmp_path, "MCP_UNLOCK_PASSWORD_PROTECTED=dpapi:AAAAZm9yZWlnbg==\n")
    out = _run("--show", p)
    assert out[0].startswith("repaired:") and out[1].startswith("password:"), out
    assert _run("--current", p) == [out[1]]


def test_the_reveal_follows_the_reader():
    import repair_unlock as R

    class S:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

    assert R._reveal_allowed(False, S(True)) is True        # a person at a console
    assert R._reveal_allowed(False, S(False)) is False      # start_all's pipe
    assert R._reveal_allowed(True, S(False)) is True        # explicit --show


@pytest.mark.skipif(not _POWERSHELL, reason="needs Windows PowerShell")
def test_copilot_studio_values_prints_the_unlock_password(tmp_path):
    """Run the real script in a throwaway repo: it must print the decrypted value."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "tools").mkdir()
    for rel in ("scripts/copilot_studio_values.ps1", "scripts/repair_unlock.py",
                "tools/__init__.py", "tools/secret_store.py", "tools/env_portability.py"):
        shutil.copy(os.path.join(REPO, rel), root / rel)
    (root / ".env").write_text("MCP_API_KEY=bearer-abc\nMCP_UNLOCK_PASSWORD_PROTECTED=%s\n"
                               % _protect("visible-pw-5678"), encoding="utf-8")
    env = dict(os.environ)
    # no .venv in the throwaway repo: the script falls back to `python` on PATH -- this one
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    proc = childproc.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(root / "scripts" / "copilot_studio_values.ps1")],
                         env=env, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Unlock password  :  visible-pw-5678" in proc.stdout, proc.stdout  # gitleaks:allow -- fixture password minted by this test, not a real credential

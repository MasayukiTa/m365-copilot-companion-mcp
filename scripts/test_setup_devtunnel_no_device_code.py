# -*- coding: utf-8 -*-
r"""setup_devtunnel.ps1 must never use device-code sign-in, and must open interactive
sign-in in a VISIBLE console window (root-cause fix for the 2026-11-11 outage, cause 4 and
goal (c)).

WHY THESE ARE SOURCE CHECKS, NOT A LIVE RUN. The live-run harness in
test_setup_devtunnel_access_and_identity.py only executes on Windows PowerShell (its whole
module is skipped when os.name != 'nt'), so on the Linux CI runner it proves nothing. The
property we must guarantee -- that no code path can reach `devtunnel user login -d`, and that
the interactive login is launched in a window the user can see -- is a property of the SOURCE,
so it is asserted by reading the script. These assertions run on every CI platform.

What the outage showed (measured 2026-11-11):
  * This tenant's Entra FORBIDS device-code login; `devtunnel user login -d` blocks ~15 min
    then fails with "Verification code expired".
  * The old code launched `devtunnel user login` with `Start-Process -WindowStyle Hidden`,
    which fails with "A window handle must be configured" (an interactive sign-in needs a
    real console) and then SILENTLY fell back to the blocked device-code flow.
  * The fix: launch interactive login in a VISIBLE console window and never invoke `-d`.
"""
from __future__ import annotations

import os
import re

SETUP_PS1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_devtunnel.ps1")


def _source() -> str:
    with open(SETUP_PS1, encoding="utf-8") as fh:
        return fh.read()


def _code_lines() -> list[str]:
    """Script lines with full-line and trailing comments stripped, so assertions about what
    the script DOES are not fooled by prose in a comment that merely describes device code.
    A '#' inside a double-quoted string is rare in this file's Write-Host lines and none of
    the checks below depend on such a line, so a simple split is enough."""
    out = []
    for raw in _source().splitlines():
        s = raw.strip()
        if s.startswith("#"):
            continue
        code = raw.split("#", 1)[0] if "#" in raw else raw
        out.append(code)
    return out


def test_no_devicecode_parameter_remains():
    """The -DeviceCode switch is gone: it only existed to force `user login -d`."""
    src = _source()
    assert not re.search(r"\[switch\]\s*\$DeviceCode", src), \
        "the -DeviceCode param must be removed; it only forced device-code login"
    assert "$DeviceCode" not in src, "$DeviceCode must not be referenced anywhere"


def test_no_code_path_runs_device_code_login():
    """No executable line invokes `devtunnel user login -d` (or `login --device-code`).
    Prose comments that NAME device code to explain why it is avoided are allowed; executable
    statements that RUN it are not."""
    offenders = []
    for line in _code_lines():
        # Only lines that actually INVOKE the CLI count: the call operator `&`, a Start-Process,
        # or the `Dt` wrapper. A Write-Host string that merely spells out "login (NOT -d)" as
        # guidance is not a code path that runs it.
        invokes = ("& $script:DtExe" in line or "& $DtExe" in line
                   or "Start-Process" in line
                   or re.search(r"(?<![A-Za-z])Dt\s+(?:user\s+)?login\b", line))
        if not invokes:
            continue
        if re.search(r"\blogin\b.*(?:\s-d\b|--device-code\b)", line):
            offenders.append(line.strip())
    assert not offenders, "device-code login is reachable from code: %r" % offenders


def test_interactive_login_opens_a_visible_console():
    """The one Start-Process that launches `user login` must be VISIBLE (-WindowStyle Normal),
    not Hidden. A hidden window fails with 'A window handle must be configured' and used to
    fall back to device code."""
    code = "\n".join(_code_lines())
    # Find every Start-Process line that launches the devtunnel CLI for a user login.
    login_launches = [
        ln for ln in _code_lines()
        if "Start-Process" in ln and '"user"' in ln and '"login"' in ln
    ]
    assert login_launches, "expected a Start-Process that launches 'user login'"
    for ln in login_launches:
        assert "-WindowStyle Normal" in ln, \
            "interactive login must open a VISIBLE console (-WindowStyle Normal): %r" % ln.strip()
        assert "-WindowStyle Hidden" not in ln, \
            "interactive login must not be hidden: %r" % ln.strip()
    # And no login launch anywhere is hidden.
    assert not [
        ln for ln in _code_lines()
        if "Start-Process" in ln and '"login"' in ln and "-WindowStyle Hidden" in ln
    ], "a hidden Start-Process still launches login"


def test_alert_names_visible_console_and_disclaims_device_code():
    """The user-facing sign-in messaging must say a visible window opened and that this is NOT
    a device code, so the operator knows which cause they are looking at (goal (c): when
    interactive login is needed, open it in a visible console window and say so)."""
    src = _source()
    assert re.search(r"(?i)visible console", src), \
        "the sign-in message must tell the user a visible console window opened"
    assert re.search(r"(?i)not a device code|NOT -d|device code is blocked", src), \
        "the sign-in message must make clear device code is not used / is blocked"


def test_the_hidden_login_start_process_is_gone():
    """Guards the exact regression: `Start-Process ... user, login ... -WindowStyle Hidden`.
    The host launch (section 1) stays hidden; only the LOGIN launch changed."""
    src = _source()
    assert 'Start-Process $script:DtExe -ArgumentList @("user", "login") -WindowStyle Hidden' \
        not in src, "the hidden login launch must be gone (it fails with no window handle)"

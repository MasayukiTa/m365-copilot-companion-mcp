# -*- coding: utf-8 -*-
r"""The "a generated tunnel name is exempt from the identifying-name check" rule must give the
SAME answer in PowerShell and in Python.

BACKGROUND (D29 of the new-PC install review; consolidated 2026-09-24). Three copies of this
one rule existed: Test-GeneratedTunnelName in scripts/setup_devtunnel.ps1,
Test-GeneratedTunnelNameDoctor in scripts/doctor.ps1 (byte-for-byte identical except its name),
and _is_generated_tunnel_name in scripts/bootstrap.py. The two PowerShell copies are now ONE
function, Test-GeneratedTunnelName in scripts/tunnel_name_util.ps1, which both
setup_devtunnel.ps1 and doctor.ps1 dot-source. bootstrap.py's Python version stays separate --
a different language cannot dot-source a .ps1 file -- which is exactly the situation where two
independently-maintained implementations of "the same rule" can quietly drift apart without
either language's own tests noticing. This test is the thing that would notice: it runs BOTH
implementations over one shared table of names and asserts every verdict matches.

WHY tunnel_name_util.ps1 CAN BE DOT-SOURCED DIRECTLY, UNLIKE supervisor.ps1/heal_tunnel.ps1
elsewhere in this repo. Its own header comment says so: "this file has NO top-level side
effects ... so dot-sourcing it ... never runs real process/.env I/O and needs no dot-source
guard." No balanced-brace extraction is needed here -- the whole file is loaded as-is.

TABLE COVERAGE: generated names with this machine's real suffix (both the current 8-hex scheme
and the legacy 6-hex one), a generated name carrying some OTHER machine's suffix, the bare
default with no suffix at all, malformed suffix shapes (too many segments, non-hex characters,
wrong lengths), and names that are NOT generated at all -- including the exact usernames
("pan", "com") whose presence inside a generated name used to cause a false positive before
D29's fix (see both source files' own comments), plus a fabricated "real" identifying name.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO, "scripts")
sys.path.insert(0, REPO)
sys.path.insert(0, SCRIPTS_DIR)

from tools import childproc  # noqa: E402  (see: repository ratchet against text=True)
import bootstrap  # noqa: E402

_POWERSHELL = (
    shutil.which("powershell")
    or shutil.which("powershell.exe")
    or (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") else None)
)

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not _POWERSHELL,
    reason="tunnel_name_util.ps1 and this test are Windows/PowerShell-only "
           "(os.name=%r, powershell found=%r)" % (os.name, bool(_POWERSHELL)),
)

TUNNEL_NAME_UTIL_PS1 = os.path.join(SCRIPTS_DIR, "tunnel_name_util.ps1")


def _names_table() -> list[str]:
    """One shared table both implementations are run over. Order is not significant; each
    name is checked independently."""
    this_machine_suffix = bootstrap._machine_suffix()
    legacy_suffix = bootstrap._legacy_machine_suffix()
    default = bootstrap.DEFAULT_TUNNEL_NAME
    return [
        "",
        "   ",
        default,
        default.upper(),
        "%s-%s" % (default, this_machine_suffix),          # this machine, current 8-hex scheme
        "%s-%s" % (default, legacy_suffix),                 # this machine, legacy 6-hex scheme
        "%s-deadbeef" % default,                             # SOME OTHER machine's 8-hex suffix
        "%s-cafe01" % default,                               # some other machine's 6-hex suffix
        "%s-%s-%s" % (default, legacy_suffix, this_machine_suffix),  # two stacked suffixes (ok)
        "%s-%s-%s-%s" % (default, legacy_suffix, this_machine_suffix, "abcdef"),  # three (too many)
        "%s-" % default,                                     # trailing dash, empty suffix
        "%s-pan" % default,                                  # non-hex short suffix (username-shaped)
        "%s-com" % default,                                  # non-hex short suffix (username-shaped)
        "%s-xyz123" % default,                                # 6 chars but not all hex ('x','y','z')
        "pan",                                                # bare username, not generated at all
        "com",
        "some-companys-real-tunnel-name",                    # a fabricated "real" identifying name
        "%s-extra-suffix-not-hex-at-all" % default,
    ]


_DRIVER = r'''param(
    [Parameter(Mandatory=$true)][string]$NamesJson,
    [Parameter(Mandatory=$true)][string]$OutFile
)
$ErrorActionPreference = "Stop"
. "%s"
$names = $NamesJson | ConvertFrom-Json
$results = [ordered]@{}
$i = 0
foreach ($n in $names) {
    $verdict = Test-GeneratedTunnelName $n
    $results["$i"] = [bool]$verdict
    $i++
}
$results | ConvertTo-Json | Set-Content -Path $OutFile -Encoding UTF8
''' % TUNNEL_NAME_UTIL_PS1.replace("\\", "\\\\")


def _powershell_verdicts(names: list[str], tmp_path) -> list[bool]:
    driver_path = os.path.join(str(tmp_path), "driver.ps1")
    names_path = os.path.join(str(tmp_path), "names.json")
    out_path = os.path.join(str(tmp_path), "out.json")
    with open(driver_path, "w", encoding="utf-8") as fh:
        fh.write(_DRIVER)
    with open(names_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(names))

    proc = childproc.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver_path,
         "-NamesJson", Path(names_path).read_text(encoding="utf-8"), "-OutFile", out_path],
        timeout=60,
        creationflags=childproc.headless_creationflags(),
    )
    assert proc.returncode == 0, (
        "driver powershell exited %s\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (proc.returncode, proc.stdout, proc.stderr)
    )
    with open(out_path, "r", encoding="utf-8-sig") as fh:
        raw = json.load(fh)
    # ConvertTo-Json on a 1-entry ordered dict returns a scalar-shaped object in some PS5.1
    # builds rather than a single-key object; the table here always has >1 entries, so that
    # edge case cannot occur, but assert the shape rather than silently mis-reading it.
    assert isinstance(raw, dict), "unexpected driver output shape: %r" % (raw,)
    return [bool(raw[str(i)]) for i in range(len(names))]


def test_powershell_and_python_agree_on_every_name_in_the_table(tmp_path):
    names = _names_table()
    ps_verdicts = _powershell_verdicts(names, tmp_path)
    py_verdicts = [bootstrap._is_generated_tunnel_name(n) for n in names]

    mismatches = [
        (n, py, ps) for n, py, ps in zip(names, py_verdicts, ps_verdicts) if py != ps
    ]
    assert not mismatches, (
        "PowerShell's Test-GeneratedTunnelName (tunnel_name_util.ps1) and Python's "
        "_is_generated_tunnel_name (bootstrap.py) disagree on:\n" +
        "\n".join("  name=%r python=%r powershell=%r" % m for m in mismatches)
    )


def test_the_table_actually_exercises_both_true_and_false(tmp_path):
    """A table that is all-True or all-False would make the agreement check above pass
    vacuously (e.g. if both implementations were stubbed to always return the same constant).
    This is the sanity check that the table is doing its job."""
    names = _names_table()
    py_verdicts = [bootstrap._is_generated_tunnel_name(n) for n in names]
    assert any(py_verdicts), "table has no name Python considers generated"
    assert not all(py_verdicts), "table has no name Python considers NOT generated"

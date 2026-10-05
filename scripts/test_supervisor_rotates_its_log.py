"""supervisor.ps1 rotates its own log: at 5 MB the log becomes <log>.1 (one generation kept).

The function is extracted from the real script and run in a real PowerShell against a temp file,
so what is tested is the shipped text, not a Python re-implementation of it.
"""
import os
import shutil
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

PS1 = os.path.join(REPO, "scripts", "supervisor.ps1")
_PS = shutil.which("powershell") or shutil.which("powershell.exe")

with open(PS1, "r", encoding="utf-8-sig") as _fh:
    SRC = _fh.read()


def _function_block():
    i = SRC.index("function Invoke-SupervisorLogRotation")
    depth, start = 0, SRC.index("{", i)
    for k in range(start, len(SRC)):
        if SRC[k] == "{":
            depth += 1
        elif SRC[k] == "}":
            depth -= 1
            if depth == 0:
                return SRC[i:k + 1]
    raise AssertionError("unbalanced")


def test_rotation_is_in_the_script_and_ascii_and_called_from_write_log_and_startup():
    SRC.encode("ascii", errors="ignore")
    block = _function_block()
    assert all(ord(c) < 128 for c in block)
    wl = SRC[SRC.index("function Write-Log"):]
    wl = wl[:wl.index("\n}\n")]
    assert "Invoke-SupervisorLogRotation" in wl
    before_write_log = SRC[:SRC.index("function Write-Log")]
    assert "[void](Invoke-SupervisorLogRotation -Path $Log)" in before_write_log


@pytest.mark.skipif(os.name != "nt" or not _PS, reason="needs Windows PowerShell")
def test_a_big_log_rotates_a_small_one_does_not(tmp_path):
    script = tmp_path / "t.ps1"
    log = tmp_path / "sup.log"
    old = tmp_path / "sup.log.1"
    big, small = tmp_path / "big.log", tmp_path / "small.log"
    big.write_bytes(b"b" * (5 * 1048576 + 10))
    small.write_bytes(b"s" * 100)
    old.write_bytes(b"previous generation")
    script.write_text(
        _function_block() + "\n"
        "$r1 = Invoke-SupervisorLogRotation -Path '%s'\n"
        "$r2 = Invoke-SupervisorLogRotation -Path '%s'\n"
        "$r3 = Invoke-SupervisorLogRotation -Path '%s'\n"
        "Write-Output (\"$r1 $r2 $r3\")\n" % (big, small, tmp_path / "missing.log"),
        encoding="utf-8")
    # rotate `big` over an existing .1 as well
    shutil.copy(str(big), str(log))
    big_old = tmp_path / "big.log.1"
    big_old.write_bytes(b"previous generation")
    out = childproc.run([_PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-File", str(script)], timeout=120)
    assert out.stdout.split() == ["True", "False", "False"], (out.stdout, out.stderr)
    assert not big.exists() and big_old.stat().st_size == 5 * 1048576 + 10
    assert small.read_bytes() == b"s" * 100

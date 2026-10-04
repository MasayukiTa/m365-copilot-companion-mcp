from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tools.childproc import decode, headless_creationflags

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "win" / "gui_submit_lock.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="requires Windows PowerShell")


def _harness(tmp_path: Path) -> Path:
    p = tmp_path / "lock_harness.ps1"
    p.write_text(
        "param([string]$Repo,[int]$HoldMs,[int]$TimeoutSec)\n"
        f". '{str(HELPER).replace(chr(39), chr(39)+chr(39))}'\n"
        "$lease = Enter-GuiSubmitLock -RepoRoot $Repo -TimeoutSeconds $TimeoutSec\n"
        "try { Write-Output 'ACQUIRED'; Start-Sleep -Milliseconds $HoldMs }\n"
        "finally { Exit-GuiSubmitLock $lease }\n",
        encoding="utf-8-sig",
    )
    return p


def _popen(script: Path, repo: Path, hold_ms: int, timeout_s: int):
    flags = headless_creationflags()
    return subprocess.Popen(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
         str(script), "-Repo", str(repo), "-HoldMs", str(hold_ms), "-TimeoutSec", str(timeout_s)],
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=flags,
    )


def test_gui_submit_file_lock_serializes_two_processes(tmp_path):
    script = _harness(tmp_path)
    repo = tmp_path / "same_repo"
    repo.mkdir()

    first = _popen(script, repo, 1800, 5)
    assert decode(first.stdout.readline()).strip() == "ACQUIRED"

    t0 = time.monotonic()
    second = _popen(script, repo, 0, 5)
    out2_raw, err2_raw = second.communicate(timeout=8)
    out2, err2 = decode(out2_raw), decode(err2_raw)
    waited = time.monotonic() - t0
    out1_raw, err1_raw = first.communicate(timeout=8)
    out1, err1 = decode(out1_raw), decode(err1_raw)

    assert first.returncode == 0, (out1, err1)
    assert second.returncode == 0, (out2, err2)
    assert "ACQUIRED" in out2
    assert waited >= 1.2, f"second submitter did not wait for the first owner: {waited:.3f}s"


def test_gui_submit_file_lock_times_out_without_stealing_owner(tmp_path):
    script = _harness(tmp_path)
    repo = tmp_path / "same_repo_timeout"
    repo.mkdir()

    first = _popen(script, repo, 2600, 5)
    assert decode(first.stdout.readline()).strip() == "ACQUIRED"
    second = _popen(script, repo, 0, 1)
    out2_raw, err2_raw = second.communicate(timeout=5)
    out2, err2 = decode(out2_raw), decode(err2_raw)
    out1_raw, err1_raw = first.communicate(timeout=8)
    out1, err1 = decode(out1_raw), decode(err1_raw)

    assert first.returncode == 0, (out1, err1)
    assert second.returncode != 0
    assert "another GUI submitter owns FleetCockpit goalInput" in (out2 + err2)

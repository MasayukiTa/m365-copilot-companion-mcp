"""PowerShell-aware execution.

`shell_exec` already runs commands via the OS default shell (cmd.exe on
Windows). This module adds:

  - `pwsh_exec`: dedicated PowerShell execution with sensible defaults
    (NoProfile + NonInteractive + ExecutionPolicy Bypass) so PowerShell
    snippets actually work end-to-end without the agent having to wrap
    them itself.
  - `pwsh_exec_file`: run a .ps1 file under the same defaults.

PowerShell on Windows defaults to Windows PowerShell 5.1 (powershell.exe).
If PowerShell 7+ (pwsh.exe) is installed, pass `use_core=True` to use it.
"""
import os
import shutil
import subprocess
from typing import Optional

from .file_ops import _validate_path
from .security import require_unlocked


def _resolve_shell(use_core: bool) -> Optional[str]:
    if use_core:
        return shutil.which("pwsh.exe") or shutil.which("pwsh")
    return (
        shutil.which("powershell.exe")
        or shutil.which("powershell")
        or shutil.which("pwsh.exe")
        or shutil.which("pwsh")
    )


def _format_result(args: list[str], result: subprocess.CompletedProcess) -> str:
    head = "$ " + " ".join(args[:3]) + (" ..." if len(args) > 3 else "") + "\n"
    out = ""
    if result.stdout:
        out += f"[stdout]\n{result.stdout}"
    if result.stderr:
        out += f"[stderr]\n{result.stderr}"
    if result.returncode != 0:
        out += f"\n[returncode: {result.returncode}]"
    return head + (out or "(no output)")


def pwsh_exec(
    script: str,
    timeout: int = 60,
    working_dir: Optional[str] = None,
    use_core: bool = False,
) -> str:
    """Run a PowerShell snippet and return stdout/stderr.

    Args:
        script: PowerShell script body (one or more lines).
        timeout: Timeout in seconds.
        working_dir: Optional working directory under the allowed base.
        use_core: True to use PowerShell 7 (pwsh.exe) if installed, else falls
            back to Windows PowerShell 5.1 (powershell.exe).
    """
    locked = require_unlocked()
    if locked:
        return locked
    from . import contract_gate as _cg
    if _cg.destructive_shell(script):
        _g = _cg.check_op("shell_destructive", script[:200])
        if _g is not None:
            return _g
    try:
        shell = _resolve_shell(use_core)
        if not shell:
            return (
                "[pwsh_exec error: neither powershell.exe nor pwsh.exe found on PATH]"
            )
        cwd = os.getcwd()
        if working_dir:
            p = _validate_path(working_dir)
            if not p.is_dir():
                return f"[pwsh_exec error: not a directory: {p}]"
            cwd = str(p)
        args = [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-Command", script,
        ]
        r = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return _format_result(args, r)
    except subprocess.TimeoutExpired:
        return f"[pwsh_exec timeout: exceeded {timeout}s]"
    except Exception as e:
        return f"[pwsh_exec error: {type(e).__name__}: {e}]"


def pwsh_exec_file(
    path: str,
    args: Optional[list[str]] = None,
    timeout: int = 120,
    working_dir: Optional[str] = None,
    use_core: bool = False,
) -> str:
    """Run an existing .ps1 file under PowerShell.

    Args:
        path: Path to the .ps1 script (under the allowed base).
        args: Optional positional arguments forwarded to the script.
        timeout: Timeout in seconds.
        working_dir: Optional working directory. Defaults to the script's
            parent directory.
        use_core: True to use PowerShell 7 (pwsh.exe) if available.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        shell = _resolve_shell(use_core)
        if not shell:
            return "[pwsh_exec_file error: no PowerShell on PATH]"
        p = _validate_path(path)
        if not p.is_file():
            return f"[pwsh_exec_file error: not a file: {p}]"
        if p.suffix.lower() != ".ps1":
            return "[pwsh_exec_file error: path must be a .ps1 file]"
        cwd = str(_validate_path(working_dir)) if working_dir else str(p.parent)
        cmd = [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", str(p),
        ] + [str(a) for a in (args or [])]
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return _format_result(cmd, r)
    except subprocess.TimeoutExpired:
        return f"[pwsh_exec_file timeout after {timeout}s]"
    except Exception as e:
        return f"[pwsh_exec_file error: {type(e).__name__}: {e}]"


def shell_which() -> str:
    """Report which shells are available on this host (cmd / powershell / pwsh / bash)."""
    candidates = ["cmd.exe", "powershell.exe", "pwsh.exe", "pwsh", "bash", "wsl.exe"]
    lines = []
    for c in candidates:
        path = shutil.which(c)
        lines.append(f"  {c:<14}  {path or '(not found)'}")
    return "\n".join(lines)


#: This tool runs caller-supplied code, so the evidence trace cannot see what it did:
#: it records that the call happened and nothing about its effects. Declared HERE rather
#: than in a list inside evidence_trace.py, because a list in another file is a list that
#: goes stale the first time an executor is added -- which is exactly what happened.
pwsh_exec.evidence_opaque = True
pwsh_exec_file.evidence_opaque = True

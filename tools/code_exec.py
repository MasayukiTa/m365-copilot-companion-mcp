import os
import subprocess
import sys
import tempfile
from typing import Optional

from ._subproc import sanitized_child_env
from .file_ops import _validate_path
from .security import require_unlocked


def _working_dir(path: Optional[str]) -> str:
    if not path:
        return os.getcwd()
    p = _validate_path(path)
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    return str(p)


def run_python(
    code: str,
    timeout: int = 60,
    working_dir: Optional[str] = None,
) -> str:
    """Run Python code in a temporary file and return stdout, stderr, and return code.

    NOT a sandbox. Under an active autonomy contract, destructive file ops in the
    source (os.remove/unlink/rmdir, shutil.rmtree/move, pathlib unlink/rmdir,
    truncating open(...,'w'), and os.system/subprocess escape hatches) are routed
    through the contract gate (op_class 'shell_destructive') for approval — this is
    detection-based, so it can miss obfuscated code. Treat run_python as not fully
    sandboxed when granting long-running autonomy.

    Args:
        code: Python source code to execute.
        timeout: Maximum execution time in seconds.
        working_dir: Optional working directory under the allowed base.

    If the script produces an artifact, verify it before declaring success: read_image
    for a saved plot/image, or verify_python / verify_file_contains for a computed result.
    """
    locked = require_unlocked()
    if locked:
        return locked
    from . import contract_gate as _cg
    # Gate destructive ops whether expressed as shell text OR as Python source. Both are
    # routed through the existing 'shell_destructive' op_class so any contract that already
    # asks-before destructive shell also covers destructive Python (no schema change needed).
    if _cg.destructive_shell(code) or _cg.destructive_python(code):
        _g = _cg.check_op("shell_destructive", code[:200])
        if _g is not None:
            return _g
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp_path = f.name

        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=_working_dir(working_dir),
            shell=False,
            env=sanitized_child_env(),
        )
        output = ""
        if result.stdout:
            output += f"[stdout]\n{result.stdout}"
        if result.stderr:
            output += f"[stderr]\n{result.stderr}"
        if result.returncode != 0:
            output += f"\n[returncode: {result.returncode}]"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"[timeout: exceeded {timeout} seconds]"
    except Exception as e:
        return f"[run_python error: {type(e).__name__}: {e}]"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def shell_exec(
    command: str,
    timeout: int = 30,
    working_dir: Optional[str] = None,
) -> str:
    """Run a shell command and return stdout, stderr, and return code.

    Args:
        command: Command line to execute.
        timeout: Maximum execution time in seconds.
        working_dir: Optional working directory under the allowed base.
    """
    locked = require_unlocked()
    if locked:
        return locked
    from . import contract_gate as _cg
    if _cg.destructive_shell(command):
        _g = _cg.check_op("shell_destructive", command[:200])
        if _g is not None:
            return _g
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=_working_dir(working_dir),
            env=sanitized_child_env(),
        )
        output = ""
        if result.stdout:
            output += f"[stdout]\n{result.stdout}"
        if result.stderr:
            output += f"[stderr]\n{result.stderr}"
        if result.returncode != 0:
            output += f"\n[returncode: {result.returncode}]"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"[timeout: exceeded {timeout} seconds]"
    except Exception as e:
        return f"[shell_exec error: {type(e).__name__}: {e}]"

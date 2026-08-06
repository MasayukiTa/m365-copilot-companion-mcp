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


def _decode(raw: bytes) -> str:
    """子プロセスの出力を、落とさずに文字へ直す。

    text=True にすると、その場のコードページ（日本語Windowsなら cp932）で復号する。
    UTF-8 で出力するスクリプトを走らせると、そこで例外になって出力が丸ごと消える。
    実際、日本語を出す調査スクリプトが returncode 0 なのに「cp932 で復号できない」で
    落ち、呼び出し側はファイルへ迂回する羽目になった。

    UTF-8 を先に試し、駄目ならその場のコードページへ落とす。どちらでも読めない字は
    捨てずに置き換える。出力の一部が化けるより、出力ごと消えるほうが困る。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    import locale
    fallback = locale.getpreferredencoding(False) or "utf-8"
    return raw.decode(fallback, errors="replace")


def _format_output(result, label: str) -> str:
    out = ""
    stdout, stderr = _decode(result.stdout), _decode(result.stderr)
    if stdout:
        out += f"[stdout]\n{stdout}"
    if stderr:
        out += f"[stderr]\n{stderr}"
    if result.returncode != 0:
        out += f"\n[returncode: {result.returncode}]"
    return out or "(no output)"


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
            timeout=timeout,
            cwd=_working_dir(working_dir),
            shell=False,
            env=sanitized_child_env(),
        )
        return _format_output(result, "run_python")
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
            timeout=timeout,
            cwd=_working_dir(working_dir),
            env=sanitized_child_env(),
        )
        return _format_output(result, "shell_exec")
    except subprocess.TimeoutExpired:
        return f"[timeout: exceeded {timeout} seconds]"
    except Exception as e:
        return f"[shell_exec error: {type(e).__name__}: {e}]"

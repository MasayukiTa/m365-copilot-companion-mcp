import os
import platform
import shutil
import subprocess
import sys
import sysconfig
from importlib.metadata import distributions
from pathlib import Path
from typing import Optional

from .security import require_unlocked

ALLOWED_PIP_FLAGS = {"--upgrade", "-U", "--pre", "--no-deps", "--force-reinstall", "--no-cache-dir"}


def env_info() -> str:
    """Return a snapshot of the Python environment: version, paths, installed packages.

    Use this before calling pip_install to check what is already available.
    """
    try:
        pkgs = sorted(
            (dist.metadata["Name"], dist.version)
            for dist in distributions()
        )
        lines = [
            f"python: {sys.version.split()[0]} ({sys.executable})",
            f"platform: {platform.platform()}",
            f"cpu: {platform.processor() or '(unknown)'}",
            f"cwd: {os.getcwd()}",
            f"prefix: {sys.prefix}",
            f"base_prefix: {sys.base_prefix}",
            f"venv active: {sys.prefix != sys.base_prefix}",
            f"site-packages: {sysconfig.get_paths().get('purelib', '?')}",
            f"pip: {_pip_version()}",
            f"packages: {len(pkgs)} installed",
            "",
            "--- installed packages ---",
        ]
        for name, ver in pkgs:
            lines.append(f"  {name}=={ver}")
        return "\n".join(lines)
    except Exception as e:
        return f"[env_info error: {type(e).__name__}: {e}]"


def _pip_version() -> str:
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip().split()[1] if r.returncode == 0 else "(error)"
    except Exception:
        return "(unavailable)"


def pip_install(
    packages: list[str],
    extra_flags: Optional[list[str]] = None,
    timeout: int = 180,
) -> str:
    """Install Python packages into the current venv via pip.

    Only specific safe flags are allowed (see ALLOWED_PIP_FLAGS in source).
    Each package name is validated to start with an alphanumeric character.

    Args:
        packages: List of package specifiers, e.g. ["pandas", "scipy>=1.10"].
        extra_flags: Optional list of pip flags from the allowlist.
        timeout: Subprocess timeout in seconds.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        if not isinstance(packages, list) or not packages:
            return "[pip_install error: 'packages' must be a non-empty list]"
        cleaned: list[str] = []
        for raw in packages:
            if not isinstance(raw, str) or not raw:
                return f"[pip_install error: invalid package spec: {raw!r}]"
            if not raw[0].isalnum():
                return f"[pip_install error: package spec must start alphanumeric: {raw!r}]"
            cleaned.append(raw)
        flags: list[str] = []
        if extra_flags:
            for f in extra_flags:
                if f not in ALLOWED_PIP_FLAGS:
                    return f"[pip_install error: flag {f!r} not in allowlist {sorted(ALLOWED_PIP_FLAGS)}]"
                flags.append(f)
        cmd = [sys.executable, "-m", "pip", "install", *flags, *cleaned]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        head = f"$ {' '.join(cmd[3:])}\n"
        out = (r.stdout or "")[-4000:]
        err = (r.stderr or "")[-2000:]
        body = head + out
        if err:
            body += f"\n[stderr]\n{err}"
        if r.returncode != 0:
            body += f"\n[returncode: {r.returncode}]"
        return body
    except subprocess.TimeoutExpired:
        return f"[pip_install timeout after {timeout}s]"
    except Exception as e:
        return f"[pip_install error: {type(e).__name__}: {e}]"


def which(name: str) -> str:
    """Return the absolute path of an executable on PATH, or a not-found message."""
    try:
        found = shutil.which(name)
        if found:
            return found
        return f"[which: {name} not found on PATH]"
    except Exception as e:
        return f"[which error: {type(e).__name__}: {e}]"

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

# TLS args for pip installs: the SAME definition scripts/bootstrap.py uses for its own
# `pip install` calls (INST-09). That function verifies against this machine's exported CA
# bundle (ca_bundle.ps1) or pip's own truststore, and only falls back to --trusted-host (TLS
# verification OFF) when the operator has explicitly opted in with SETUP_PIP_TRUSTED_HOST=1.
# This used to be its own hardcoded, unconditional --trusted-host list here -- a second copy
# of the workaround bootstrap.py has since replaced with a verified default. Import, don't
# copy, so there is exactly one place that decides how pip talks to PyPI.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from scripts.bootstrap import pip_tls_args  # noqa: E402


def _trusted_host_args() -> list[str]:
    """pip TLS args for this install (see scripts.bootstrap.pip_tls_args). Kept as a thin
    wrapper -- not renamed -- so callers and tests do not need to change."""
    args, _note = pip_tls_args()
    return args


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
            capture_output=True, text=True, errors="replace", timeout=10,
        )
        return r.stdout.strip().split()[1] if r.returncode == 0 else "(error)"
    except Exception:
        return "(unavailable)"


# The harness's own environment, resolved once. A worker's checkout may hold a venv of its
# own; that is where its dependencies belong.
_HARNESS_PREFIX = os.path.normcase(os.path.abspath(sys.prefix))


def _project_interpreter() -> Optional[str]:
    """The interpreter to install into, or None when only the harness's own is available.

    Looks for a virtual environment in the current working directory -- which for a fleet
    worker is its checkout -- and returns its interpreter. Returns None rather than falling
    back to `sys.executable`, because falling back is what put an open-source project's
    requirements into the server's environment.
    """
    here = os.path.abspath(os.getcwd())
    for name in (".venv", "venv", "env"):
        for rel in (os.path.join("Scripts", "python.exe"), os.path.join("bin", "python")):
            cand = os.path.join(here, name, rel)
            if os.path.isfile(cand):
                if os.path.normcase(os.path.abspath(os.path.dirname(os.path.dirname(cand))))                         != _HARNESS_PREFIX:
                    return cand
    # An explicitly activated environment that is not the harness's is fine too.
    ve = os.environ.get("VIRTUAL_ENV")
    if ve and os.path.normcase(os.path.abspath(ve)) != _HARNESS_PREFIX:
        for rel in (os.path.join("Scripts", "python.exe"), os.path.join("bin", "python")):
            cand = os.path.join(ve, rel)
            if os.path.isfile(cand):
                return cand
    return None


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
        # THE INTERPRETER IS THE HARNESS'S OWN, AND THAT IS THE WHOLE PROBLEM.
        #
        # `sys.executable` here is the venv that runs the MCP server, the tool gateway and
        # the approval gate. On 2026-08-30 07:02 a fleet worker solving an open-source
        # instance installed THAT PROJECT'S requirements through this function: pydantic went
        # 2.13 -> 2.1.0, httpx 0.28.1 -> 0.24.1, and the harness -- which declares
        # httpx>=0.28.1 -- stopped importing. Every test collection died and the server would
        # not have restarted. Nothing was bypassed; this function did exactly what its
        # docstring said, into the wrong environment.
        #
        # An external review put it plainly: a project venv is not a security boundary, and
        # this is not the fix for the general problem -- confinement is. It is the fix for
        # THIS: a worker that means to populate its checkout must not be able to reach the
        # harness by accident, through a tool that was offered to it for that purpose.
        target = _project_interpreter()
        if target is None:
            return ("[pip_install refused: this would install into the harness's own "
                    "environment (%s), which runs the server and the approval gate. "
                    "Create a virtual environment inside the checkout you are working on "
                    "and install into that instead.]" % sys.prefix)
        cmd = [target, "-m", "pip", "install", *_trusted_host_args(), *flags, *cleaned]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout)
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

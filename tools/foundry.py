"""Operator A - tool foundry (foundation).

Lets the framework write a NEW tool module, syntax-validate it, and stage it so
a server restart picks it up. Forged modules are written under tools/auto/.

IMPORTANT activation model: we deliberately do NOT attempt FastMCP hot/runtime
registration (it is version-dependent and risky). Instead we use the
restart-to-activate model: a forged module is importable by the framework
immediately, but Copilot/MCP clients only see new tools after the server is
restarted (and, for Copilot Studio, after the connector is re-synced). The
tools/auto_loader.py:load_auto_tools() hook lets a future startup auto-register
forged callables.
"""
import keyword
import py_compile
import tempfile
from pathlib import Path

from .file_ops import _validate_path
from .security import require_unlocked

AUTO_DIR = Path(__file__).resolve().parent / "auto"


def _safe_name(name: str) -> bool:
    return bool(name) and name.isidentifier() and not keyword.iskeyword(name)


def forge_tool(name: str, code: str) -> str:
    """Write, syntax-check, and stage a new tool module under tools/auto/.

    Validates that `name` is a safe Python identifier, writes `code` to
    tools/auto/<name>.py, then compile-checks it with py_compile. If it compiles
    it is kept; otherwise the file is deleted and the compile error returned.

    ACTIVATION: the forged module is available to the framework immediately by
    import, but Copilot/MCP clients only see new tools after the server is
    restarted (and, for Copilot Studio, after the connector is re-synced). This
    function never restarts the running server.

    Args:
        name: Module/identifier name (a safe Python identifier).
        code: Full Python source of the module to write.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        if not _safe_name(name):
            return f"[forge_tool error: {name!r} is not a safe Python identifier]"
        AUTO_DIR.mkdir(parents=True, exist_ok=True)
        target = _validate_path(str(AUTO_DIR / f"{name}.py"))
        target.write_text(code, encoding="utf-8")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                py_compile.compile(
                    str(target), cfile=str(Path(tmp) / "check.pyc"), doraise=True
                )
        except Exception as ce:
            try:
                target.unlink()
            except OSError:
                pass
            return f"[forge_tool error: did not compile, discarded: {type(ce).__name__}: {ce}]"
        return (
            f"forged tools/auto/{name}.py ({len(code)} chars), syntax OK.\n"
            "Available to the framework on next import; Copilot/MCP clients see it "
            "only after the server is restarted (and Copilot Studio connector re-synced)."
        )
    except Exception as e:
        return f"[forge_tool error: {type(e).__name__}: {e}]"


def forge_list() -> str:
    """List forged modules under tools/auto/ with their first-line docstring."""
    try:
        if not AUTO_DIR.is_dir():
            return "(no forged tools)"
        rows = []
        for p in sorted(AUTO_DIR.glob("*.py")):
            if p.name == "__init__.py":
                continue
            doc = ""
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    s = line.strip().strip('"').strip("'")
                    if s:
                        doc = s
                        break
            except OSError:
                pass
            rows.append(f"{p.stem:<24}  {doc[:70]}")
        if not rows:
            return "(no forged tools)"
        return "\n".join(rows)
    except Exception as e:
        return f"[forge_list error: {type(e).__name__}: {e}]"


def forge_read(name: str) -> str:
    """Return the source of a forged module tools/auto/<name>.py.

    Args:
        name: The forged module name (without .py).
    """
    try:
        if not _safe_name(name):
            return f"[forge_read error: {name!r} is not a safe Python identifier]"
        target = _validate_path(str(AUTO_DIR / f"{name}.py"))
        if not target.is_file():
            return f"[forge_read: no such forged module: {name}]"
        return target.read_text(encoding="utf-8")
    except Exception as e:
        return f"[forge_read error: {type(e).__name__}: {e}]"


def forge_delete(name: str) -> str:
    """Delete a forged module tools/auto/<name>.py.

    Args:
        name: The forged module name (without .py).
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        if not _safe_name(name):
            return f"[forge_delete error: {name!r} is not a safe Python identifier]"
        target = _validate_path(str(AUTO_DIR / f"{name}.py"))
        if not target.is_file():
            return f"[forge_delete: no such forged module: {name}]"
        target.unlink()
        return f"deleted tools/auto/{name}.py (effective for clients after restart)"
    except Exception as e:
        return f"[forge_delete error: {type(e).__name__}: {e}]"

"""Startup hook for auto-registering forged tools (operator A).

load_auto_tools() imports every module under tools/auto/ and returns a list of
(module_name, callable) for top-level functions whose name does not start with
"_". A future main.py can iterate this and register each callable so forged
tools become live after a restart. It is intentionally defensive: a failing
module is skipped, not fatal, and an empty tools/auto/ yields an empty list.
"""
import importlib
import inspect
import pkgutil
from typing import Callable


def load_auto_tools() -> list[tuple[str, Callable]]:
    """Import all modules under tools/auto/ and collect public top-level functions.

    Returns a list of (module_name, callable). Modules that fail to import are
    skipped. Returns an empty list when tools/auto/ is empty.
    """
    found: list[tuple[str, Callable]] = []
    try:
        auto_pkg = importlib.import_module("tools.auto")
    except Exception:
        return found
    pkg_path = getattr(auto_pkg, "__path__", None)
    if not pkg_path:
        return found
    for mod_info in pkgutil.iter_modules(pkg_path):
        if mod_info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"tools.auto.{mod_info.name}")
        except Exception:
            continue
        for attr_name, obj in vars(module).items():
            if attr_name.startswith("_"):
                continue
            if inspect.isfunction(obj) and obj.__module__ == module.__name__:
                found.append((mod_info.name, obj))
    return found

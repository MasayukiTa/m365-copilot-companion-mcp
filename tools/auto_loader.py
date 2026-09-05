"""Startup hook for auto-registering forged tools (operator A).

load_auto_tools() imports every module under tools/auto/ and returns a list of
(module_name, callable) for top-level functions whose name does not start with
"_". A future main.py can iterate this and register each callable so forged
tools become live after a restart. It is intentionally defensive: a failing
module is skipped, not fatal, and an empty tools/auto/ yields an empty list.

THE FOUNDRY WRITES A MODULE AND ITS TESTS SIDE BY SIDE, and "every module under
tools/auto/" collected both. 55 pytest functions and fixtures were published as
live MCP tools next to the 8 real ones -- test_rolling_back_undoes_the_edits,
fake_cell, always -- each one callable by a connected agent, several of which
create repositories and run git. It also defeated the tool cap the client needs:
MCP_TOOL_MAP_MAX was 10 because Copilot Studio stops at 70, and the server was
publishing 72, so the limit that exists to keep unlock and the loop protocol
reachable was being set and then walked past.

So the filter is by NAME and by MARKER, not by taste: a module named like a test
contributes nothing, a function named like a test is skipped wherever it lives,
and a pytest fixture is skipped by the attribute pytest itself stamps on it -- a
fixture has no test_ prefix (fake_cell, repo, both) and is otherwise ordinary.
"""
import importlib
import inspect
import pkgutil
from typing import Callable


def _is_test_module(name: str) -> bool:
    return name.startswith("test_") or name.endswith("_test")


def _is_test_function(name: str, obj) -> bool:
    if name.startswith("test_"):
        return True
    # pytest stamps this on anything decorated with @pytest.fixture. Fixtures are the half
    # of the leak that name-matching alone does not catch.
    return hasattr(obj, "_pytestfixturefunction")


def load_auto_tools(package: str = "tools.auto") -> list[tuple[str, Callable]]:
    """Import all modules under tools/auto/ and collect public top-level functions.

    Returns a list of (module_name, callable). Modules that fail to import are
    skipped. Returns an empty list when tools/auto/ is empty.

    `package` exists so the filtering below can be tested against a package built for the
    test. The default is the one the server loads, and no caller passes anything else.
    """
    found: list[tuple[str, Callable]] = []
    try:
        auto_pkg = importlib.import_module(package)
    except Exception:
        return found
    pkg_path = getattr(auto_pkg, "__path__", None)
    if not pkg_path:
        return found
    for mod_info in pkgutil.iter_modules(pkg_path):
        if mod_info.name.startswith("_") or _is_test_module(mod_info.name):
            continue
        try:
            module = importlib.import_module(f"{package}.{mod_info.name}")
        except Exception:
            continue
        for attr_name, obj in vars(module).items():
            if attr_name.startswith("_") or _is_test_function(attr_name, obj):
                continue
            if inspect.isfunction(obj) and obj.__module__ == module.__name__:
                found.append((mod_info.name, obj))
    return found

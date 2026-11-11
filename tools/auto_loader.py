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
        # A module may opt out of registration by defining `_auto_tools_enabled()`. A gate that
        # returns false OR raises means "publish nothing": the safe reading of a gate that
        # cannot answer is closed. The name starts with "_", so the gate is never itself a tool.
        gate = getattr(module, "_auto_tools_enabled", None)
        if callable(gate):
            try:
                if not gate():
                    continue
            except Exception:
                continue
        # These desktop tools have a fixed public surface. Register the actual
        # callables here; other foundry modules continue to use discovery.
        if package == "tools.auto" and mod_info.name in {
            "desktop_snapshot", "desktop_observe", "desktop_act", "desktop_secure"
        }:
            if mod_info.name == "desktop_snapshot":
                desktop_tools = (module.desktop_state, module.capture_window_by_title)
            elif mod_info.name == "desktop_observe":
                desktop_tools = (module.screen_full_desktop, module.screen_state_report,
                                 module.screen_lock_history, module.screen_state_diff)
            elif mod_info.name == "desktop_act":
                desktop_tools = (module.screen_msg_click, module.screen_msg_text,
                                 module.screen_msg_key, module.screen_msg_close,
                                 module.screen_uia_dump, module.screen_uia_act)
            else:
                desktop_tools = (module.screen_secure_status, module.screen_secure_capture)
            found.extend((mod_info.name, tool) for tool in desktop_tools)
            continue
        for attr_name, obj in vars(module).items():
            if attr_name.startswith("_") or _is_test_function(attr_name, obj):
                continue
            if inspect.isfunction(obj) and obj.__module__ == module.__name__:
                found.append((mod_info.name, obj))
    return found

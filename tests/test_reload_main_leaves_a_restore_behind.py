"""A test file that reloads `main` under a synthetic environment must undo the reload.

THE LEAK THIS GUARDS AGAINST. `importlib.reload(main)` rebinds EVERY module-level name on
`main` -- API_KEY, TOOLS, _ALL_TOOLS -- to whatever the environment says at the moment of the
call. A test that reloads `main` under a synthetic MCP_API_KEY (or MCP_TOOL_MAP) and never
reloads it again leaves that synthetic state on the module for the rest of the pytest session:
73e3624 fixed exactly this for tests/test_exec_via_gateway.py and tests/test_tool_map_pinning.py
(the synthetic key leaked into tests/test_health_route.py's bearer-token checks, hundreds of
tests later), and relay/test_gateway_executes.py had the identical bug independently -- its
module-scoped `gateway` fixture reloaded `main` on setup and never reloaded it back.

Both fixes share one shape: reload again, after the fact, once it is safe to (a fixture's
teardown -- the code after `yield` -- or a `finally` block). A source assertion cannot catch a
name that is not bound yet, but a source SCAN can catch a reload that is never undone: this
walks every .py file in the repo, finds every `importlib.reload(<alias of "main">)` call, and
requires each one to be paired with a later reload of the same module inside the same
function's teardown or finally block. New test files that add a reload without the restore
fail this test instead of leaking silently into whatever runs after them.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_EXCLUDE_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache",
                  ".pytest_cache", "build", "dist"}


def _py_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _main_aliases(tree):
    """Names in this module bound to the `main` module by an `import main[ as X]` statement,
    at any scope (module level or inside a function/fixture)."""
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "main":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _is_reload_call(node, aliases):
    """`importlib.reload(<alias>)` or `reload(<alias>)` (the latter needs `from importlib
    import reload`, which nobody in this repo uses today, but the shape is cheap to allow)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    is_reload_name = (
        (isinstance(func, ast.Attribute) and func.attr == "reload"
         and isinstance(func.value, ast.Name) and func.value.id == "importlib")
        or (isinstance(func, ast.Name) and func.id == "reload")
    )
    if not is_reload_name:
        return False
    if len(node.args) != 1 or not isinstance(node.args[0], ast.Name):
        return False
    return node.args[0].id in aliases


def _reload_calls(tree, aliases):
    return [node for node in ast.walk(tree) if _is_reload_call(node, aliases)]


def _in_finally(target, tree):
    """True if `target` (a Call node) sits inside the finalbody of some ast.Try in `tree`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for fin_node in node.finalbody:
                if target in ast.walk(fin_node):
                    return True
    return False


def _restoring_reload_exists(func_node, aliases):
    """A reload call in `func_node` that happens after a `yield` in the same function (a
    fixture's teardown half), or inside a `finally` block."""
    yield_lines = [n.lineno for n in ast.walk(func_node) if isinstance(n, (ast.Yield, ast.YieldFrom))]
    reload_calls = _reload_calls(func_node, aliases)
    if not reload_calls:
        return False
    for call in reload_calls:
        if _in_finally(call, func_node):
            return True
        if yield_lines and call.lineno > min(yield_lines):
            return True
    return False


@pytest.mark.parametrize("path", sorted(_py_files()), ids=lambda p: os.path.relpath(p, ROOT))
def test_every_reload_of_main_is_paired_with_a_restore(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
    except (UnicodeDecodeError, OSError):
        pytest.skip("not a readable utf-8 .py file")
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        pytest.skip("not parseable (e.g. a fixtures/golden file, not real source)")

    aliases = _main_aliases(tree)
    if not aliases:
        pytest.skip("does not `import main` at all")

    top_level_calls = _reload_calls(tree, aliases)
    if not top_level_calls:
        pytest.skip("imports main but never calls importlib.reload() on it")

    # The two patterns actually used in this repo split reload-and-restore across TWO
    # functions (tests/test_tool_map_pinning.py, tests/test_exec_via_gateway.py: a helper
    # like `_registered()` does the reload, and a separate module-scoped autouse fixture
    # does the restoring reload in its teardown) as well as ONE function doing both
    # (relay/test_gateway_executes.py: a single fixture reloads on setup and again, after
    # `yield`, on teardown). Either is fine -- what matters is that the FILE somewhere
    # contains a restoring reload (after a `yield`, or inside a `finally`) at all; a file
    # that only ever reloads and never reloads back has no such function anywhere.
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    has_restore = any(_restoring_reload_exists(fn, aliases) for fn in functions)

    assert has_restore, (
        "%s calls importlib.reload() on `main` but no function in the file ever reloads it "
        "again after a `yield` (fixture teardown) or inside a `finally` block -- the "
        "synthetic MCP_API_KEY / MCP_TOOL_MAP this file sets before reloading will leak into "
        "main's module-level names (API_KEY, TOOLS, _ALL_TOOLS, ...) for the rest of the "
        "pytest session. See relay/test_gateway_executes.py and "
        "tests/test_exec_via_gateway.py for the fixture pattern that avoids this."
        % os.path.relpath(path, ROOT)
    )

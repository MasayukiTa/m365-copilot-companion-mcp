"""The command validator must admit every field produced by live goal writers."""

import ast
import re
from pathlib import Path

from relay import fleet_runner


ROOT = Path(__file__).resolve().parents[1]


def _python_item_keys(path, function, variable):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    fn = next(node for node in ast.walk(tree)
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
              and node.name == function)
    keys = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == variable:
                    value = node.value
                    # The router's fallback dictionary is inside an if-expression.
                    dictionaries = [n for n in ast.walk(value) if isinstance(n, ast.Dict)]
                    for dictionary in dictionaries:
                        for key in dictionary.keys:
                            assert isinstance(key, ast.Constant) and isinstance(key.value, str)
                            keys.add(key.value)
                if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                        and target.value.id == variable):
                    assert isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str)
                    keys.add(target.slice.value)
    return keys


def _csharp_item_keys(path, start, end, variable):
    source = (ROOT / path).read_text(encoding="utf-8")
    body = source[source.index(start):source.index(end, source.index(start))]
    return set(re.findall(r'\b' + variable + r'\["([^"\n]+)"\]\s*=', body))


def test_all_live_goal_writer_keys_are_allowed():
    keys = _python_item_keys("relay/task_router.py", "add_goal_to_live_fleet", "item")
    keys |= _python_item_keys("relay/code_task.py", "main", "entry")
    keys |= _csharp_item_keys("ui/FleetCockpit.Submit.cs", "var uiOrigin =", "var patch = Cmd1(\"add_goal\"", "item")
    keys |= _csharp_item_keys("ui/FleetCockpit.Card.cs", "Dictionary<string, object> RetryEntry(", "// Re-run the worker's goal", "item")
    keys |= _csharp_item_keys("ui/ChatSend.cs", "public static FleetSend DecideFleetSend(", "// ═══ ORCHESTRATION", "g")
    keys |= _csharp_item_keys("ui/ChatSend.cs", "var item = new Dictionary<string, object>();", 'bool ok = fx.AppendCommand("add_goal", item);', "item")
    assert {"text", "priority", "checks", "cwd", "jid", "resume_conv", "origin", "retry", "follow_up_to", "new_task"} <= keys
    assert keys <= fleet_runner._GOAL_KEYS, "unrecognized writer keys: %s" % sorted(keys - fleet_runner._GOAL_KEYS)


def test_origin_shape_is_validated():
    ok = {"text": "x", "origin": {"via": "mcp", "source": "tester"}}
    assert fleet_runner._goal_item_error(ok) == ""
    assert fleet_runner._goal_item_error({"text": "x", "origin": "mcp"})
    assert fleet_runner._goal_item_error({"text": "x", "origin": {"via": "mcp", "other": "y"}})
    assert fleet_runner._goal_item_error({"text": "x", "origin": {"via": 1}})

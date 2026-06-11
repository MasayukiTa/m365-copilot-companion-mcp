"""Tests for auto-verification detection (project_introspect) and the natural-language
front door (code_task.build_goal). No browser.

Proves the companion can DECIDE how to verify a folder from its contents -- the thing
that lets "fix the bug" work without the user writing --check-cmd -- and that a plain
instruction becomes one self-verifying goal.

Run:  .venv\\Scripts\\python.exe relay\\test_code_task.py
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.code_task import build_goal
from relay.project_introspect import detect_checks

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def types(res):
    return [c["type"] for c in res["checks"]]


def main():
    # 1. pytest.ini -> pytest check
    d = tempfile.mkdtemp(prefix="pi_pytest_")
    open(os.path.join(d, "pytest.ini"), "w").write("[pytest]\n")
    open(os.path.join(d, "mod.py"), "w").write("x=1\n")
    check("pytest_ini", "pytest" in types(detect_checks(d)))

    # 2. a test_*.py file -> pytest check
    d = tempfile.mkdtemp(prefix="pi_testfile_")
    open(os.path.join(d, "test_thing.py"), "w").write("def test_x():\n    assert 1\n")
    check("test_file", "pytest" in types(detect_checks(d)))

    # 3. tests/ dir -> pytest check
    d = tempfile.mkdtemp(prefix="pi_testsdir_")
    os.makedirs(os.path.join(d, "tests"))
    open(os.path.join(d, "app.py"), "w").write("x=1\n")
    check("tests_dir", "pytest" in types(detect_checks(d)))

    # 4. plain .py, no tests -> compileall (shell), NOT pytest
    d = tempfile.mkdtemp(prefix="pi_plainpy_")
    open(os.path.join(d, "lib.py"), "w").write("def f():\n    return 1\n")
    res = detect_checks(d)
    check("plain_py_compileall", types(res) == ["shell"]
          and "compileall" in " ".join(res["checks"][0]["argv"]))

    # 5. package.json with a real test script -> npm test
    d = tempfile.mkdtemp(prefix="pi_node_")
    open(os.path.join(d, "package.json"), "w").write('{"scripts":{"test":"jest"}}')
    check("npm_test", any("npm test" in c.get("cmd", "") for c in detect_checks(d)["checks"]))

    # 6. placeholder npm test is ignored
    d = tempfile.mkdtemp(prefix="pi_nodeplaceholder_")
    open(os.path.join(d, "package.json"), "w").write(
        '{"scripts":{"test":"echo \\"Error: no test specified\\" && exit 1"}}')
    check("npm_placeholder_ignored",
          not any("npm test" in c.get("cmd", "") for c in detect_checks(d)["checks"]))

    # 7. empty folder -> no checks, explanatory note
    d = tempfile.mkdtemp(prefix="pi_empty_")
    res = detect_checks(d)
    check("empty_no_checks", res["checks"] == [] and any("no automatic" in n for n in res["notes"]))

    # 8. build_goal: auto-verify attaches detected checks, cwd + instruction present
    d = tempfile.mkdtemp(prefix="pi_goal_")
    open(os.path.join(d, "pytest.ini"), "w").write("[pytest]\n")
    goal, notes = build_goal("落ちてるテストを直して", d)
    check("goal_has_checks", goal.get("checks") and goal["checks"][0]["type"] == "pytest")
    check("goal_cwd", goal["cwd"] == os.path.abspath(d))
    check("goal_text", "落ちてるテストを直して" in goal["text"] and os.path.abspath(d) in goal["text"])

    # 9. build_goal --no-verify -> no checks
    goal, notes = build_goal("x", d, no_verify=True)
    check("goal_no_verify", "checks" not in goal)

    # 10. build_goal extra_check appends a shell check
    goal, notes = build_goal("x", d, extra_check="ruff check .")
    check("goal_extra_check", any(c.get("cmd") == "ruff check ." for c in goal["checks"]))

    print("\n=== %d/%d code-task checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

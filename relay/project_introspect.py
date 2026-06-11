"""project_introspect.py -- look at a folder and DECIDE how to verify it.

This is what lets the companion behave like Claude Code instead of a CLI with flags:
you say a task in plain language, and the FRAME figures out how to prove it's done --
run the project's tests if there are any, otherwise at least make sure everything still
compiles. No --check-cmd / --verify ceremony from the user.

detect_checks(folder) returns acceptance-check specs (acceptance.Check format) plus a
short human note of what was detected, so a natural-language task can be turned into a
gated, self-verifying run automatically.

stdlib only.
"""
from __future__ import annotations

import json
import os
import sys

# Dirs we never treat as project content when sniffing / walking.
_SKIP = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__",
                   "dist", "build", ".fleet", ".setup", ".companion_runs"})


def _is(folder, name):
    return os.path.isfile(os.path.join(folder, name))


def _read(folder, name, cap=20000):
    try:
        with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
            return f.read(cap)
    except Exception:
        return ""


def _pytest_available():
    """True if pytest can actually be run in this interpreter -- so we never gate a run
    on a tool that is not installed (which would fail for an environmental reason, not
    the agent's work)."""
    try:
        import importlib.util
        return importlib.util.find_spec("pytest") is not None
    except Exception:
        return False


def _has_pytest_config(folder):
    """True if the folder declares pytest in any of the usual config files."""
    if _is(folder, "pytest.ini"):
        return True
    if _is(folder, "tox.ini") and "[pytest]" in _read(folder, "tox.ini"):
        return True
    if _is(folder, "pyproject.toml") and "[tool.pytest" in _read(folder, "pyproject.toml"):
        return True
    if _is(folder, "setup.cfg") and "[tool:pytest]" in _read(folder, "setup.cfg"):
        return True
    return False


def _scan(folder, max_files=4000):
    """One shallow-ish walk collecting the few signals we need: any test file, any .py,
    presence of a tests/ dir. Bounded so a huge tree can't stall the decision."""
    has_test_file = False
    has_py = False
    has_tests_dir = os.path.isdir(os.path.join(folder, "tests")) or \
        os.path.isdir(os.path.join(folder, "test"))
    seen = 0
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d not in _SKIP]
        for name in filenames:
            seen += 1
            if seen > max_files:
                return has_test_file, has_py, has_tests_dir
            low = name.lower()
            if low.endswith(".py"):
                has_py = True
                if low.startswith("test_") or low.endswith("_test.py"):
                    has_test_file = True
    return has_test_file, has_py, has_tests_dir


def _npm_test(folder):
    """Return the npm test command if package.json defines a non-placeholder test
    script, else None."""
    if not _is(folder, "package.json"):
        return None
    try:
        data = json.loads(_read(folder, "package.json"))
        t = (data.get("scripts") or {}).get("test", "")
        if t and "no test specified" not in t.lower():
            return "npm test --silent"
    except Exception:
        pass
    return None


def detect_checks(folder):
    """Decide how to verify `folder`. Returns {"checks": [spec...], "notes": [str...]}.

    Priority for Python: a real test suite (pytest) beats a mere compile. When there is
    no test suite we still gate on "does it all still compile" (compileall) so a DONE at
    least can't have left broken syntax. Node projects with a test script add `npm test`.
    Empty list = nothing detected (the run will accept a self-reported DONE as before).
    """
    folder = os.path.abspath(folder)
    checks = []
    notes = []
    if not os.path.isdir(folder):
        return {"checks": [], "notes": ["folder not found: %s" % folder]}

    has_test_file, has_py, has_tests_dir = _scan(folder)
    pytest_like = _has_pytest_config(folder) or has_test_file or has_tests_dir

    if pytest_like and _pytest_available():
        checks.append({"type": "pytest", "args": "-q", "cwd": folder})
        notes.append("Python tests detected -> verify with `pytest -q`")
    elif pytest_like and has_py:
        # tests exist but pytest is not installed in this env -> don't gate on a tool we
        # can't run (that would be a false failure). Fall back to a compile gate and say so.
        checks.append({"type": "shell",
                       "argv": [sys.executable, "-m", "compileall", "-q", "."],
                       "cwd": folder})
        notes.append("tests detected but pytest is NOT installed -> falling back to "
                     "compile-only; `pip install pytest` to gate on the tests")
    elif has_py:
        # folder-level syntax gate: every .py must still compile after the edits.
        checks.append({"type": "shell",
                       "argv": [sys.executable, "-m", "compileall", "-q", "."],
                       "cwd": folder})
        notes.append("Python sources (no test suite) -> verify everything still compiles")

    npm = _npm_test(folder)
    if npm:
        checks.append({"type": "shell", "cmd": npm, "cwd": folder})
        notes.append("package.json test script -> verify with `npm test`")

    if not checks:
        notes.append("no automatic verification detected (DONE will be trusted)")
    return {"checks": checks, "notes": notes}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Detect how to verify a project folder.")
    ap.add_argument("--folder", required=True)
    args = ap.parse_args()
    res = detect_checks(args.folder)
    for n in res["notes"]:
        print("- " + n)
    print(json.dumps(res["checks"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

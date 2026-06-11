"""Tests for relay/repo_map.py -- the codebase map that primes the agent.

Run:  .venv\\Scripts\\python.exe relay\\test_repo_map.py
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.repo_map import build_map

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def main():
    d = tempfile.mkdtemp(prefix="rm_")
    os.makedirs(os.path.join(d, "pkg"))
    os.makedirs(os.path.join(d, ".venv", "lib"))     # must be skipped
    open(os.path.join(d, "pkg", "mod.py"), "w", encoding="utf-8").write(
        '"""Module doc."""\n\n'
        "def foo(a, b, c=3, *args, **kw):\n    '''Foo does things.'''\n    return a\n\n"
        "class Bar:\n    '''A bar.'''\n    def m1(self, x):\n        pass\n    def m2(self):\n        pass\n")
    open(os.path.join(d, "app.js"), "w", encoding="utf-8").write("function x(){}\n")
    open(os.path.join(d, "broken.py"), "w", encoding="utf-8").write("def f(:\n  pass\n")
    open(os.path.join(d, ".venv", "lib", "junk.py"), "w", encoding="utf-8").write("def hidden(): pass\n")

    m = build_map(d)
    check("has_function_sig", "def foo(a, b, c=..., *args, **kw)" in m)
    check("has_func_doc", "Foo does things." in m)
    check("has_class", "class Bar" in m and "A bar." in m)
    check("has_methods", "def m1(self, x)" in m and "def m2(self)" in m)
    check("lists_js_file", "app.js" in m)
    check("broken_py_listed", "broken.py" in m)            # listed even if unparned
    check("skips_venv", "hidden" not in m and ".venv" not in m)

    # cap respected
    m2 = build_map(d, max_chars=120)
    check("cap_respected", len(m2) <= 200 and "truncated" in m2)

    # missing folder -> empty
    check("missing_folder_empty", build_map(os.path.join(d, "nope")) == "")

    print("\n=== %d/%d repo-map checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

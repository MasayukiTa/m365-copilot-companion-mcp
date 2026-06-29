"""Tests for the destructive-Python detector + contract-gate inertness.

Run: python -m tools.test_python_gate

Covers the safety gap closed on 2026-06-29: run_python() executes arbitrary Python,
so the shell-only destructive matcher missed os.remove / shutil.rmtree / Path.unlink /
truncating open(...,'w') / os.system / subprocess. destructive_python() now catches those
and run_python() routes them through the existing 'shell_destructive' contract op_class.
"""
from tools.contract_gate import destructive_python, destructive_shell, check_op


def test_destructive_python_positive():
    cases = [
        "import shutil; shutil.rmtree('x')",
        "import os\nos.remove('a.txt')",
        "import os\nos.unlink('a')",
        "import os\nos.rmdir('d')",
        "from pathlib import Path; Path('a').unlink()",
        "from pathlib import Path; Path('a').rmdir()",
        "from pathlib import Path; Path('a').write_text('x')",
        "open('out.txt','w').write('x')",
        "f = open('a', mode='a')",
        "import os; os.system('rm -rf /')",
        "import subprocess; subprocess.run(['ls'])",
        "import os; os.truncate('a', 0)",
    ]
    for c in cases:
        assert destructive_python(c), "expected destructive: %r" % c
    print("ok test_destructive_python_positive (%d)" % len(cases))


def test_destructive_python_negative():
    # read-only / benign code must NOT be flagged (else autonomy asks on everything)
    cases = [
        "print(open('a.txt','r').read())",
        "data = open('write.txt').read()",          # default mode 'r', filename contains 'w'
        "x = open('writeup.md').read(); print(len(x))",
        "print(sum(range(10)))",
        "import json; print(json.dumps({'a': 1}))",
        "with open('config.json') as f: cfg = f.read()",
    ]
    for c in cases:
        assert not destructive_python(c), "false positive on read-only: %r" % c
    print("ok test_destructive_python_negative (%d)" % len(cases))


def test_inert_without_contract():
    # No active contract -> check_op is a no-op even for a destructive op_class, so
    # run_python's behaviour is unchanged when no autonomy contract is active.
    # (contract_gate locates .fleet/active_contract.json; in a normal dev checkout it is
    #  absent or inactive -> check_op returns None.)
    assert check_op("shell_destructive", "shutil.rmtree('x')") is None
    print("ok test_inert_without_contract")


def test_shell_still_works():
    # the pre-existing shell matcher is untouched
    assert destructive_shell("rm -rf /tmp/x")
    assert not destructive_shell("pytest -q")
    print("ok test_shell_still_works")


if __name__ == "__main__":
    test_destructive_python_positive()
    test_destructive_python_negative()
    test_inert_without_contract()
    test_shell_still_works()
    print("ALL PYTHON GATE TESTS PASSED")

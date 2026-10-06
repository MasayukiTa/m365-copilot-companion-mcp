# -*- coding: utf-8 -*-
"""setup.bat and bootstrap.py must agree on the oldest usable Python (D8, 2026-09-24).

The number lives in two languages -- a cmd one-liner that probes interpreters before any
Python code of ours can run, and bootstrap.MIN_PYTHON -- so this pins them together, and runs
setup.bat's probe code under the interpreter running the tests to prove it is valid Python
that answers 0 here (CI runs 3.10, the minimum itself).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

from tools import childproc  # noqa: E402


def _bat_value(name):
    text = (REPO / "setup.bat").read_text(encoding="ascii")
    m = re.search(r'^set "%s=(.*)"\s*$' % re.escape(name), text, re.M)
    assert m, "setup.bat no longer sets %s" % name
    return m.group(1)


def _min_python():
    src = (HERE / "bootstrap.py").read_text(encoding="utf-8")
    m = re.search(r"^MIN_PYTHON = \((\d+), (\d+)\)", src, re.M)
    assert m
    return int(m.group(1)), int(m.group(2))


def test_the_batch_probe_uses_bootstraps_minimum():
    check = _bat_value("PY_MIN_CHECK")
    m = re.search(r">= \((\d+), (\d+)\)", check)
    assert m and (int(m.group(1)), int(m.group(2))) == _min_python()
    assert _bat_value("PY_MIN_TEXT") == "%d.%d" % _min_python()


def test_the_batch_probe_is_valid_python_and_distinguishes_too_old():
    check = _bat_value("PY_MIN_CHECK")
    r = childproc.run([sys.executable, "-c", check])
    assert r.returncode == (0 if sys.version_info[:2] >= _min_python() else 3)
    forced = check.replace("sys.version_info[:2]", "(3, 9)")
    assert childproc.run([sys.executable, "-c", forced]).returncode == 3


def test_the_minimum_is_one_the_ci_interpreter_satisfies():
    # CI installs and imports the shipped requirements on 3.10; a minimum above that would
    # refuse the very interpreter the dependency set is verified on.
    assert _min_python() <= (3, 10)

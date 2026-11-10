# -*- coding: utf-8 -*-
"""Regression test for the brand-new-PC STEP 1/7 install failure.

THE FAILURE. On a fresh PC the installer died in bootstrap STEP 1/7 at
``from tools.childproc import run`` (``_venv_version`` -> ``_venv_usable``).
The real cause is NOT any import-time statement on the traceback's childproc
line: it is that ``from tools.childproc import run`` fails with
``ModuleNotFoundError: No module named 'tools'`` whenever the repo ROOT (the
PARENT of the ``tools`` package) is not on sys.path at the moment of import.

WHY ROOT CAN BE ABSENT. bootstrap is launched as ``python scripts/bootstrap.py``,
so the interpreter puts the ``scripts`` directory on sys.path[0] -- NOT ROOT.
If nothing adds ROOT, the very first ``from tools.childproc import`` in STEP 1/7
cannot resolve the ``tools`` package. We prove the raw condition really fails:
a child whose cwd is ``scripts`` and whose sys.path lacks ROOT raises
ModuleNotFoundError on that import. The self-heal written inside
tools/childproc.py CANNOT rescue this path -- a module body does not run until
the module is found, so an import that fails to resolve never reaches it.

THE FIX THAT ACTUALLY HOLDS, AND WHAT THIS TEST PINS. scripts/bootstrap.py must
put ROOT on sys.path BEFORE importing tools.childproc, at module top level, so
STEP 1/7 resolves the ``tools`` package no matter the launcher's cwd. This test
launches bootstrap exactly the way setup.bat does -- ``python <path>/bootstrap.py
--status`` with the current directory set to the ``scripts`` dir and ROOT scrubbed
from PYTHONPATH -- and requires it to run its STEP table without dying on the
tools import. It is the regression gate for the brand-new-PC STEP 1/7 failure.

WHY --status. It exercises the same module-level import wiring as a real run
(the ``from tools.childproc import`` call sites are reached through the same ROOT
bootstrapping) but touches no venv, network, or system state, so it is fast and
hermetic on the Linux CI runner.

OS-INDEPENDENT. No Windows-only or POSIX-only constructs; runs the same on the
Linux CI runner and on Windows.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
BOOTSTRAP = SCRIPTS / "bootstrap.py"


def _child_env() -> dict:
    env = dict(os.environ)
    # Remove any inherited path injection so ROOT is on sys.path ONLY if bootstrap
    # itself puts it there -- the whole point of the regression.
    env.pop("PYTHONPATH", None)
    return env


def test_raw_import_from_scripts_cwd_without_root_fails() -> None:
    """Pin the FAILURE CONDITION itself: with cwd=scripts and ROOT off sys.path,
    a bare ``from tools.childproc import run`` cannot resolve the package. This is
    the brand-new-PC STEP 1/7 trigger and the reason bootstrap must add ROOT.
    """
    child = (
        "import sys, os\n"
        "root = os.environ['REPO_ROOT']\n"
        "sys.path[:] = [p for p in sys.path "
        "if os.path.abspath(p or os.getcwd()) != os.path.abspath(root)]\n"
        "import tools.childproc\n"
    )
    env = _child_env()
    env["REPO_ROOT"] = str(ROOT)
    proc = subprocess.run(
        [sys.executable, "-I", "-c", child],
        cwd=str(SCRIPTS),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0, (
        "Expected ModuleNotFoundError when ROOT is absent from sys.path, but the "
        "import succeeded -- the reproduction no longer holds.\n" + proc.stderr
    )
    assert "No module named 'tools'" in proc.stderr, proc.stderr


def test_bootstrap_status_resolves_tools_import_from_scripts_cwd() -> None:
    """Pin THE FIX: launched the setup.bat way (script path, cwd=scripts, no
    PYTHONPATH), bootstrap must add ROOT at module top and reach its STEP table
    without ``ModuleNotFoundError: No module named 'tools'``.
    """
    proc = subprocess.run(
        [sys.executable, str(BOOTSTRAP), "--status"],
        cwd=str(SCRIPTS),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=180,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "No module named 'tools'" not in combined, (
        "bootstrap --status died on the tools.childproc import when started from "
        "the scripts dir without ROOT on PYTHONPATH -- the brand-new-PC STEP 1/7 "
        "regression.\n" + combined
    )
    assert proc.returncode == 0, (
        "bootstrap --status should print its step table and exit 0.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    # The STEP table is what --status prints once imports resolved.
    assert "ensure_venv" in proc.stdout, proc.stdout

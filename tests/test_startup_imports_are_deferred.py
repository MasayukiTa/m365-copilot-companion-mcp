"""Importing main -- the whole startup path -- must load no heavy third-party library.

GOAL CONTEXT. The launch->listen gap (supervisor log: "server not listening yet
(ConnectFailure); main.py alive N s after launch") was dominated by importing
pandas (which drags in numpy and pyarrow), python-docx and python-pptx at module
load. Those imports were moved into the functions that use them (commit 7f864ec4),
so the cost moves from boot to first use.

WHY THIS TEST EXISTS ALONGSIDE test_heavy_libraries_are_not_imported_at_startup.py.
That sibling test checks the three TOOL MODULES in isolation (import tools.data_ops
etc.). This test checks the END-TO-END startup image: `import main`, which imports
every tools.* module AND runs all @mcp.tool registration -- exactly the import set
the process has built up at the moment just before uvicorn binds 127.0.0.1:8000. A
heavy library could be reintroduced by some OTHER module pulled in only by main
(not by the three tool modules), and only this test would catch that. The two tests
are deliberately complementary: per-module vs. whole-image.

WHY A SUBPROCESS. 'is pandas imported?' can only be answered in a process that did
NOT already import it for some unrelated reason. A fresh interpreter is used whose
sys.path is pinned to this repo root (PYTHONPATH stripped, any sibling checkout
dropped) so it imports THIS worktree's main and tools.*, never a live checkout.

HERMETIC. No server, no socket bound, no network, no login, no GPU. It installs
nothing; it only imports modules requirements.txt already provides and inspects
sys.modules. Runs on the ubuntu-latest CI runner.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tools import childproc  # locale-safe child output

# Repo root = parent of this tests/ directory. Pinned into the child's sys.path[0]
# so the child imports this worktree, never a sibling live checkout.
ROOT = Path(__file__).resolve().parent.parent

# The libraries that must stay out of the import-time path. pandas is the big one
# (it imports numpy and, in this project's version, pyarrow); docx/pptx are the
# document builders; matplotlib is listed defensively -- nothing should import it
# at startup either.
HEAVY = ("pandas", "numpy", "pyarrow", "docx", "pptx", "matplotlib")


def _child_env() -> dict:
    env = dict(os.environ)
    # Strip PYTHONPATH so a sibling checkout (or a stray autostamp dir) cannot be
    # imported ahead of this repo; the child pins sys.path[0] itself.
    env.pop("PYTHONPATH", None)
    # main's import-time code reads a few env knobs; give it an inert default so the
    # child never blocks on a real credential.
    env.setdefault("MCP_API_KEY", "dummy-for-test")
    return env


def test_importing_main_loads_no_heavy_library_at_startup():
    # Drop ONLY sibling-checkout entries: any sys.path dir that is NOT this ROOT yet
    # contains a tools/data_ops.py (i.e. another copy of this repo, e.g. the live
    # checkout). stdlib, DLLs and site-packages are left completely untouched so the
    # interpreter still finds _socket, pandas, etc.
    code = (
        "import sys, os, json\n"
        "import os.path as _p\n"
        "ROOT = r'''" + str(ROOT) + "'''\n"
        "_rootn = _p.normcase(_p.abspath(ROOT))\n"
        "def _is_sibling_repo(x):\n"
        "    if not x:\n"
        "        return False\n"
        "    ax = _p.normcase(_p.abspath(x))\n"
        "    if ax == _rootn:\n"
        "        return False\n"
        "    return _p.isfile(_p.join(x, 'tools', 'data_ops.py'))\n"
        "sys.path = [x for x in sys.path if not _is_sibling_repo(x)]\n"
        "sys.path.insert(0, ROOT)\n"
        "import main\n"
        "HEAVY = " + repr(list(HEAVY)) + "\n"
        "present = [m for m in HEAVY if m in sys.modules]\n"
        "print('HEAVY_PRESENT=' + json.dumps(present))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=_child_env(),
        capture_output=True,
        timeout=300,
    )
    out, err = childproc.decode(proc.stdout), childproc.decode(proc.stderr)
    assert proc.returncode == 0, (
        "importing main failed (rc=%s)\nSTDOUT:\n%s\nSTDERR:\n%s"
        % (proc.returncode, out, err[-2000:])
    )
    line = [l for l in out.splitlines() if l.startswith("HEAVY_PRESENT=")]
    assert line, "probe produced no HEAVY_PRESENT line\nSTDOUT:\n%s" % out
    present = json.loads(line[-1].split("=", 1)[1])
    assert present == [], (
        "importing main loaded heavy libraries at startup: %s -- these must be "
        "deferred to first use so the launch->listen gap stays small" % present
    )

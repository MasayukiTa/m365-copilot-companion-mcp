"""Heavy third-party libraries must NOT be imported at server-startup time.

GOAL CONTEXT. The launch->listen gap (supervisor log: "server not listening yet
(ConnectFailure); main.py alive N s after launch") was dominated by importing
pandas (which drags in numpy and pyarrow), python-docx and python-pptx at module
load. Those imports were moved into the functions that use them, so the cost moves
from boot to first use. This test is the regression guard: it fails the moment any
of those libraries is pulled back to import time.

WHY A SUBPROCESS. 'is pandas imported?' can only be answered in a process that did
NOT already import it for some unrelated reason. pytest itself, or an earlier test,
may have imported pandas; so each check runs in a FRESH interpreter whose sys.path
is pinned to this repo root (and whose PYTHONPATH is stripped) so it imports THIS
worktree's tools.* and nothing from a sibling checkout.

HERMETIC. No server, no socket, no network, no login, no GPU. It installs nothing;
it only imports modules that requirements.txt already provides and inspects
sys.modules. Runs on the ubuntu-latest CI runner.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Repo root = parent of this tests/ directory. Pinned into the child's sys.path[0]
# so the child imports this worktree, never a sibling live checkout.
ROOT = Path(__file__).resolve().parent.parent

# The libraries that must stay out of the import-time path. pandas is the big one
# (it imports numpy and, in this project's version, pyarrow); docx/pptx are the
# document builders. matplotlib is listed defensively -- nothing should import it
# at startup either.
HEAVY = ("pandas", "numpy", "pyarrow", "docx", "pptx", "matplotlib")

# Modules whose top-level import used to pull the heavy libs in. Importing these is
# a tighter, faster proxy for "startup" than importing all of main (no auth/env/
# watchers), and it is exactly where the deferral edits live.
DEFERRED_MODULES = ("tools.data_ops", "tools.docx_ops", "tools.pptx_ops")


def _child_env() -> dict:
    env = dict(os.environ)
    # Strip PYTHONPATH so a sibling checkout (or a stray autostamp dir) cannot be
    # imported ahead of this repo; the child pins sys.path[0] itself.
    env.pop("PYTHONPATH", None)
    # main import-time code reads a few env knobs; give it inert defaults so the
    # child never blocks on real credentials.
    env.setdefault("MCP_API_KEY", "dummy-for-test")
    return env


def _run_probe(import_stmt: str) -> list:
    """Run `import_stmt` in a fresh interpreter and return the heavy libs that ended
    up in sys.modules. sys.path[0] is pinned to ROOT and any path pointing outside
    ROOT (a sibling checkout) is dropped so 'import tools.*' resolves to THIS repo."""
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
        + import_stmt + "\n"
        "HEAVY = " + repr(list(HEAVY)) + "\n"
        "present = [m for m in HEAVY if m in sys.modules]\n"
        "print('HEAVY_PRESENT=' + json.dumps(present))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        "probe failed (rc=%s)\nSTDOUT:\n%s\nSTDERR:\n%s"
        % (proc.returncode, proc.stdout, proc.stderr[-2000:])
    )
    line = [l for l in proc.stdout.splitlines() if l.startswith("HEAVY_PRESENT=")]
    assert line, "probe produced no HEAVY_PRESENT line\nSTDOUT:\n%s" % proc.stdout
    return json.loads(line[-1].split("=", 1)[1])


@pytest.mark.parametrize("module", DEFERRED_MODULES)
def test_importing_a_deferred_module_pulls_in_no_heavy_library(module):
    present = _run_probe("import " + module)
    assert present == [], (
        "importing %s loaded heavy libraries at import time: %s -- move those "
        "imports inside the functions that use them" % (module, present)
    )


def test_importing_main_loads_no_heavy_library_at_startup():
    """The whole point, end to end: importing main (which imports every tools.*
    module and registers the tools) must not drag in pandas/docx/pptx."""
    present = _run_probe("import main")
    assert present == [], (
        "importing main loaded heavy libraries at startup: %s -- these must be "
        "deferred to first use so the launch->listen gap stays small" % present
    )

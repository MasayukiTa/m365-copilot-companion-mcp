"""The server must bind its port quickly, so the heavy third-party libraries used by only a few
tools (pandas, python-docx, python-pptx) are imported inside those tools, not when the tool
modules are imported.

Measured on 2026-11-01: importing main took about 20 s on the production PC (CPU at 100%, under
1 GB of free memory), and every deploy restarts the server, during which the tunnel relay answers
502 to every client. Moving these three imports into the functions that use them cut the import
time to about 14 s without changing the tool list.

Each check runs in a fresh interpreter so an earlier test cannot have loaded the library.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CASES = [
    ("tools.data_ops", "pandas"),
    ("tools.docx_ops", "docx"),
    ("tools.pptx_ops", "pptx"),
]


@pytest.mark.parametrize("module,heavy", CASES)
def test_importing_the_tool_module_does_not_load_the_heavy_library(module, heavy):
    code = (
        "import sys; sys.path.insert(0, %r); import %s; "
        "print('LOADED' if %r in sys.modules else 'DEFERRED')" % (REPO, module, heavy)
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True, text=True, timeout=120, cwd=REPO,
    )
    assert out.returncode == 0, out.stderr[-600:]
    assert out.stdout.strip().endswith("DEFERRED"), (
        "%s imports %s at module import time again; import it inside the function that uses it"
        % (module, heavy)
    )

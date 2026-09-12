# -*- coding: utf-8 -*-
"""`MCP_GATE_DIR` isolated one of the two gate writers, so it isolated nothing you could trust.

MEASURED 2026-09-12, by walking into it. An isolated demonstration set `MCP_GATE_DIR` to a
sandbox and ran a contract. The approval gate appeared in the OPERATOR'S LIVE
`~/.companion_gates` regardless -- its file count went 1433 -> 1434 while the sandbox stayed
empty and the script reported, in its own step name, that the operator's machine was untouched.

    tools/gate_ops.py:42     GATE_DIR = Path(os.environ.get("MCP_GATE_DIR") or
                                             (ALLOWED_BASE / ".companion_gates"))   # honours it
    tools/contract_gate.py   _find_existing_gate / _create_gate built the same path by hand

contract_gate's own comment said the directory "mirrors gate_ops.py / GATE_DIR": the
duplication was known and the override was simply not carried across.

PARTIAL ISOLATION IS WORSE THAN NONE. The variable existing is what tells a caller the writes
are contained, and half of them were. The same shape had already cost four minutes of a live
fleet kill-switch on 2026-09-10, when MCP_GATE_DIR was not set at all and the switch turned out
to live under HOME rather than inside the worktree -- so this is the second time one gate path
has escaped an isolation that looked complete.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
PY = os.path.join(REPO, ".venv", "Scripts", "python.exe")


def test_the_two_writers_resolve_one_directory():
    """Read as source because the alternative is asserting on a path this process computed --
    and the defect was precisely that two places computed it differently."""
    src = open(os.path.join(REPO, "tools", "contract_gate.py"), encoding="utf-8").read()
    body = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    assert 'ALLOWED_BASE / ".companion_gates"' not in body.replace(
        'return ALLOWED_BASE / ".companion_gates"', "", 1), (
        "a gate path is still being built by hand instead of resolved through gate_ops")
    assert "def _gate_dir()" in body
    assert body.count("_gate_dir()") >= 3, "not every caller goes through the resolver"


def test_gate_ops_is_still_the_one_that_reads_the_variable():
    """If gate_ops stops honouring MCP_GATE_DIR, resolving through it quietly stops isolating
    anything -- and this file would keep passing."""
    src = open(os.path.join(REPO, "tools", "gate_ops.py"), encoding="utf-8").read()
    assert 'os.environ.get("MCP_GATE_DIR")' in src


@pytest.mark.skipif(not os.path.isfile(PY), reason="no venv interpreter here (CI)")
def test_a_contract_gate_lands_in_the_sandbox_and_nowhere_else(tmp_path):
    """THE MEASUREMENT THAT FAILED. A child process with MCP_GATE_DIR set must write its
    approval gate there, and must not touch the live directory.

    Run as a subprocess because gate_ops resolves GATE_DIR at import time from the
    environment -- setting the variable inside an already-imported interpreter would prove
    nothing about how a real isolated run behaves.
    """
    sandbox = tmp_path / "gates"
    live = os.path.join(os.path.expanduser("~"), ".companion_gates")
    before = len(os.listdir(live)) if os.path.isdir(live) else 0

    env = dict(os.environ)
    env["MCP_GATE_DIR"] = str(sandbox)
    env.pop("PYTEST_CURRENT_TEST", None)
    code = (
        "import sys, json, os; sys.path.insert(0, r'%s');\n"
        "import tools.contract_gate as CG\n"
        "CG._create_gate('gate_isolation_probe', 'probe?', 'isolation probe')\n"
        "print(json.dumps({'dir': str(CG._gate_dir())}))\n" % REPO)
    p = subprocess.run([PY, "-c", code], capture_output=True, cwd=REPO, env=env, timeout=120)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")[-600:]

    assert (sandbox / "gate_isolation_probe.json").is_file(), (
        "the gate did not land in the sandbox MCP_GATE_DIR named")
    after = len(os.listdir(live)) if os.path.isdir(live) else 0
    assert after == before, (
        "the live gate directory grew by %d while an isolated run was in progress"
        % (after - before))


@pytest.mark.skipif(not os.path.isfile(PY), reason="no venv interpreter here (CI)")
def test_without_the_variable_it_still_uses_the_default(tmp_path):
    """The override must not have become mandatory: a normal run with no variable set has to
    keep writing where the cockpit looks."""
    env = dict(os.environ)
    env.pop("MCP_GATE_DIR", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    code = (
        "import sys, json, os; sys.path.insert(0, r'%s');\n"
        "import tools.contract_gate as CG\n"
        "from tools.file_ops import ALLOWED_BASE\n"
        "print(json.dumps({'same': str(CG._gate_dir()) == str(ALLOWED_BASE / '.companion_gates')}))\n"
        % REPO)
    p = subprocess.run([PY, "-c", code], capture_output=True, cwd=REPO, env=env, timeout=120)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")[-600:]
    out = json.loads(p.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    assert out["same"] is True


def test_the_resolver_survives_a_missing_gate_ops(monkeypatch):
    """A gate that cannot be written at all silently turns an approval into an allow, so the
    fallback matters more than the override does."""
    import builtins
    import tools.contract_gate as CG
    real = builtins.__import__

    def boom(name, *a, **k):
        if name == "tools.gate_ops":
            raise ImportError("gone")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    importlib.reload  # keep the import used; the resolver is called fresh below
    assert str(CG._gate_dir()).endswith(".companion_gates")

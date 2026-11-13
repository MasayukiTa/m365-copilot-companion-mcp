"""Pre-started Python interpreters for run_python.

WHY. On this class of PC the interpreter start-up alone costs 1-2 s (0.7-0.9 s even with
-S; Defender real-time scanning competes for the CPU), and a print(1) through run_python
measured 2.0-14.6 s (median 4.7 s) in a separate process on 2026-11-13. A client that cuts
the call after about 7 s therefore sees timeouts for trivial snippets. The start-up is paid
here, BEFORE a call arrives: N interpreters are already running and blocked reading one
JSON line from stdin. run_python takes one, sends {"path", "cwd"}, and the child runs the
script with runpy and exits. Each warm interpreter is used once, so no state leaks between
calls; the pool is topped up in the background.

Anything that goes wrong (pool disabled, empty, interpreter dead, write fails) returns
None from take() / raises from send, and the caller falls back to the plain Popen path.
A warm interpreter ends when the server dies: its stdin pipe closes and the read returns
EOF, and it is also created with the same tree-kill options as every other run_python child.

MCP_RUNPY_POOL = number of warm interpreters (default 2, 0 = off, read on every call).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from typing import List, Optional

from ._subproc import sanitized_child_env
from . import childproc

_BOOT = (
    "import sys, os, json, runpy\n"
    "line = sys.stdin.readline()\n"
    "if not line:\n"
    "    sys.exit(0)\n"
    "req = json.loads(line)\n"
    "_dn = os.open(os.devnull, os.O_RDONLY)\n"
    "os.dup2(_dn, 0)\n"
    "sys.stdin = open(os.devnull, 'r')\n"
    "os.chdir(req['cwd'])\n"
    "sys.argv = [req['path']]\n"
    "sys.path[0] = os.path.dirname(req['path'])\n"
    "runpy.run_path(req['path'], run_name='__main__')\n"
)

_lock = threading.Lock()
_pool: List[subprocess.Popen] = []
_filling = False


def pool_size() -> int:
    try:
        return max(0, int(os.environ.get("MCP_RUNPY_POOL", "2")))
    except ValueError:
        return 2


def _spawn() -> Optional[subprocess.Popen]:
    try:
        return subprocess.Popen(
            [sys.executable, "-c", _BOOT],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            env=sanitized_child_env(),
            **childproc.tree_popen_kwargs(headless=True),
        )
    except Exception:
        return None


def _top_up() -> None:
    global _filling
    try:
        while True:
            want = pool_size()
            with _lock:
                _pool[:] = [p for p in _pool if p.poll() is None]
                if len(_pool) >= want:
                    return
            p = _spawn()
            if p is None:
                return
            with _lock:
                _pool.append(p)
    finally:
        with _lock:
            _filling = False


def ensure_filling() -> None:
    """Start a background top-up unless one is running or the pool is off."""
    global _filling
    if pool_size() <= 0:
        return
    with _lock:
        if _filling:
            return
        _filling = True
    try:
        threading.Thread(target=_top_up, name="runpy-pool-fill", daemon=True).start()
    except Exception:
        with _lock:
            _filling = False


def take() -> Optional[subprocess.Popen]:
    """One live warm interpreter, or None (the caller then starts one the old way)."""
    if pool_size() <= 0:
        return None
    got = None
    with _lock:
        while _pool:
            p = _pool.pop(0)
            if p.poll() is None:
                got = p
                break
    ensure_filling()
    return got


def send(proc: subprocess.Popen, path: str, cwd: str) -> bool:
    """Hand the script to a warm interpreter. False = it was unusable (already cleaned up)."""
    try:
        proc.stdin.write((json.dumps({"path": path, "cwd": cwd}) + "\n").encode("utf-8"))
        proc.stdin.flush()
        proc.stdin.close()
        return True
    except Exception:
        try:
            childproc.kill_tree(proc, wait_s=2)
        except Exception:
            pass
        for s in (proc.stdout, proc.stderr, proc.stdin):
            try:
                s.close()
            except Exception:
                pass
        return False


def shutdown() -> None:
    with _lock:
        procs = list(_pool)
        _pool.clear()
    for p in procs:
        try:
            childproc.kill_tree(p, wait_s=2)
        except Exception:
            pass
        for s in (p.stdout, p.stderr, p.stdin):
            try:
                s.close()
            except Exception:
                pass

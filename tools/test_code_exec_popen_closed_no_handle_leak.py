# -*- coding: utf-8 -*-
"""run_python / shell_exec must close the Popen they open, on every path.

WHY THIS EXISTS. _run_with_tree_timeout (shell_exec) and _run_python_tree (run_python) both
spawn with subprocess.Popen(stdout=PIPE, stderr=PIPE). On Windows that opens two kernel pipe
handles, plus the child process and primary-thread handles the Popen object holds. The old
body wrote `proc = subprocess.Popen(...)` and never closed it explicitly: the Popen was left
for __del__ to close at some later garbage collection, and on the timeout path the second
proc.communicate(timeout=5) can itself raise, so even that fallback could skip the close.
Until the GC ran, the pipe handles stayed open, so each timed-out (and, under a non-compacting
GC, each ordinary) run_python/shell_exec leaked handles that accumulated in the long-lived
main.py process.

MEASURED 2026-11-11: main.py's handle count climbed from ~1445 to ~2300 over ~17 minutes of
fleet traffic, with a +570 step during a burst of tool calls; the supervisor had to restart
main.py when /health began timing out.

THE FIX. subprocess.Popen is a context manager; its __exit__ closes stdout/stderr/stdin and
waits. Wrapping the spawn in `with subprocess.Popen(...) as proc:` releases the handles
deterministically at the end of the call -- on a normal return, on the timeout return, and on
any exception -- instead of at an unpredictable later GC.

These tests are OS-independent: they patch subprocess.Popen with a fake that records whether it
was used as a context manager (its __exit__ ran), so they verify the close contract without
spawning a real process or reading Windows handle tables.
"""
import ast
import os
import subprocess

import pytest

from tools import code_exec as CE

_SRC_PATH = os.path.join(os.path.dirname(os.path.abspath(CE.__file__)), "code_exec.py")


class _FakePopen:
    """Stand-in for subprocess.Popen that records context-manager use.

    It is deliberately minimal: communicate() returns immediately, so no real child and no real
    handles are involved. __enter__/__exit__ set flags so a test can prove the spawn was used
    inside a `with` (i.e. that the production code closes it deterministically).
    """

    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = 0
        self.entered = False
        self.exited = False
        self.closed = False
        _FakePopen.instances.append(self)

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited = True
        self.closed = True
        return False  # never suppress exceptions

    def communicate(self, timeout=None):
        return (b"out", b"err")

    # If the code path ever fell back to manual teardown we would still see it.
    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass

    def terminate(self):
        pass


@pytest.fixture(autouse=True)
def _reset_instances():
    _FakePopen.instances = []
    yield
    _FakePopen.instances = []


def _patch(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    # Neutralise the side helpers so the test is hermetic and OS-independent.
    monkeypatch.setattr(CE, "sanitized_child_env", lambda: {}, raising=False)
    monkeypatch.setattr(CE, "_kill_tree", lambda proc: None, raising=False)
    monkeypatch.setattr(CE.childproc, "tree_popen_kwargs", lambda **k: {}, raising=False)
    monkeypatch.setattr(CE.proc_registry, "register", lambda *a, **k: None, raising=False)


def test_shell_exec_path_closes_the_popen(monkeypatch, tmp_path):
    _patch(monkeypatch)
    CE._run_with_tree_timeout("echo hi", 30, str(tmp_path))
    assert _FakePopen.instances, "shell_exec no longer spawns through subprocess.Popen"
    p = _FakePopen.instances[-1]
    assert p.entered, "the Popen was not used as a context manager (no `with`)"
    assert p.exited and p.closed, "the Popen was not closed on return -- handles would leak"


def test_run_python_path_closes_the_popen(monkeypatch, tmp_path):
    _patch(monkeypatch)
    CE._run_python_tree(["python", "-c", "pass"], 30, str(tmp_path))
    assert _FakePopen.instances, "run_python no longer spawns through subprocess.Popen"
    p = _FakePopen.instances[-1]
    assert p.entered, "the Popen was not used as a context manager (no `with`)"
    assert p.exited and p.closed, "the Popen was not closed on return -- handles would leak"


def test_shell_exec_closes_the_popen_even_on_timeout(monkeypatch, tmp_path):
    """The timeout path is where the old code was most likely to leak: the second
    communicate(timeout=5) can raise, and before this fix nothing closed the Popen afterwards.
    """
    _patch(monkeypatch)

    class _TimeoutPopen(_FakePopen):
        def communicate(self, timeout=None):
            # First call (the real timeout) raises; the fallback call also raises, exercising
            # the `except Exception` branch -- the worst case for leaking a handle.
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout or 0)

    monkeypatch.setattr(subprocess, "Popen", _TimeoutPopen)
    out = CE._run_with_tree_timeout("ping -n 20 127.0.0.1", 1, str(tmp_path))
    assert "timeout" in out
    p = _TimeoutPopen.instances[-1]
    assert p.exited and p.closed, "the Popen was not closed on the timeout path -- the leak"


def test_run_python_closes_the_popen_even_on_timeout(monkeypatch, tmp_path):
    _patch(monkeypatch)

    class _TimeoutPopen(_FakePopen):
        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout or 0)

    monkeypatch.setattr(subprocess, "Popen", _TimeoutPopen)
    with pytest.raises(subprocess.TimeoutExpired):
        CE._run_python_tree(["python", "-c", "pass"], 1, str(tmp_path))
    p = _TimeoutPopen.instances[-1]
    assert p.exited and p.closed, "the Popen was not closed on the timeout path -- the leak"


def test_source_spawns_only_inside_a_with_block():
    """Static backstop: a future edit that reintroduces a bare `proc = subprocess.Popen(...)`
    (not wrapped in `with`) would reopen the leak. Parse the source and insist every
    subprocess.Popen call in code_exec.py is the context expression of a `with` statement.
    """
    src = open(_SRC_PATH, encoding="utf-8").read()
    tree = ast.parse(src)

    # Collect the Popen call nodes that are directly the context manager of a `with`.
    with_popen_calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                ce = item.context_expr
                if _is_subprocess_popen_call(ce):
                    with_popen_calls.add(id(ce))

    all_popen_calls = [n for n in ast.walk(tree) if _is_subprocess_popen_call(n)]
    assert all_popen_calls, "no subprocess.Popen call found -- the test is testing nothing"
    for call in all_popen_calls:
        assert id(call) in with_popen_calls, (
            "a subprocess.Popen in code_exec.py is not wrapped in `with` -- its pipe handles "
            "would be closed only at GC, which is the handle leak this guards against"
        )


def _is_subprocess_popen_call(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    )

# -*- coding: utf-8 -*-
"""proc_registry: a worker's tool-call processes are recorded at spawn and killed when the
worker ends -- the fix for the 2026-11-02 incident where closed/cancelled workers left
python.exe / powershell.exe running on the desktop for 20+ minutes.

Hermetic: MCP_PROC_REGISTRY_REPO redirects the whole .fleet/procs tree into tmp_path, and the
"real process" the tests kill is a short-lived `python -c 'time.sleep(N)'` child we start and
are responsible for -- never anything else on the machine. Every test confirms both that the
thing that SHOULD die does, and that a thing that should NOT (this process, an unrelated live
process, a recycled pid) is left alone.
"""
import os
import subprocess
import sys
import time

import pytest

from tools import childproc
from tools import proc_registry as PR

psutil = pytest.importorskip("psutil")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect the registry under tmp_path and clear any worker binding between tests."""
    monkeypatch.setenv("MCP_PROC_REGISTRY_REPO", str(tmp_path))
    tok = PR.set_worker("")
    yield
    PR.reset_worker(tok)


def _spawn_sleeper(seconds=30):
    """Start a real child that just sleeps, and return the Popen. The caller must ensure it is
    killed (every test does, via the code under test or an explicit teardown)."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(%d)" % seconds],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )


def _alive(pid):
    try:
        return psutil.pid_exists(int(pid))
    except Exception:
        return False


# --- recording -------------------------------------------------------------------------------
def test_register_requires_a_worker():
    """A spawn made outside any worker turn is NOT recorded -- an unattributed pid is exactly
    what a later kill must never act on."""
    assert PR.register(12345, tool="run_python") is False
    assert PR.registered("nobody") == []


def test_register_under_worker_scope_records_pid_and_create_time():
    p = _spawn_sleeper()
    try:
        with PR.worker_scope("w-A"):
            assert PR.register(p.pid, tool="run_python") is True
        rows = PR.registered("w-A")
        assert len(rows) == 1
        assert rows[0]["pid"] == p.pid
        assert rows[0]["create_time"] is not None
        assert rows[0]["owner_pid"] == os.getpid()
    finally:
        p.kill(); p.wait(timeout=5)


def test_contextvar_worker_is_used_when_arg_omitted():
    p = _spawn_sleeper()
    try:
        tok = PR.set_worker("w-ctx")
        try:
            assert PR.register(p.pid) is True   # no worker= arg: reads the ContextVar
        finally:
            PR.reset_worker(tok)
        assert [r["pid"] for r in PR.registered("w-ctx")] == [p.pid]
    finally:
        p.kill(); p.wait(timeout=5)


# --- the kill, and its fences ----------------------------------------------------------------
def test_kill_worker_tree_kills_the_registered_process():
    p = _spawn_sleeper()
    with PR.worker_scope("w-kill"):
        PR.register(p.pid, tool="run_python")
    assert _alive(p.pid)
    rep = PR.kill_worker_tree("w-kill")
    assert p.pid in rep["killed"]
    # give the OS a beat, then confirm it is actually gone
    for _ in range(50):
        if not _alive(p.pid):
            break
        time.sleep(0.1)
    assert not _alive(p.pid)


def test_kill_skips_a_recycled_pid_create_time_mismatch():
    """PID REUSE GUARD. A row whose recorded creation time does not match the live process of
    that pid is skipped -- the live process is an unrelated one that inherited the number."""
    p = _spawn_sleeper()
    try:
        with PR.worker_scope("w-reuse"):
            PR.register(p.pid, tool="run_python")
        # Corrupt the recorded create_time to simulate a recycled pid: same number, different
        # process identity. The real process p must survive.
        path = PR._path_for("w-reuse")
        rows = [l for l in open(path, encoding="utf-8")]
        import json
        obj = json.loads(rows[0]); obj["create_time"] = (obj["create_time"] or 0) + 10000.0
        open(path, "w", encoding="utf-8").write(json.dumps(obj) + "\n")
        rep = PR.kill_worker_tree("w-reuse")
        assert p.pid not in rep["killed"]
        assert _alive(p.pid)   # untouched
    finally:
        p.kill(); p.wait(timeout=5)


def test_kill_skips_a_row_whose_create_time_was_never_read():
    """A row with create_time=None (could not be read at spawn) fails SAFE: it is never a
    match, so the process is left alone."""
    p = _spawn_sleeper()
    try:
        path = PR._path_for("w-none")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import json
        open(path, "w", encoding="utf-8").write(
            json.dumps({"pid": p.pid, "create_time": None, "owner_pid": os.getpid()}) + "\n")
        rep = PR.kill_worker_tree("w-none")
        assert p.pid not in rep["killed"]
        assert _alive(p.pid)
    finally:
        p.kill(); p.wait(timeout=5)


def test_kill_worker_tree_kills_grandchildren():
    """The incident was a GRANDCHILD outliving the worker. A child that itself spawns a child
    must have the whole tree reaped."""
    parent = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess,sys,time;"
         "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
         "print(c.pid,flush=True);time.sleep(60)"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )
    try:
        # Read the grandchild's pid as BYTES and decode via tools.childproc.decode -- never
        # text=True, which would decode child output with the local code page (cp932 here) and
        # lose the whole line on one bad byte. The child prints only an ASCII integer, but the
        # decode policy is the one the repository's ratchet requires of every child-output read.
        grandchild_pid = int(childproc.decode(parent.stdout.readline()).strip())
        with PR.worker_scope("w-tree"):
            PR.register(parent.pid, tool="run_python")
        assert _alive(parent.pid) and _alive(grandchild_pid)
        PR.kill_worker_tree("w-tree")
        for _ in range(50):
            if not _alive(parent.pid) and not _alive(grandchild_pid):
                break
            time.sleep(0.1)
        assert not _alive(parent.pid)
        assert not _alive(grandchild_pid)
    finally:
        for pid in (parent.pid,):
            try:
                psutil.Process(pid).kill()
            except Exception:
                pass


def test_kill_never_touches_this_process_or_an_unrelated_one():
    """Only registered pids are candidates. A live process that was never registered -- here,
    THIS test process and a second sleeper -- is never killed."""
    bystander = _spawn_sleeper()
    target = _spawn_sleeper()
    try:
        with PR.worker_scope("w-scope"):
            PR.register(target.pid, tool="run_python")   # bystander NOT registered
        rep = PR.kill_worker_tree("w-scope")
        assert target.pid in rep["killed"]
        assert _alive(bystander.pid)          # bystander survived
        assert _alive(os.getpid())            # we survived
    finally:
        for q in (bystander, target):
            try:
                q.kill(); q.wait(timeout=5)
            except Exception:
                pass


# --- sweeps ----------------------------------------------------------------------------------
def test_sweep_dead_owners_reaps_only_dead_owner_rows():
    """owner-scoped sweep: a row whose owning server pid is dead is reaped; a row whose owner
    is alive (here, this test process) is kept. Concurrent-run safe."""
    # A process whose pid we will reuse as a DEAD owner_pid. Started and then killed so its pid
    # is gone; we do not depend on a sleeper exiting on its own within a fixed window (that made
    # this test flaky on a loaded machine, where even `python -c 'sleep(1)'` can take >5s just to
    # start). Kill is prompt and load-independent.
    dead_owner = _spawn_sleeper(30)
    dead_owner_pid = dead_owner.pid
    dead_owner.kill()
    try:
        dead_owner.wait(timeout=30)
    except Exception:
        pass
    for _ in range(100):
        if not _alive(dead_owner_pid):
            break
        time.sleep(0.1)
    assert not _alive(dead_owner_pid)

    orphan = _spawn_sleeper()
    live = _spawn_sleeper()
    try:
        import json
        # orphan: owned by the now-dead pid -> should be reaped
        path_o = PR._path_for("w-orphan")
        os.makedirs(os.path.dirname(path_o), exist_ok=True)
        open(path_o, "w", encoding="utf-8").write(json.dumps({
            "pid": orphan.pid, "create_time": psutil.Process(orphan.pid).create_time(),
            "owner_pid": dead_owner_pid}) + "\n")
        # live: owned by THIS process (alive) -> should be kept
        with PR.worker_scope("w-liveowner"):
            PR.register(live.pid, tool="run_python")

        out = PR.sweep_dead_owners()
        assert any(orphan.pid in rep["killed"] for rep in out["reaped"])
        assert os.getpid() in out["kept_owners"]
        for _ in range(50):
            if not _alive(orphan.pid):
                break
            time.sleep(0.1)
        assert not _alive(orphan.pid)   # orphan reaped
        assert _alive(live.pid)         # live owner's process kept
    finally:
        for q in (orphan, live):
            try:
                q.kill(); q.wait(timeout=5)
            except Exception:
                pass


def test_all_public_functions_are_total_on_garbage():
    """Nothing here raises into a tool call, whatever the input."""
    assert PR.register("not-an-int", worker="w") is False
    assert PR.registered("no-such-worker") == []
    assert PR.kill_worker_tree("no-such-worker")["killed"] == []
    assert isinstance(PR.sweep_dead_owners(), dict)
    assert PR.forget("no-such-worker") is False


# --- integration: the spawn choke point records under the active worker ----------------------
def test_childproc_run_registers_its_child_under_the_current_worker():
    """The wiring, not just the module: a process started through childproc.run()'s timeout path
    -- the choke point run_python/shell_exec/run_powershell share -- is recorded for whatever
    worker's turn is running, read from the ContextVar the gateway sets. This is what lets a
    worker closed mid-call, or a coordinator-start sweep, kill the tree instead of orphaning it.
    """
    # A timeout is given so run() drives Popen itself (the branch that registers); the child
    # exits at once so the call completes well inside the timeout.
    with PR.worker_scope("w-run"):
        childproc.run([sys.executable, "-c", "pass"], timeout=30)
    rows = PR.registered("w-run")
    assert rows, "childproc.run did not record its child under the active worker"
    assert all(isinstance(r.get("pid"), int) and r["pid"] > 0 for r in rows)
    assert any(r.get("tool") == "childproc.run" for r in rows)


def test_childproc_run_records_nothing_when_no_worker_is_bound():
    """Outside a worker turn (a CLI/test spawn) the same call records nothing: an unattributed
    pid is exactly what a later kill must never act on. The _isolate fixture leaves the worker
    cleared, so this asserts the no-op path directly."""
    before = os.listdir(PR.registry_dir()) if os.path.isdir(PR.registry_dir()) else []
    childproc.run([sys.executable, "-c", "pass"], timeout=30)
    after = os.listdir(PR.registry_dir()) if os.path.isdir(PR.registry_dir()) else []
    assert after == before

"""Routing a worker's work into its container, and refusing rather than running it here.

The broker and its client were both verified before anything used them -- measured, no tool
referenced the client at all and the worktrees were still on this machine. A verified door
that nobody is sent through is not containment.

The property that makes routing worth having is the negative one: no local fallback. A router
that quietly runs the command here when it cannot reach a container leaves the run looking
identical and unconfined, and the operator has no way to tell the two apart.
"""
import io
import json

import os
import pytest

from relay import fleet_tool_router as R


@pytest.fixture
def wt(tmp_path, monkeypatch):
    a = tmp_path / "p00"
    b = tmp_path / "p01"
    a.mkdir()
    b.mkdir()
    m = tmp_path / "wt.json"
    io.open(m, "w", encoding="utf-8").write(json.dumps({"inst-a": str(a), "inst-b": str(b)}))
    monkeypatch.setattr(R, "WT_MAP", str(m))
    return a, b


def test_a_path_inside_a_worktree_names_its_instance(wt):
    a, _ = wt
    assert R.instance_for(str(a)) == "inst-a"
    assert R.instance_for(str(a / "src" / "x.py")) == "inst-a"


def test_a_path_outside_every_worktree_names_none(wt, tmp_path):
    assert R.instance_for(str(tmp_path / "elsewhere")) is None


def test_a_prefix_is_not_a_parent(wt, tmp_path):
    """p0 must not claim p00's files. Matching on a bare startswith would."""
    (tmp_path / "p0").mkdir()
    assert R.instance_for(str(tmp_path / "p0")) is None


def test_paths_translate_to_the_checkout_not_the_scratch_mount(wt):
    """The Step 3 pilot established that these images carry the repo at /app. A tool pointed
    at /work would run cleanly and edit nothing, which is the hardest kind of broken."""
    a, _ = wt
    assert R.to_container_path(str(a), "inst-a") == "/app"
    assert R.to_container_path(str(a / "src" / "x.py"), "inst-a") == "/app/src/x.py"


def test_a_path_outside_the_instance_is_refused(wt, tmp_path):
    a, _ = wt
    with pytest.raises(R.NotRoutable):
        R.to_container_path(str(tmp_path / "other" / "y.py"), "inst-a")


def test_routing_off_refuses_rather_than_running_here(wt, monkeypatch, tmp_path):
    """THE PROPERTY. Off means the caller falls back by its own choice; this function never
    does it silently."""
    from relay import broker_client as bc
    monkeypatch.delenv("SWE_BROKER", raising=False)
    # AND THE MARKER. Deleting only the environment variable left routing ON here,
    # because the repository has a real .fleet/BROKER_ON while a run is in flight.
    monkeypatch.setattr(bc, "MARKER", str(tmp_path / "BROKER_ON"))
    with pytest.raises(R.NotRoutable) as e:
        R.route("shell_exec", {"command": "ls"})
    assert "routing is off" in str(e.value)


def test_an_unplaceable_call_is_refused_not_run(wt, monkeypatch, tmp_path):
    """No instance owns the path, so there is no container to run in -- and running it on the
    operator's machine instead is exactly what this exists to prevent."""
    monkeypatch.setenv("SWE_BROKER", "on")
    # UNDER THE STAGING ROOT. Outside it the call is not the fleet's at all and passes
    # through, which is the distinction this module now draws; naming a tmp dir tested the
    # pass-through, not the refusal.
    with pytest.raises(R.NotRoutable) as e:
        R.route("shell_exec", {"command": "ls",
                               "working_dir": os.path.join(R.STAGING_ROOT, "p_unstaged")})
    assert "must not run outside one" in str(e.value)


def test_an_allowed_but_unrouted_tool_is_refused_while_routing_is_on(wt, monkeypatch):
    """git_diff is in the fleet's allowed set and has no container equivalent here yet.
    Allowed-but-unroutable must refuse, not run locally: keeping the two lists separate is
    what makes that distinction expressible at all."""
    monkeypatch.setenv("SWE_BROKER", "on")
    # UNDER THE STAGING ROOT. Outside it the call is not the fleet's at all and passes
    # through, which is the distinction this module now draws; naming a tmp dir tested the
    # pass-through, not the refusal.
    with pytest.raises(R.NotRoutable) as e:
        R.route("git_diff", {})
    assert "no container equivalent" in str(e.value)


def test_the_command_enters_the_checkout_before_running(wt, monkeypatch):
    """The container's working directory is the scratch mount. A build run there would
    succeed and touch nothing."""
    a, _ = wt
    seen = {}
    monkeypatch.setenv("SWE_BROKER", "on")
    from relay import broker_client as bc
    monkeypatch.setattr(bc, "exec_", lambda inst, cmd, timeout=600: seen.update(
        {"inst": inst, "cmd": cmd}) or {"rc": 0, "output": "ok"})
    R.route("shell_exec", {"command": "make test", "working_dir": str(a)})
    assert seen["inst"] == "inst-a"
    assert seen["cmd"].startswith("cd /app && ")


def test_there_is_no_local_execution_path_in_the_module():
    """Asserted against the source. One `except NotRoutable: run_locally()` would undo the
    whole thing while leaving every behavioural test above still passing."""
    import inspect
    src = inspect.getsource(R)
    for bad in ("subprocess", "os.system", "popen", "shell=True"):
        assert bad not in src.lower(), "a local execution path appeared: %s" % bad


def test_the_gateway_routes_before_it_runs_anything_locally():
    """Asserted against main.py's source, because booting an MCP server to check an ordering
    is a worse test than reading the order.

    The refusal must come from the router branch and not from a later one: a call that reaches
    the local dispatcher has already escaped the boundary."""
    import io
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(root, "main.py"), encoding="utf-8").read()
    assert "from relay import fleet_tool_router as _router" in src
    i_route = src.index("_router.route(name, _args)")
    i_local = src.index("_out = fn(**_args)")
    assert i_route < i_local, "the local dispatch happens before routing is considered"


def test_routing_off_is_the_only_case_that_continues_to_local():
    """When routing is ON, NotRoutable must refuse. Treating it as a generic fallback signal
    would turn every unroutable call into a local one -- the exact hole this closes."""
    import io
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(root, "main.py"), encoding="utf-8").read()
    block = src[src.index("from relay import broker_client as _bc"):]
    block = block[:block.index("from relay.fleet_toolset import check")]
    # Matched as a CONDITION, not as a literal line: the gate later grew a second term
    # (only while a fleet run is in flight), and an equality check on the old text failed
    # while the property it was written to protect was still held.
    import re as _re
    cond = _re.search(r"if\s+_bc\.enabled\(\)(.*?):", block)
    assert cond, "routing must be gated on being enabled"
    assert "return (" in block and "was not run here" in block

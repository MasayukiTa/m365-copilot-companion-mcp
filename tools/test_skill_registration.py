import json
import os
import subprocess
import sys
import time


def _main_catalog(tmp_path, tool_map="0"):
    env = os.environ.copy()
    env.update({
        "MCP_API_KEY": "test-token",
        "MCP_EXECUTION_PROFILES": "1",
        "MCP_TOOL_MAP": tool_map,
        "MCP_TOOL_MAP_MAX": "8",
        "MCP_LOCAL_JOB_DB": str(tmp_path / "jobs.sqlite3"),
        "MCP_SKILLS_STATE_DB": str(tmp_path / "skills.sqlite3"),
    })
    code = (
        "import json, main; "
        "print(json.dumps({'all': sorted(main._ALL_TOOLS), "
        "'registered': [f.__name__ for f in main.TOOLS], "
        "'annotations': main._TOOL_ANNOTATIONS}))"
    )
    # 30s HAD 2.7x HEADROOM, MEASURED. Importing `main` takes ~11s under a modest six-worker
    # CPU load on this machine, and this suite is one of the pair that has only ever failed
    # inside a full run. A `TimeoutExpired` here is indistinguishable from a real registration
    # defect in the output, so the budget is widened AND the two are made to read differently.
    # Widened rather than removed: an import that genuinely hangs should still end the test.
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=os.getcwd(), env=env,
            text=True, capture_output=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "importing `main` in a child did not finish within 180s. This is contention, not "
            "a registration defect -- it measured ~11s under a six-worker load. Nothing about "
            "the tool catalogue is being asserted by this failure.")
    elapsed = time.time() - started
    assert proc.returncode == 0, (
        "the child exited %r after %.1fs, so the catalogue was never produced. stderr: %s"
        % (proc.returncode, elapsed, proc.stderr))
    out = proc.stdout.strip()
    assert out, (
        "the child exited 0 after %.1fs but printed nothing on stdout. stderr: %s"
        % (elapsed, proc.stderr))
    return json.loads(out)


def test_skill_tools_are_read_only_and_gate_answer_is_not_model_facing(tmp_path):
    catalog = _main_catalog(tmp_path)
    for name in ("skill_list", "skill_match", "skill_load", "skill_read_resource"):
        assert name in catalog["all"]
        assert catalog["annotations"][name]["readOnlyHint"] is True
    assert "gate_answer" not in catalog["all"]


def test_local_loop_map_keeps_skills_gateway_only(tmp_path):
    catalog = _main_catalog(tmp_path, tool_map="1")
    assert catalog["registered"][:2] == ["unlock", "call_tool"]
    assert len(catalog["registered"]) == 8
    assert "skill_load" not in catalog["registered"]
    assert "skill_load" in catalog["all"]
